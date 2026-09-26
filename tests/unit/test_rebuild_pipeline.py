"""Customers rebuild the image themselves: snapshot pinning, SBOM, Ansible hook, containers, CI."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("sbom", ROOT / "tools" / "sbom.py")
sbom = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(sbom)
COLLECTION = ROOT / "ansible/collections/ansible_collections/synos/workstation"


def _render(manifest: Path, directory: Path) -> tuple[str, dict]:
    output = directory / "args.sh"
    resolved = directory / "resolved.json"
    result = subprocess.run(
        (sys.executable, str(ROOT / "tools/render_manifest.py"), "--manifest", str(manifest),
         "--output", str(output), "--resolved", str(resolved)),
        text=True, capture_output=True, check=False, cwd=ROOT)
    assert result.returncode == 0, result.stderr
    return output.read_text(encoding="utf-8"), json.loads(resolved.read_text(encoding="utf-8"))


class SnapshotPinningTests(unittest.TestCase):
    def test_snapshot_date_pins_build_mirrors_and_keeps_live_release_mirrors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            text, _ = _render(ROOT / "manifest.yml", Path(directory))
        self.assertIn('export SNAPSHOT_PINNED="true"', text)
        self.assertIn('export APT_SOURCE="https://snapshot.ubuntu.com/ubuntu/20260901T000000Z/"', text)
        self.assertIn('export RELEASE_APT_SOURCE="http://archive.ubuntu.com/ubuntu/"', text)
        build = (ROOT / "build.sh").read_text(encoding="utf-8")
        self.assertIn('Acquire::Check-Valid-Until "false"', build)
        cleanup = (ROOT / "mods/85-cleanup-mod/install.sh").read_text(encoding="utf-8")
        self.assertIn("RELEASE_APT_SOURCE", cleanup)
        self.assertIn("rm -f /etc/apt/apt.conf.d/80-snapshot-build", cleanup)

    def test_explicit_mirror_disables_pinning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "m.yml"
            manifest.write_text(json.dumps({
                "schema_version": 1, "name": "m", "version": "1.0.0", "base": "debian", "suite": "trixie",
                "arch": "amd64", "profile": "minimal", "regions": ["fr"], "brand": "synos",
                "mirrors": {"snapshot": "2026-09-01", "apt": "http://mirror.corp.example/debian/"},
            }), encoding="utf-8")
            text, _ = _render(manifest, Path(directory))
        self.assertIn('export SNAPSHOT_PINNED="false"', text)
        self.assertIn('export APT_SOURCE="http://mirror.corp.example/debian/"', text)


class SbomTests(unittest.TestCase):
    def test_lock_and_cyclonedx_from_dpkg_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            _, resolved = _render(ROOT / "manifest.yml", work)
            dpkg = work / "filesystem.manifest"
            dpkg.write_text("bash 5.2-1\nlibc6:amd64 2.40-1\nsynos-branding 1.0.0\nbash 5.2-1\n", encoding="utf-8")
            result = subprocess.run(
                (sys.executable, str(ROOT / "tools/sbom.py"), "--dpkg-manifest", str(dpkg),
                 "--resolved", str(work / "resolved.json"), "--stem", str(work / "out" / "SynOS-1.0.0-amd64")),
                text=True, capture_output=True, check=False)
            self.assertEqual(0, result.returncode, result.stderr)
            lock = (work / "out/SynOS-1.0.0-amd64.packages.lock").read_text(encoding="utf-8")
            self.assertEqual("bash=5.2-1\nlibc6:amd64=2.40-1\nsynos-branding=1.0.0\n", lock)
            bom = json.loads((work / "out/SynOS-1.0.0-amd64.sbom.cdx.json").read_text(encoding="utf-8"))
            self.assertEqual("CycloneDX", bom["bomFormat"])
            self.assertEqual(3, len(bom["components"]))
            self.assertEqual("pkg:deb/ubuntu/libc6@2.40-1?arch=amd64", bom["components"][1]["purl"])
            self.assertEqual("operating-system", bom["metadata"]["component"]["type"])
        build = (ROOT / "build.sh").read_text(encoding="utf-8")
        self.assertIn("tools/sbom.py", build)
        self.assertIn(".resolved.json", build)


class AnsibleHookTests(unittest.TestCase):
    def test_policy_profiles_require_ansible_in_the_chroot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "m.yml"
            manifest.write_text(json.dumps({
                "schema_version": 1, "name": "acme", "version": "1.0.0", "base": "ubuntu", "suite": "resolute",
                "arch": "amd64", "profile": "example-acme-finance", "regions": ["fr", "be"], "brand": "example-acme",
            }), encoding="utf-8")
            text, _ = _render(manifest, Path(directory))
            variables = json.loads((Path(directory) / "ansible-vars.json").read_text(encoding="utf-8"))
        self.assertIn('export ANSIBLE_CHROOT_REQUIRED="true"', text)
        self.assertIn('export ANSIBLE_PLAYBOOKS="ansible/playbooks/examples/acme-finance.yml"', text)
        self.assertEqual("active-directory", variables["synos_profile"]["directory"]["join"])
        self.assertEqual("example-acme", variables["synos_brand"]["id"])

    def test_ansible_is_required_whenever_a_profile_adds_packages_of_its_own(self) -> None:
        """This repository's own manifest.yml builds the workstation profile, whose
        `software.packages.add` carries keepassxc — so Ansible is required, because
        the live-session gating role has to look at what units those packages ship
        and suppress them under rd.synos.live (see tools/render_manifest.py's
        appliance package set and docs/ARCHITECTURE.md's "Live session vs.
        installed system"). Skipping that silently would ship an image whose
        appliance services run in a passwordless live session, so this is a
        deliberate, loud requirement rather than the "false" it used to be; the
        builder image carries ansible (bases/*/Containerfile), so no ordinary
        build is affected. A profile that adds nothing of its own still says
        false — tests/unit/test_manifest_render.py holds that case."""
        with tempfile.TemporaryDirectory() as directory:
            text, _ = _render(ROOT / "manifest.yml", Path(directory))
        self.assertIn('export PROFILE_PACKAGES_ADD="keepassxc"', text)
        self.assertIn('export ANSIBLE_CHROOT_REQUIRED="true"', text)

    def test_missing_customer_playbook_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "m.yml"
            manifest.write_text(json.dumps({
                "schema_version": 1, "name": "m", "version": "1.0.0", "base": "ubuntu", "suite": "resolute",
                "arch": "amd64", "profile": "minimal", "regions": ["fr"], "brand": "synos",
                "overrides": {"ansible": {"playbooks": ["nope/missing.yml"]}},
            }), encoding="utf-8")
            result = subprocess.run(
                (sys.executable, str(ROOT / "tools/render_manifest.py"), "--manifest", str(manifest), "--check"),
                text=True, capture_output=True, check=False, cwd=ROOT)
        self.assertEqual(1, result.returncode)
        self.assertIn("does not exist", result.stderr)

    def test_build_runs_the_collection_playbook_in_the_chroot(self) -> None:
        build = (ROOT / "build.sh").read_text(encoding="utf-8")
        self.assertIn("--connection community.general.chroot", build)
        self.assertIn("playbooks/customize_chroot.yml", build)
        self.assertIn("run_chroot\nrun_ansible_chroot\nrun_cleanup_mod\numount_folders", build)

    def test_collection_is_well_formed(self) -> None:
        galaxy = yaml.safe_load((COLLECTION / "galaxy.yml").read_text(encoding="utf-8"))
        self.assertEqual(("synos", "workstation"), (galaxy["namespace"], galaxy["name"]))
        for role in ("desktop_branding", "base_hardening", "directory_join", "workplace_apps", "desktop_policy", "fleet_enrollment"):
            tasks = yaml.safe_load((COLLECTION / "roles" / role / "tasks/main.yml").read_text(encoding="utf-8"))
            self.assertIsInstance(tasks, list, role)
            self.assertTrue(all("name" in task for task in tasks), role)
            yaml.safe_load((COLLECTION / "roles" / role / "defaults/main.yml").read_text(encoding="utf-8"))
        for playbook in ("customize_chroot.yml", "build.yml"):
            plays = yaml.safe_load((COLLECTION / "playbooks" / playbook).read_text(encoding="utf-8"))
            self.assertIsInstance(plays, list)


class ContainerAndCiTests(unittest.TestCase):
    def test_every_base_has_a_containerfile_and_make_builds_in_it(self) -> None:
        for base in ("ubuntu", "debian"):
            text = (ROOT / "bases" / base / "Containerfile").read_text(encoding="utf-8")
            self.assertIn("ARG SUITE", text)
            self.assertIn("SYNOS_IN_CONTAINER=1", text)
            self.assertIn("debootstrap", text)
        makefile = (ROOT / "makefile").read_text(encoding="utf-8")
        self.assertIn("container-build:", makefile)
        self.assertIn("tools/synos build", makefile)
        self.assertIn("--privileged", (ROOT / "tools" / "synos").read_text(encoding="utf-8"))
        self.assertIn('[ -z "$$SYNOS_IN_CONTAINER" ]', makefile)

    def test_ci_templates_parse_and_cover_the_pipeline(self) -> None:
        gitlab = yaml.safe_load((ROOT / "ci/gitlab-ci.yml").read_text(encoding="utf-8"))
        self.assertEqual(["validate", "build", "test", "release"], gitlab["stages"])
        self.assertIn("container-build", " ".join(gitlab["build-iso"]["script"]))
        github = yaml.safe_load((ROOT / ".github/workflows/build.yml").read_text(encoding="utf-8"))
        self.assertEqual({"validate", "build", "acceptance"}, set(github["jobs"]))


if __name__ == "__main__":
    unittest.main()


class ThirdPartySoftwareTests(unittest.TestCase):
    def _stage(self, overrides: dict, profile: str = "minimal") -> tuple[Path, dict, "tempfile.TemporaryDirectory[str]"]:
        directory = tempfile.TemporaryDirectory()
        manifest = Path(directory.name) / "m.yml"
        manifest.write_text(json.dumps({
            "schema_version": 1, "name": "m", "version": "1.0.0", "base": "ubuntu", "suite": "resolute",
            "arch": "amd64", "profile": profile, "regions": ["fr"], "brand": "synos", "overrides": overrides,
        }), encoding="utf-8")
        _, resolved = _render(manifest, Path(directory.name))
        return Path(directory.name) / "software", resolved, directory

    def test_customer_profile_stages_repositories_keys_and_flatpak(self) -> None:
        staging, resolved, directory = self._stage({}, profile="example-acme-finance")
        with directory:
            self.assertTrue((staging / "keyrings/acme-internal.asc").is_file())
            sources = (staging / "repos/acme-internal.sources").read_text(encoding="utf-8")
            self.assertIn("Signed-By: /etc/apt/keyrings/acme-internal.asc", sources)
            self.assertIn("Architectures: amd64", sources)
            self.assertEqual("acme-internal\tacme-vpn-client acme-agent\n", (staging / "repos.txt").read_text(encoding="utf-8"))
            flatpak = (staging / "flatpak.txt").read_text(encoding="utf-8")
            self.assertIn("preinstall|true\n", flatpak)
            self.assertIn("app|org.onlyoffice.desktopeditors\n", flatpak)
            self.assertEqual(["acme-vpn-client", "acme-agent"], resolved["software"]["repositories"][0]["packages"])

    def test_missing_or_invalid_repository_key_is_rejected(self) -> None:
        for key in ("keys/nope.asc", "keys/README.md"):
            with tempfile.TemporaryDirectory() as directory:
                manifest = Path(directory) / "m.yml"
                manifest.write_text(json.dumps({
                    "schema_version": 1, "name": "m", "version": "1.0.0", "base": "ubuntu", "suite": "resolute",
                    "arch": "amd64", "profile": "minimal", "regions": ["fr"], "brand": "synos",
                    "overrides": {"software": {"repositories": [{"name": "x", "url": "https://x.example", "key": key}]}},
                }), encoding="utf-8")
                result = subprocess.run(
                    (sys.executable, str(ROOT / "tools/render_manifest.py"), "--manifest", str(manifest), "--check"),
                    text=True, capture_output=True, check=False, cwd=ROOT)
                self.assertEqual(1, result.returncode, key)
                self.assertIn("manifest error", result.stderr)

    def test_debs_appimages_and_deferred_flatpak(self) -> None:
        digest = "a" * 64
        staging, resolved, directory = self._stage({"software": {
            "debs": [{"url": "https://vendor.example/agent_1.0_amd64.deb", "sha256": digest}],
            "appimages": [{"name": "Obsidian", "url": "https://vendor.example/Obsidian.AppImage", "sha256": digest}],
            "flatpak": {"apps": ["org.zotero.Zotero"], "preinstall": False},
        }})
        with directory:
            self.assertEqual(f"https://vendor.example/agent_1.0_amd64.deb\t{digest}\n", (staging / "debs.txt").read_text(encoding="utf-8"))
            self.assertIn("Obsidian\thttps://vendor.example/Obsidian.AppImage", (staging / "appimages.txt").read_text(encoding="utf-8"))
            flatpak = (staging / "flatpak.txt").read_text(encoding="utf-8")
            self.assertIn("preinstall|false\n", flatpak)
            self.assertIn("remote|flathub|https://dl.flathub.org/repo/flathub.flatpakrepo\n", flatpak)
            self.assertFalse(resolved["software"]["flatpak"]["preinstall"])

    def test_build_and_mod_consume_the_staging_folder(self) -> None:
        build = (ROOT / "build.sh").read_text(encoding="utf-8")
        self.assertIn("new_building_os/root/software", build)
        mod = (ROOT / "mods/08-third-party-software/install.sh").read_text(encoding="utf-8")
        for needle in ("sha256sum --check", "/etc/apt/keyrings/", "flatpak remote-add --system", "synos-flatpak-first-boot.service", "/opt/appimages"):
            self.assertIn(needle, mod)
        mods = sorted(p.name for p in (ROOT / "mods").iterdir() if p.is_dir())
        self.assertLess(mods.index("07-brand-kit"), mods.index("08-third-party-software"))
        self.assertLess(mods.index("08-third-party-software"), mods.index("80-dracut-live-image"))
