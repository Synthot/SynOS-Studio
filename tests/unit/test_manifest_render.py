"""The manifest renderer is the only writer of args.sh."""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("render_manifest", ROOT / "tools" / "render_manifest.py")
render_manifest = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(render_manifest)


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (sys.executable, str(ROOT / "tools" / "render_manifest.py"), *args),
        text=True, capture_output=True, check=False, cwd=ROOT,
    )


class ManifestRenderTests(unittest.TestCase):
    def test_repository_manifest_validates(self) -> None:
        result = _run("--check")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("OK:", result.stdout)

    def test_rendered_args_carry_the_engine_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "args.sh"
            resolved = Path(directory) / "resolved.json"
            result = _run("--output", str(output), "--resolved", str(resolved))
            self.assertEqual(0, result.returncode, result.stderr)
            text = output.read_text(encoding="utf-8")
            for name in (
                "TARGET_NAME", "TARGET_BUSINESS_NAME", "TARGET_FILE_NAME", "TARGET_BUILD_VERSION", "TARGET_ARCH",
                "TARGET_UBUNTU_VERSION", "APT_SOURCE", "LANGUAGE_PACKS", "SUPPORTED_LIVE_REGIONS",
                "BASE_ID", "REGION_DEFAULT", "BRAND_ID",
            ):
                self.assertIn(f"\nexport {name}=", text, msg=name)
            self.assertIn("GENERATED FILE", text)
            self.assertEqual(0, subprocess.run(("bash", "-n", str(output)), check=False).returncode)
            data = json.loads(resolved.read_text(encoding="utf-8"))
            rows = re.search(r'SUPPORTED_LIVE_REGIONS="\n(.*?)\n"', text, re.DOTALL).group(1).splitlines()
            self.assertEqual(len(data["live_entries"]), len(rows))
            self.assertEqual(data["profile_chain"][0], "minimal")

    def test_profile_chain_merges_lists_and_overrides_scalars(self) -> None:
        profile, chain = render_manifest.resolve_profile("developer")
        self.assertEqual(["minimal", "workstation", "developer"], chain)
        self.assertIn("office", profile["software"]["bundles"])
        self.assertIn("containers", profile["software"]["bundles"])
        self.assertTrue(profile["installer"]["passwordless_sudo"])
        self.assertEqual("btrfs", profile["installer"]["filesystem"])

    def test_live_rows_are_one_per_region_locale_and_grub_safe(self) -> None:
        regions = render_manifest.resolve_regions(["be", "fr", "cn"])
        rows = render_manifest.live_region_rows(regions)
        self.assertEqual(["fr_BE", "nl_BE", "fr_FR", "zh_CN"], [row[0] for row in rows])
        self.assertEqual("Simplified Chinese (China Mainland)", rows[3][1])
        self.assertEqual(len({row[1] for row in rows}), len(rows))
        self.assertFalse(any(re.search(r'["\\$|]', row[1]) for row in rows))

    def test_unknown_base_profile_region_or_brand_is_rejected(self) -> None:
        for field, value in (("base", "arch"), ("profile", "nope"), ("regions", ["xx"]), ("brand", "nobody")):
            with tempfile.TemporaryDirectory() as directory:
                manifest = Path(directory) / "manifest.yml"
                data = {
                    "schema_version": 1, "name": "t", "version": "1.0.0", "base": "debian", "suite": "trixie",
                    "arch": "amd64", "profile": "minimal", "regions": ["fr"], "brand": "synos",
                }
                data[field] = value
                manifest.write_text(json.dumps(data), encoding="utf-8")
                result = _run("--manifest", str(manifest), "--check")
                self.assertEqual(1, result.returncode, field)
                self.assertIn("manifest error", result.stderr)

    def test_extends_cycle_is_rejected(self) -> None:
        with self.assertRaises(render_manifest.ManifestError):
            render_manifest.resolve_profile("a", seen=("b", "a"))


if __name__ == "__main__":
    unittest.main()


class BaseAdapterTests(unittest.TestCase):
    """Bases are folders of data; the engine must not name a distribution."""

    def _render(self, manifest: str) -> tuple[str, dict]:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "args.sh"
            resolved = Path(directory) / "resolved.json"
            result = _run("--manifest", manifest, "--output", str(output), "--resolved", str(resolved))
            self.assertEqual(0, result.returncode, result.stderr)
            return output.read_text(encoding="utf-8"), json.loads(resolved.read_text(encoding="utf-8"))

    def test_every_base_has_a_complete_adapter(self) -> None:
        for base in sorted(p for p in (ROOT / "bases").iterdir() if p.is_dir() and not p.name.startswith("_")):
            with self.subTest(base=base.name):
                env = render_manifest.load_env(base / "base.env")
                for key in ("BASE_ID", "APT_MIRROR", "SECURITY_MIRROR", "COMPONENTS", "KEYRING", "SUPPORTED_SUITES"):
                    self.assertIn(key, env)
                self.assertEqual(base.name, env["BASE_ID"])
                template = (base / "sources.tmpl").read_text(encoding="utf-8")
                for placeholder in ("${APT_MIRROR}", "${SECURITY_MIRROR}", "${SUITE}", "${COMPONENTS}", "${KEYRING}"):
                    self.assertIn(placeholder, template)
                pkg_map = render_manifest.load_package_map(base / "packages.map")
                for name in ("translations", "office", "directory", "usbguard", "openscap"):
                    self.assertIn(name, pkg_map)

    def test_debian_manifest_renders_debian_sources_and_packages(self) -> None:
        text, resolved = self._render(str(ROOT / "manifests" / "debian-workstation.yml"))
        self.assertIn('export BASE_ID="debian"', text)
        self.assertIn('export APT_COMPONENTS="main contrib non-free non-free-firmware"', text)
        self.assertIn("firefox-esr-l10n-fr", text)
        self.assertNotIn("language-pack-", text)
        self.assertIn("firmware-iwlwifi", resolved["packages"]["install"])

    def test_ubuntu_profile_packages_respect_the_image_policy(self) -> None:
        sys.path.insert(0, str(ROOT / "tests"))
        from assertions.install import FORBIDDEN_IMAGE_PACKAGES  # type: ignore

        _, resolved = self._render(str(ROOT / "manifest.yml"))
        clash = set(resolved["packages"]["install"]) & set(FORBIDDEN_IMAGE_PACKAGES)
        self.assertEqual(set(), clash)

    def test_engine_scripts_do_not_hardcode_a_distribution(self) -> None:
        build = (ROOT / "build.sh").read_text(encoding="utf-8")
        self.assertNotIn("archive.ubuntu.com", build)
        self.assertNotIn("ubuntu-archive-keyring", build)
        self.assertIn("bases/$BASE_ID/sources.tmpl", build)
        self.assertNotIn("TARGET_UBUNTU_VERSION", build)
        mod = (ROOT / "mods" / "06-profile-software" / "install.sh").read_text(encoding="utf-8")
        self.assertIn("PROFILE_INSTALL_PACKAGES", mod)
        self.assertIn("PROFILE_REMOVE_PACKAGES", mod)
        self.assertNotIn("ubuntu", mod.lower())
        self.assertNotIn("debian", mod.lower())


class StackTests(unittest.TestCase):
    """Every role of the stack is filled by a SynOS package or a base archive package."""

    def test_default_manifest_renders_the_synos_stack(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "args.sh"
            result = _run("--output", str(output), "--resolved", str(Path(directory) / "r.json"))
            self.assertEqual(0, result.returncode, result.stderr)
            text = output.read_text(encoding="utf-8")
        desktop_line = text.split('export STACK_DESKTOP_PACKAGES="', 1)[1].split('"', 1)[0].split()
        for name in ("synos-desktop", "synos-desktop-apps", "synos-gnome-extensions", "synos-theme", "synos-wallpapers", "synos-fonts", "synos-session",
                     "synos-appstore", "synos-no-snapd"):
            self.assertIn(name, desktop_line)
        self.assertIn('export STACK_APT_PACKAGES="synos-apt-config synos-archive-keyring base-files"', text)
        self.assertIn('export STACK_DESKTOP_EXCLUDE="build-essential"', text)
        self.assertIn('export STACK_VMWARE_ONLY_ARCH="amd64"', text)
        for name in ("UPSTREAM_PKG_NS", "APKG_SERVER", "STACK_TAKEOVER", "UPSTREAM_REPO_NEEDED", "APT_CONFIG_PACKAGE"):
            self.assertNotIn(name, text)

    def test_takeover_is_accepted_and_ignored(self) -> None:
        for takeover in ("all", ["apt-config"], []):   # older bundles still carry it
            with tempfile.TemporaryDirectory() as directory:
                manifest = Path(directory) / "m.yml"
                manifest.write_text(json.dumps({
                    "schema_version": 1, "name": "m", "version": "1.0.0", "base": "ubuntu", "suite": "resolute",
                    "arch": "amd64", "profile": "minimal", "regions": ["fr"], "brand": "synos",
                    "packages": {"takeover": takeover},
                }), encoding="utf-8")
                result = _run("--manifest", str(manifest), "--check")
                self.assertEqual(0, result.returncode, result.stderr)

    def test_every_takeover_target_builds_into_the_local_repository(self) -> None:
        import yaml
        stack = yaml.safe_load((ROOT / "packages/stack.yml").read_text(encoding="utf-8"))
        targets = [e["package"] for g in stack["groups"].values() for e in g["packages"]
                   if (ROOT / "packages" / e["package"]).is_dir()]
        built = {p.name for p in (ROOT / "packages").iterdir() if (p / "control").is_file()}
        self.assertTrue(set(targets) <= built, set(targets) - built)
        with tempfile.TemporaryDirectory() as directory:
            # plain packages only: prebuilds and forks need network and toolchains
            env = dict(os.environ, SYNOS_KEYS_DIR=str(Path(directory) / "keys"), SYNOS_SKIP_PREBUILD="1")
            result = subprocess.run(
                (sys.executable, str(ROOT / "tools/build_packages.py"), "--output", str(Path(directory) / "repo")),
                text=True, capture_output=True, check=False, cwd=ROOT, env=env)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("generated a new repository signing key", result.stderr)
            repo = Path(directory) / "repo"
            self.assertTrue((repo / "InRelease").is_file())
            self.assertEqual("yes\n", (repo / "SIGNED").read_text(encoding="utf-8"))
            self.assertTrue((Path(directory) / "keys/public/synos-archive-keyring.gpg").stat().st_size > 0)
            # a second run reuses the key instead of generating another
            again = subprocess.run(
                (sys.executable, str(ROOT / "tools/build_packages.py"), "--output", str(Path(directory) / "repo")),
                text=True, capture_output=True, check=False, cwd=ROOT, env=env)
            self.assertNotIn("generated", again.stderr)
            packages = (repo / "Packages").read_text(encoding="utf-8")
            plain = [t for t in targets if not (ROOT / "packages" / t / "prebuild.sh").is_file() and not (ROOT / "packages" / t / "fork.json").is_file()]
            for target in plain:
                self.assertIn(f"Package: {target}\n", packages)
            skipped = (repo / "SKIPPED").read_text(encoding="utf-8")
            for target in set(targets) - set(plain):
                self.assertIn(target, skipped)
            self.assertTrue((repo / "Release").is_file())
            self.assertNotIn("anduinos", packages)
            self.assertNotIn("X-Vendored-From", packages)
            desktop_stanza = packages.split("Package: synos-desktop\n", 1)[1].split("\n\n", 1)[0]
            self.assertIn("Provides: yaru-theme-gnome-shell", desktop_stanza)
            self.assertIn("Conflicts: yaru-theme-gnome-shell", desktop_stanza)
            # the brand kit rides along when make brand ran before make packages
            if (ROOT / ".build/branding/synos").is_dir():
                self.assertIn("Package: synos-branding\n", packages)
            sources = subprocess.run(("dpkg-deb", "--fsys-tarfile", str(next(repo.glob("synos-apt-config_*.deb")))),
                                     check=True, capture_output=True).stdout
            self.assertIn(b"Signed-By: /usr/share/keyrings/synos-archive-keyring.gpg", sources)
            self.assertIn(b"Enabled: no", sources)

    def test_build_serves_the_local_repository_and_strips_it_from_the_image(self) -> None:
        build = (ROOT / "build.sh").read_text(encoding="utf-8")
        self.assertIn("URIs: file:///root/repo", build)
        self.assertIn("new_building_os/root/repo", build)
        cleanup = (ROOT / "mods/85-cleanup-mod/install.sh").read_text(encoding="utf-8")
        self.assertIn("rm -f /etc/apt/sources.list.d/synos-local.sources", cleanup)
        for mod in ("01-install-swap-packages-mod", "05-live-kernel-apps-installer"):
            text = (ROOT / "mods" / mod / "install.sh").read_text(encoding="utf-8")
            self.assertIn("install_stack_group", text)
            self.assertNotIn("anduinos", text)


class RepositoryParameterTests(unittest.TestCase):
    """packages.repository is the one parameter that points installed systems at a server."""

    def _apt_config(self, repository: str) -> tuple[str, str]:
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "m.yml"
            manifest.write_text(json.dumps({
                "schema_version": 1, "name": "m", "version": "3.0.0", "base": "ubuntu", "suite": "resolute",
                "arch": "amd64", "profile": "minimal", "regions": ["fr"], "brand": "synos",
                "packages": {"repository": repository},
            }), encoding="utf-8")
            result = subprocess.run(
                (sys.executable, str(ROOT / "tools/build_packages.py"), "--manifest", str(manifest),
                 "--only", "synos-apt-config", "--output", str(Path(directory) / "repo")),
                text=True, capture_output=True, check=False, cwd=ROOT,
                env=dict(os.environ, SYNOS_KEYS_DIR=str(Path(directory) / "keys")))
            self.assertEqual(0, result.returncode, result.stderr)
            deb = next((Path(directory) / "repo").glob("synos-apt-config_*.deb"))
            tar = subprocess.run(("dpkg-deb", "--fsys-tarfile", str(deb)), check=True, capture_output=True).stdout
            sources = subprocess.run(("tar", "-xO", "./etc/apt/sources.list.d/synos.sources"), input=tar, check=True, capture_output=True).stdout.decode()
            pin = subprocess.run(("tar", "-xO", "./etc/apt/preferences.d/synos-repository"), input=tar, check=True, capture_output=True).stdout.decode()
            return sources, pin

    def test_repository_url_enables_the_source_per_suite(self) -> None:
        sources, pin = self._apt_config("http://192.0.2.10:8080")
        self.assertIn("URIs: http://192.0.2.10:8080/resolute/\n", sources)
        self.assertIn("Suites: ./\n", sources)
        self.assertIn("Enabled: yes\n", sources)
        self.assertIn("Pin: origin 192.0.2.10\n", pin)

    def test_no_repository_ships_the_source_disabled(self) -> None:
        sources, _ = self._apt_config("")
        self.assertIn("Enabled: no\n", sources)
        self.assertIn("Signed-By: /usr/share/keyrings/synos-archive-keyring.gpg\n", sources)

    def test_build_checks_the_repository_when_configured(self) -> None:
        mod = (ROOT / "mods/01-install-swap-packages-mod/install.sh").read_text(encoding="utf-8")
        self.assertIn('if [ -n "${SYNOS_REPO_URL:-}" ]', mod)
        self.assertIn("apt-get update -o Dir::Etc::SourceList=/etc/apt/sources.list.d/synos.sources", mod)
        makefile = (ROOT / "makefile").read_text(encoding="utf-8")
        self.assertIn("serve-repo:", makefile)
        self.assertIn("publish-repo:", makefile)


class PackageMetadataTests(unittest.TestCase):
    """Package metadata and maintainer scripts name only SynOS."""

    def test_packages_are_self_named(self) -> None:
        for source in sorted(p for p in (ROOT / "packages").iterdir() if (p / "control").is_file()):
            control = (source / "control").read_text(encoding="utf-8")
            self.assertIn(f"Package: {source.name}\n", control)
            self.assertNotIn("X-Vendored-From", control, source.name)
            self.assertNotIn("anduinos", control.lower(), source.name)
            for field in ("Provides", "Conflicts", "Replaces"):
                for line in control.splitlines():
                    if line.startswith(field + ":"):
                        self.assertNotIn(source.name, [i.strip() for i in line.split(":", 1)[1].split(",")], f"{source.name} {field} names itself")
            for script in (source / "scripts").glob("*") if (source / "scripts").is_dir() else ():
                text = script.read_text(encoding="utf-8")
                self.assertNotIn("anduinos", text, script)
                self.assertTrue(text.startswith("#!"), script)


class PortedStackTests(unittest.TestCase):
    """Every product role has a SynOS package; build-time kinds are declared, not checked in."""

    def test_every_product_role_is_ported(self) -> None:
        import yaml
        stack = yaml.safe_load((ROOT / "packages/stack.yml").read_text(encoding="utf-8"))
        for group in stack["groups"].values():
            for entry in group["packages"]:
                self.assertNotIn("upstream", entry, entry["role"])
                package = entry["package"]
                if (ROOT / "packages" / package).is_dir():
                    self.assertTrue((ROOT / "packages" / package / "control").is_file(), package)

    def test_no_control_depends_on_an_upstream_name(self) -> None:
        for control in (ROOT / "packages").glob("*/control"):
            text = control.read_text(encoding="utf-8")
            for field in ("Depends", "Recommends", "Suggests", "Pre-Depends"):
                for line in text.splitlines():
                    if line.startswith(field + ":"):
                        self.assertNotRegex(line, r"(^|[ ,|])anduinos-", f"{control.parent.name}: {line}")

    def test_build_time_material_is_recipes_not_artefacts(self) -> None:
        for source in (ROOT / "packages").iterdir():
            if not (source / "control").is_file():
                continue
            if (source / "prebuild.sh").is_file():
                self.assertTrue((source / "upstream").is_dir(), source.name)
                self.assertTrue((source / "lib").is_symlink(), source.name)
                text = (source / "prebuild.sh").read_text(encoding="utf-8")
                self.assertIn('cd "$(dirname "$0")/upstream"', text)
                for artefact in ("deploy", "obj", "target"):
                    path = source / "upstream" / artefact
                    if path.exists():
                        ignored = subprocess.run(("git", "check-ignore", "-q", str(path)), cwd=ROOT, check=False).returncode == 0
                        self.assertTrue(ignored, f"{source.name}/upstream/{artefact} must be git-ignored")
            if (source / "fork.json").is_file():
                spec = json.loads((source / "fork.json").read_text(encoding="utf-8"))
                self.assertIn(spec["distro"], ("ubuntu", "debian", "mozilla", "google"))
                self.assertTrue(spec["package"])
        self.assertTrue((ROOT / "packages/_lib/build-guards.sh").is_file())

    def test_full_takeover_renders_only_synos_names_in_the_stack(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "args.sh"
            result = _run("--output", str(output), "--resolved", str(Path(directory) / "r.json"))
            self.assertEqual(0, result.returncode, result.stderr)
            text = output.read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.startswith("export STACK_") and line.endswith('"') and "_PACKAGES=" in line:
                self.assertNotIn("anduinos", line, line)


class NamespaceTests(unittest.TestCase):
    """Nothing that ships, builds or documents names the former upstream; only the
    copyright, author and translator credits inside incorporated sources may."""

    CREDIT = re.compile(r"copyright|translator|language-team|authors\s*=|@[A-Za-z]|Upstream-Name|Requested-By", re.I)
    SKIP_PARTS = {".git", ".build", "new_building_os", "dist", "image", "logs", "deploy", "obj", "target", "__pycache__", "node_modules"}
    SKIP_SUFFIXES = {".png", ".jpg", ".webp", ".gz", ".xz", ".zst", ".mo", ".pf2", ".deb", ".ico", ".woff", ".woff2", ".ttf", ".otf", ".gresource", ".zip", ".tar"}

    def test_the_former_name_survives_only_in_credits(self) -> None:
        token = re.compile(r"anduin", re.I)
        leftovers = []
        for path in ROOT.rglob("*"):
            if not path.is_file() or path.is_symlink() or path.suffix.lower() in self.SKIP_SUFFIXES:
                continue
            rel = path.relative_to(ROOT)
            if any(part in self.SKIP_PARTS for part in rel.parts) or rel.parts[0] == "tests" or path.name.lower() == "copyright":
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if token.search(line) and not self.CREDIT.search(line):
                    leftovers.append(f"{rel}:{number}: {line.strip()[:80]}")
        self.assertEqual([], leftovers[:20], f"{len(leftovers)} line(s) name the former upstream outside credits")

    def test_engine_uses_the_synos_kernel_parameters(self) -> None:
        build = (ROOT / "build.sh").read_text(encoding="utf-8")
        self.assertIn("rd.synos.live=1", build)
        self.assertIn("rd.synos.keyboard=", build)
        self.assertNotIn("UPSTREAM", build)
        self.assertNotIn("add_upstream_apt_source", build)


class DebianBaseTests(unittest.TestCase):
    """Per-base dependency lists and base-following forks make the Debian build possible."""

    def test_base_fields_select_the_right_list(self) -> None:
        spec = importlib.util.spec_from_file_location("build_packages", ROOT / "tools" / "build_packages.py")
        tool = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(tool)
        control = "Package: x\nDepends: a, ubuntu-only\nDepends[debian]: a, debian-only\nRecommends[debian]: r\nDescription: d\n x\n"
        debian = tool.apply_base_fields(control, "debian")
        ubuntu = tool.apply_base_fields(control, "ubuntu")
        self.assertIn("Depends: a, debian-only\n", debian)
        self.assertIn("Recommends: r\n", debian)
        self.assertNotIn("[", debian)
        self.assertIn("Depends: a, ubuntu-only\n", ubuntu)
        self.assertNotIn("Recommends", ubuntu)
        self.assertTrue(ubuntu.rstrip().endswith(" x"))

    def test_metapackages_have_debian_lists_without_ubuntu_only_packages(self) -> None:
        for name, forbidden in (("synos-core-system", ("linux-generic", "systemd-hwe-hwdb")),
                                ("synos-desktop-core", ("policykit-desktop-privileges", "ubuntu-session"))):
            control = (ROOT / "packages" / name / "control").read_text(encoding="utf-8")
            debian = next(l for l in control.splitlines() if l.startswith("Depends[debian]:"))
            for token in forbidden:
                self.assertNotIn(token, debian, name)

    def test_forks_and_extension_recipes_follow_the_base(self) -> None:
        for name in ("plymouth-synos", "firmware-sof-synos", "synos-software-properties-common"):
            spec = json.loads((ROOT / "packages" / name / "fork.json").read_text(encoding="utf-8"))
            self.assertIn("debian", spec.get("by_base", {}), name)
        for inc in (ROOT / "packages").glob("*/includes.txt"):
            self.assertNotIn("deploy/resolute/", inc.read_text(encoding="utf-8"), inc.parent.name)
        versions = (ROOT / "packages/_lib/gnome-versions.sh").read_text(encoding="utf-8")
        self.assertIn("[trixie]=48", versions)


class DistributionNameOverrideTests(unittest.TestCase):
    """A corporate brand kit renames the distribution: the displayed product name
    follows brand display_name while the synos namespace stays fixed."""

    @classmethod
    def setUpClass(cls) -> None:
        spec = importlib.util.spec_from_file_location("build_packages", ROOT / "tools" / "build_packages.py")
        cls.tool = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(cls.tool)
        cls.subs = {"BRAND_NAME": "Acme Workstation"}

    def test_display_text_is_rebranded_but_the_namespace_is_not(self) -> None:
        text = ('Name=SynOS Installer\nExec=synos-installer --brand SynOS\n'
                'class SynOSBtrfsSnapshotsManager: pass\nrd.synos.live com.synos.Panel /usr/share/synos\n'
                'msgstr "SynOS-Installer (Entwicklung)"\nSynOS\'s desktop\n')
        out = self.tool.brand_text(text, self.subs)
        self.assertIn("Name=Acme Workstation Installer", out)
        self.assertIn("--brand Acme Workstation\n", out)
        self.assertIn("Acme Workstation-Installer (Entwicklung)", out)
        self.assertIn("Acme Workstation's desktop", out)
        self.assertIn("SynOSBtrfsSnapshotsManager", out)
        self.assertIn("synos-installer", out)
        self.assertIn("rd.synos.live com.synos.Panel /usr/share/synos", out)

    def test_keep_list_and_default_brand_are_untouched(self) -> None:
        text = '"User-Agent": "SynOS-Installer/2", title="SynOS"'
        out = self.tool.brand_text(text, self.subs)
        self.assertIn('"SynOS-Installer/2"', out)
        self.assertIn('title="Acme Workstation"', out)
        self.assertEqual(self.tool.brand_text(text, {"BRAND_NAME": "SynOS"}), text)

    def test_gettext_literals_are_escaped(self) -> None:
        out = self.tool.brand_text('msgstr "SynOS"', {"BRAND_NAME": "Acme «Pro»"}, po=True)
        self.assertEqual(out, 'msgstr "Acme «Pro»"')
        with self.assertRaises(self.tool.PackageError):
            self.tool.check_display_name('Acme "Pro"')
        with self.assertRaises(self.tool.PackageError):
            self.tool.check_display_name("   ")

    def test_staged_tree_pass_covers_text_and_translations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pkg = Path(tmp)
            (pkg / "usr/share/applications").mkdir(parents=True)
            (pkg / "usr/share/applications/synos-installer.desktop").write_text(
                "[Desktop Entry]\nName=SynOS Installer\nExec=synos-installer\n", encoding="utf-8")
            (pkg / "usr/bin").mkdir(parents=True)
            binary = pkg / "usr/bin/synos-tool"
            binary.write_bytes(b"\x7fELF\x00SynOS\x00")
            (pkg / "usr/share/locale/de/LC_MESSAGES").mkdir(parents=True)
            mo = pkg / "usr/share/locale/de/LC_MESSAGES/synos-installer.mo"
            have_gettext = bool(shutil.which("msgfmt") and shutil.which("msgunfmt"))
            if have_gettext:
                po = 'msgid ""\nmsgstr "Content-Type: text/plain; charset=UTF-8\\n"\n\nmsgid "SynOS Installer"\nmsgstr "SynOS-Installer"\n'
                subprocess.run(["msgfmt", "-o", str(mo), "-"], input=po.encode(), check=True)
            log: list[str] = []
            changed = self.tool.brand_tree(pkg, self.subs, log)
            self.assertIn("Name=Acme Workstation Installer",
                          (pkg / "usr/share/applications/synos-installer.desktop").read_text(encoding="utf-8"))
            self.assertEqual(binary.read_bytes(), b"\x7fELF\x00SynOS\x00", "binaries are never patched")
            if have_gettext:
                self.assertEqual(changed, 2)
                dumped = subprocess.run(["msgunfmt", str(mo)], capture_output=True, check=True).stdout.decode()
                self.assertIn('msgstr "Acme Workstation-Installer"', dumped)
            else:
                self.assertEqual(changed, 1)
            self.assertEqual(self.tool.brand_tree(pkg, {"BRAND_NAME": "SynOS"}, log), 0)

    def test_installer_filesystem_labels_use_the_namespace(self) -> None:
        src = ROOT / "packages/synos-installer-beta/upstream/src/installer_core"
        commands = (src / "storage_commands.py").read_text(encoding="utf-8")
        self.assertIn('"synos-swap"', commands)
        self.assertNotIn('"SynOS-swap"', commands)
        self.assertIn('"-L", "synos"', commands)
        for name in ("storage_graph_planning.py", "manual_graph_planning.py", "storage_write_set.py", "manual_write_set.py"):
            text = (src / name).read_text(encoding="utf-8")
            self.assertNotIn('label="SynOS"', text, name)
            self.assertNotIn('("label", "SynOS")', text, name)
