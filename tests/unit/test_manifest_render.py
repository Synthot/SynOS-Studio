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


class UnavailablePackageTests(unittest.TestCase):
    """profiles/bundles.yml + bases/*/packages.map: `abstract = unavailable:
    <reason>` marks a role this base's archive genuinely has nothing for
    (not merely a different name — an actual gap, e.g. bases/ubuntu/
    packages.map's browser-headless). A profile that reaches that name on
    that base must fail loudly, naming the profile, the package and the
    reason, rather than silently resolving one package short."""

    def test_load_package_map_parses_the_unavailable_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "packages.map"
            path.write_text("real = a b c\nunreal = unavailable: nothing fills this role here\nempty =\n", encoding="utf-8")
            pkg_map = render_manifest.load_package_map(path)
            self.assertEqual(["a", "b", "c"], pkg_map["real"])
            self.assertEqual([], pkg_map["empty"])
            self.assertIsInstance(pkg_map["unreal"], render_manifest.Unavailable)
            self.assertEqual("nothing fills this role here", pkg_map["unreal"].reason)

    def test_resolving_an_unavailable_name_refuses_with_the_reason(self) -> None:
        pkg_map = {"widget": render_manifest.Unavailable("no such widget on this base")}
        with self.assertRaises(render_manifest.ManifestError) as raised:
            render_manifest.resolve_packages(["widget"], pkg_map, "amd64", ["en"], "acme", "some-profile")
        message = str(raised.exception)
        self.assertIn("some-profile", message)
        self.assertIn("widget", message)
        self.assertIn("acme", message)
        self.assertIn("no such widget on this base", message)

    def test_an_unavailable_name_never_reached_by_a_profile_causes_no_error(self) -> None:
        pkg_map = {"widget": render_manifest.Unavailable("no such widget on this base"), "gadget": ["gadget-pkg"]}
        concrete, unmapped = render_manifest.resolve_packages(["gadget"], pkg_map, "amd64", ["en"], "acme")
        self.assertEqual(["gadget-pkg"], concrete)
        self.assertEqual([], unmapped)

    def test_removing_an_unavailable_name_is_nothing_to_remove_not_a_refusal(self) -> None:
        """A profile's software.packages.remove list is resolved through the same
        map: a role this base never had is simply nothing to remove there, so it
        must not refuse the build the way *needing* it does."""
        pkg_map = {"widget": render_manifest.Unavailable("no such widget on this base"), "gadget": ["gadget-pkg"]}
        concrete, _ = render_manifest.resolve_packages(["widget", "gadget"], pkg_map, "amd64", ["en"], "acme",
                                                       "some-profile", refuse_unavailable=False)
        self.assertEqual(["gadget-pkg"], concrete)
        with self.assertRaises(render_manifest.ManifestError):
            render_manifest.resolve_packages(["widget"], pkg_map, "amd64", ["en"], "acme", "some-profile")

    def test_the_real_ubuntu_browser_headless_gap_refuses_a_render(self) -> None:
        """The concrete case this mechanism exists for: a manifest that pins
        ubuntu and a profile reaching profiles/bundles.yml's "test-engine"
        group must refuse --check rather than silently build one package
        short."""
        with tempfile.TemporaryDirectory() as directory:
            profile_path = ROOT / "profiles" / "test-engine-ubuntu-check.yml"
            profile_path.write_text("id: test-engine-ubuntu-check\nextends: minimal\nsoftware:\n  bundles: [test-engine]\n",
                                     encoding="utf-8")
            self.addCleanup(profile_path.unlink)
            manifest = Path(directory) / "m.yml"
            manifest.write_text(json.dumps({
                "schema_version": 1, "name": "m", "version": "1.0.0", "base": "ubuntu", "suite": "resolute",
                "arch": "amd64", "profile": "test-engine-ubuntu-check", "regions": ["fr"], "brand": "synos",
            }), encoding="utf-8")
            result = _run("--manifest", str(manifest), "--check")
            self.assertEqual(1, result.returncode, result.stdout)
            self.assertIn("manifest error", result.stderr)
            self.assertIn("test-engine-ubuntu-check", result.stderr)
            self.assertIn("browser-headless", result.stderr)
            self.assertIn("unavailable", result.stderr)


class ApplianceLiveGatingTests(unittest.TestCase):
    """resolved["packages"]["appliance"] (docs/ARCHITECTURE.md, "Live session
    vs. installed system"): software.packages.add, resolved concretely
    across the whole extends chain — the boundary
    synos.workstation.live_service_gating uses to decide which units a live
    boot (rd.synos.live=1) must never start. Not a curated group: no group
    in profiles/bundles.yml ships a server (printing resolves to cups,
    containers to podman — workstation conveniences, not appliance
    services), so a profile that only adds those must see them installed
    but never gated. Derived from the profile's own data, not a
    hand-written list, so these tests render real profiles
    (profiles/developer.yml: bundles [containers, build-tools] inherited
    from workstation's [office, communication, remote-access, printing,
    vpn], packages.add [git, python3-venv] on top of workstation's own
    [keepassxc]) rather than inventing a fixture."""

    def _render(self, profile_id: str, base: str = "ubuntu", suite: str = "resolute") -> dict:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "args.sh"
            resolved = Path(directory) / "resolved.json"
            manifest = Path(directory) / "m.yml"
            manifest.write_text(json.dumps({
                "schema_version": 1, "name": "m", "version": "1.0.0", "base": base, "suite": suite,
                "arch": "amd64", "profile": profile_id, "regions": ["us"], "brand": "synos",
            }), encoding="utf-8")
            result = _run("--manifest", str(manifest), "--output", str(output), "--resolved", str(resolved))
            self.assertEqual(0, result.returncode, result.stderr)
            return json.loads(resolved.read_text(encoding="utf-8"))

    def test_appliance_packages_excludes_curated_groups_like_printing_and_containers(self) -> None:
        resolved = self._render("developer")
        appliance = set(resolved["packages"]["appliance"])
        install = set(resolved["packages"]["install"])
        # printing (inherited from workstation) resolves to cups; containers
        # (developer's own bundle) resolves to podman. Both must be
        # installed — every one of developer's bundles genuinely runs — but
        # neither is an appliance service, so live_service_gating must never
        # be told to gate them: someone trying the live desktop keeps
        # printing.
        self.assertIn("cups", install)
        self.assertNotIn("cups", appliance)
        self.assertIn("podman", install)
        self.assertNotIn("podman", appliance)

    def test_appliance_packages_is_exactly_packages_add_across_the_whole_chain(self) -> None:
        resolved = self._render("developer")
        appliance = set(resolved["packages"]["appliance"])
        # git, python3-venv: developer's own software.packages.add.
        # keepassxc: workstation's (developer's parent) own add — deep_merge
        # concatenates "add" lists across extends, so this must still show
        # up without developer repeating it.
        self.assertEqual({"git", "python3-venv", "keepassxc"}, appliance)

    def test_kubernetes_appliance_packages_survive_the_narrowing(self) -> None:
        """bundle-catalog/kubernetes-server: software.packages.add:
        [containerd, runc] on top of server's own [ssh-server] — real
        daemons a package installs and dpkg enables by policy, exactly what
        this mechanism exists to keep out of the live session. Copied into
        profiles/ for the duration of this test the way tools/synos bundle
        apply would place it in a real build, and removed after."""
        target = ROOT / "profiles" / "kubernetes-server.yml"
        self.addCleanup(target.unlink, missing_ok=True)
        target.write_text((ROOT / "bundle-catalog/kubernetes-server/profiles/kubernetes-server.yml").read_text(encoding="utf-8"),
                          encoding="utf-8")
        resolved = self._render("kubernetes-server", suite="noble")
        appliance = set(resolved["packages"]["appliance"])
        self.assertEqual({"containerd", "runc", "openssh-server"}, appliance)

    def test_ansible_is_required_for_appliance_packages_alone(self) -> None:
        """profiles/developer.yml triggers none of ansible_required's other
        conditions (no policy, no compliance, no open_ports, no
        software.services, no hardware.gpu, no software.files) — before
        appliance_packages was added to that check, a build host without
        ansible-playbook installed would silently skip live_service_gating
        for this profile instead of refusing the way every other
        Ansible-needing profile already does."""
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "args.sh"
            resolved_path = Path(directory) / "resolved.json"
            manifest = Path(directory) / "m.yml"
            manifest.write_text(json.dumps({
                "schema_version": 1, "name": "m", "version": "1.0.0", "base": "ubuntu", "suite": "resolute",
                "arch": "amd64", "profile": "developer", "regions": ["us"], "brand": "synos",
            }), encoding="utf-8")
            result = _run("--manifest", str(manifest), "--output", str(output), "--resolved", str(resolved_path))
            self.assertEqual(0, result.returncode, result.stderr)
            text = output.read_text(encoding="utf-8")
            self.assertIn('export ANSIBLE_CHROOT_REQUIRED="true"', text)

    def test_ansible_is_not_required_for_a_profile_with_no_add_and_no_services(self) -> None:
        """profiles/minimal.yml: bundles: [desktop-core] only, no
        packages.add, no policy, no compliance, no open_ports, no
        software.services, no hardware.gpu, no software.files — the
        narrowing must restore this to False (its behavior before this
        mechanism existed) rather than newly requiring Ansible for every
        profile just because it now has *some* concrete install list."""
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "args.sh"
            resolved_path = Path(directory) / "resolved.json"
            manifest = Path(directory) / "m.yml"
            manifest.write_text(json.dumps({
                "schema_version": 1, "name": "m", "version": "1.0.0", "base": "ubuntu", "suite": "resolute",
                "arch": "amd64", "profile": "minimal", "regions": ["us"], "brand": "synos",
            }), encoding="utf-8")
            result = _run("--manifest", str(manifest), "--output", str(output), "--resolved", str(resolved_path))
            self.assertEqual(0, result.returncode, result.stderr)
            resolved = json.loads(resolved_path.read_text(encoding="utf-8"))
            self.assertEqual([], resolved["packages"]["appliance"])
            text = output.read_text(encoding="utf-8")
            self.assertIn('export ANSIBLE_CHROOT_REQUIRED="false"', text)

    def test_ansible_vars_json_carries_the_appliance_package_list(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "args.sh"
            resolved_path = Path(directory) / "resolved.json"
            manifest = Path(directory) / "m.yml"
            manifest.write_text(json.dumps({
                "schema_version": 1, "name": "m", "version": "1.0.0", "base": "ubuntu", "suite": "resolute",
                "arch": "amd64", "profile": "developer", "regions": ["us"], "brand": "synos",
            }), encoding="utf-8")
            result = _run("--manifest", str(manifest), "--output", str(output), "--resolved", str(resolved_path))
            self.assertEqual(0, result.returncode, result.stderr)
            ansible_vars = json.loads((resolved_path.with_name("ansible-vars.json")).read_text(encoding="utf-8"))
            self.assertIn("synos_appliance_packages", ansible_vars)
            self.assertIn("git", ansible_vars["synos_appliance_packages"])
            self.assertNotIn("podman", ansible_vars["synos_appliance_packages"])
            self.assertNotIn("cups", ansible_vars["synos_appliance_packages"])


class LanguagePackageMapTests(unittest.TestCase):
    """bases/<base>/language-packages.map (tools/generate_language_packages.py):
    the real, archive-verified package for one `${LANG}` template and one
    language, consulted by resolve_packages()'s `lang_pkg_map` parameter in
    place of naively substituting the language code into the template — the
    fix for hunspell-de not being a package (the real name is
    hunspell-de-de), libreoffice-l10n-en never existing (English is
    LibreOffice's own source language), and the like."""

    def test_load_language_package_map_parses_concrete_and_unavailable_entries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "language-packages.map"
            path.write_text(
                "hunspell-${LANG}/de = hunspell-de-de\n"
                "hunspell-${LANG}/fi = voikko-fi libenchant-2-voikko\n"
                "hunspell-${LANG}/ja = unavailable: no dictionary in either archive\n"
                "language-pack-${LANG}/ga@noble = language-pack-ga\n"
                "language-pack-${LANG}/ga@resolute = unavailable: not built for this suite\n",
                encoding="utf-8")
            table = render_manifest.load_language_package_map(path)
            self.assertEqual(["hunspell-de-de"], table["hunspell-${LANG}"]["de"])
            self.assertEqual(["voikko-fi", "libenchant-2-voikko"], table["hunspell-${LANG}"]["fi"])
            self.assertIsInstance(table["hunspell-${LANG}"]["ja"], render_manifest.Unavailable)
            self.assertEqual("no dictionary in either archive", table["hunspell-${LANG}"]["ja"].reason)
            self.assertEqual(["language-pack-ga"], table["language-pack-${LANG}"]["ga@noble"])
            self.assertIsInstance(table["language-pack-${LANG}"]["ga@resolute"], render_manifest.Unavailable)

    def test_resolve_packages_uses_the_language_map_instead_of_naive_substitution(self) -> None:
        pkg_map = {"spellcheck": ["hunspell-${LANG}"]}
        lang_pkg_map = {"hunspell-${LANG}": {"de": ["hunspell-de-de"], "fr": ["hunspell-fr"]}}
        concrete, _ = render_manifest.resolve_packages(["spellcheck"], pkg_map, "amd64", ["de", "fr"], "acme",
                                                        lang_pkg_map=lang_pkg_map)
        self.assertEqual(["hunspell-de-de", "hunspell-fr"], concrete)

    def test_a_language_the_map_marks_unavailable_is_dropped_not_added_and_recorded_as_a_gap(self) -> None:
        pkg_map = {"office-l10n": ["libreoffice-l10n-${LANG}"]}
        lang_pkg_map = {"libreoffice-l10n-${LANG}": {
            "en": render_manifest.Unavailable("English is LibreOffice's own source language"),
            "de": ["libreoffice-l10n-de"],
        }}
        gaps: list[dict] = []
        concrete, _ = render_manifest.resolve_packages(["office-l10n"], pkg_map, "amd64", ["en", "de"], "acme",
                                                        lang_pkg_map=lang_pkg_map, lang_gaps=gaps)
        self.assertEqual(["libreoffice-l10n-de"], concrete)
        self.assertEqual([{"template": "libreoffice-l10n-${LANG}", "lang": "en",
                          "reason": "English is LibreOffice's own source language"}], gaps)

    def test_a_suite_specific_override_takes_precedence_over_the_bare_language_entry(self) -> None:
        pkg_map = {"translations": ["language-pack-${LANG}"]}
        lang_pkg_map = {"language-pack-${LANG}": {
            "ga": ["language-pack-ga"],
            "ga@resolute": render_manifest.Unavailable("not built for this suite"),
        }}
        concrete, _ = render_manifest.resolve_packages(["translations"], pkg_map, "amd64", ["ga"], "ubuntu",
                                                        suite="noble", lang_pkg_map=lang_pkg_map)
        self.assertEqual(["language-pack-ga"], concrete)
        concrete, _ = render_manifest.resolve_packages(["translations"], pkg_map, "amd64", ["ga"], "ubuntu",
                                                        suite="resolute", lang_pkg_map=lang_pkg_map)
        self.assertEqual([], concrete)

    def test_a_language_the_table_does_not_cover_at_all_refuses_loudly(self) -> None:
        """The table covers this template (it has other languages in it) but not
        this one — a stale table (a region added a language after the table was
        last generated), not a language with nothing to offer. That must refuse,
        not silently fall back to a guess."""
        pkg_map = {"spellcheck": ["hunspell-${LANG}"]}
        lang_pkg_map = {"hunspell-${LANG}": {"de": ["hunspell-de-de"]}}
        with self.assertRaises(render_manifest.ManifestError) as raised:
            render_manifest.resolve_packages(["spellcheck"], pkg_map, "amd64", ["xx"], "acme", lang_pkg_map=lang_pkg_map)
        self.assertIn("xx", str(raised.exception))
        self.assertIn("language-packages.map", str(raised.exception))

    def test_a_template_the_map_does_not_cover_falls_back_to_naive_substitution(self) -> None:
        """lang_pkg_map covers some templates, not this one: unaffected, exactly
        the behaviour before this file existed at all — a partially-generated
        table, or one for a different base's own templates, must not break every
        other ${LANG} role."""
        pkg_map = {"other-role": ["something-${LANG}"]}
        concrete, _ = render_manifest.resolve_packages(["other-role"], pkg_map, "amd64", ["en", "fr"], "acme",
                                                        lang_pkg_map={"hunspell-${LANG}": {"en": ["hunspell-en-us"]}})
        self.assertEqual(["something-en", "something-fr"], concrete)

    def test_no_language_map_at_all_is_the_same_as_before_this_file_existed(self) -> None:
        pkg_map = {"spellcheck": ["hunspell-${LANG}"]}
        concrete, _ = render_manifest.resolve_packages(["spellcheck"], pkg_map, "amd64", ["de"], "acme")
        self.assertEqual(["hunspell-de"], concrete)


class LiveSuiteTests(unittest.TestCase):
    """bases/*/live.map: a suite base.env's SUPPORTED_SUITES accepts but whose
    own archive cannot back a Live image (mods/stack.sh's
    ensure_dracut_live_modules) must refuse at render/--check time, before a
    chroot ever runs — the same `unavailable: <reason>` idiom packages.map
    uses, keyed by suite (render_manifest.load_live_suite_map)."""

    def test_load_live_suite_map_parses_the_unavailable_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "live.map"
            path.write_text("stale = unavailable: no live module on this suite\n# a comment\n\n", encoding="utf-8")
            live_map = render_manifest.load_live_suite_map(path)
            self.assertIsInstance(live_map["stale"], render_manifest.Unavailable)
            self.assertEqual("no live module on this suite", live_map["stale"].reason)

    def test_a_missing_live_map_is_no_limitation_at_all(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual({}, render_manifest.load_live_suite_map(Path(directory) / "does-not-exist.map"))

    def test_an_entry_that_is_not_the_unavailable_marker_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "live.map"
            path.write_text("stale = noble\n", encoding="utf-8")
            with self.assertRaises(render_manifest.ManifestError):
                render_manifest.load_live_suite_map(path)

    def test_live_capable_suites_excludes_what_live_map_marks_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base_dir = Path(directory)
            (base_dir / "live.map").write_text("stale = unavailable: no live module\n", encoding="utf-8")
            suites = render_manifest.live_capable_suites(base_dir, {"SUPPORTED_SUITES": "stale fresh"})
            self.assertEqual(["fresh"], suites)

    def test_live_capable_suites_with_no_live_map_keeps_every_supported_suite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            suites = render_manifest.live_capable_suites(Path(directory), {"SUPPORTED_SUITES": "a b c"})
            self.assertEqual(["a", "b", "c"], suites)

    def test_the_real_ubuntu_jammy_gap_refuses_a_render_naming_base_suite_and_alternative(self) -> None:
        """The concrete case bases/ubuntu/live.map exists for: jammy's own
        dracut cannot back a Live image (packages/README.md, checked against
        the real archive), and a manifest naming it must refuse before a
        chroot ever runs, not 30+ minutes into one."""
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "m.yml"
            manifest.write_text(json.dumps({
                "schema_version": 1, "name": "m", "version": "1.0.0", "base": "ubuntu", "suite": "jammy",
                "arch": "amd64", "profile": "minimal", "regions": ["fr"], "brand": "synos",
            }), encoding="utf-8")
            result = _run("--manifest", str(manifest), "--check")
            self.assertEqual(1, result.returncode, result.stdout)
            message = result.stderr
            self.assertIn("manifest error", message)
            self.assertIn("jammy", message)
            self.assertIn("ubuntu", message)
            self.assertIn("Live image", message)
            self.assertIn("dmsquash-live-autooverlay", message)
            self.assertIn("overlayfs", message)
            self.assertIn("noble", message)

    def test_every_other_supported_ubuntu_and_debian_suite_still_renders(self) -> None:
        """Only the known gap is refused; every other suite in SUPPORTED_SUITES
        (base.env) renders exactly as before this file existed."""
        for base, suite in (("ubuntu", "noble"), ("ubuntu", "resolute"), ("debian", "trixie")):
            with tempfile.TemporaryDirectory() as directory:
                manifest = Path(directory) / "m.yml"
                manifest.write_text(json.dumps({
                    "schema_version": 1, "name": "m", "version": "1.0.0", "base": base, "suite": suite,
                    "arch": "amd64", "profile": "minimal", "regions": ["fr"], "brand": "synos",
                }), encoding="utf-8")
                result = _run("--manifest", str(manifest), "--check")
                self.assertEqual(0, result.returncode, msg=(base, suite, result.stderr))

    def test_a_live_map_entry_absent_from_supported_suites_is_rejected_as_drift(self) -> None:
        """live.map must never silently disagree with SUPPORTED_SUITES: a live.map
        naming a suite base.env does not list is a repository bug, refused
        rather than ignored."""
        base = {"SUPPORTED_SUITES": "fresh", "DEFAULT_SUITE": "fresh"}
        live_map = {"stale": render_manifest.Unavailable("not in SUPPORTED_SUITES at all")}
        with self.assertRaises(render_manifest.ManifestError) as raised:
            render_manifest.check_live_map_agrees_with_supported_suites("acme", base, live_map)
        message = str(raised.exception)
        self.assertIn("stale", message)
        self.assertIn("SUPPORTED_SUITES", message)

    def test_a_default_suite_live_map_marks_unavailable_is_rejected(self) -> None:
        """A base's own DEFAULT_SUITE can never be one live.map marks
        unavailable — that would mean the base's default cannot even build."""
        base = {"SUPPORTED_SUITES": "fresh", "DEFAULT_SUITE": "fresh"}
        live_map = {"fresh": render_manifest.Unavailable("no live module on this suite")}
        with self.assertRaises(render_manifest.ManifestError) as raised:
            render_manifest.check_live_map_agrees_with_supported_suites("acme", base, live_map)
        message = str(raised.exception)
        self.assertIn("DEFAULT_SUITE", message)
        self.assertIn("no live module on this suite", message)

    def test_agreeing_live_map_raises_nothing(self) -> None:
        base = {"SUPPORTED_SUITES": "fresh stale", "DEFAULT_SUITE": "fresh"}
        live_map = {"stale": render_manifest.Unavailable("no live module on this suite")}
        render_manifest.check_live_map_agrees_with_supported_suites("acme", base, live_map)  # must not raise

    def test_every_real_base_live_map_agrees_with_supported_suites(self) -> None:
        """Static, offline guard for the same invariant: every suite key a real
        base's live.map names must be one of that base's own SUPPORTED_SUITES,
        so the two can never drift apart in this checkout."""
        for base_dir in sorted(p for p in (ROOT / "bases").iterdir() if p.is_dir() and not p.name.startswith("_")):
            with self.subTest(base=base_dir.name):
                env = render_manifest.load_env(base_dir / "base.env")
                live_map = render_manifest.load_live_suite_map(base_dir / "live.map")
                supported = set(env.get("SUPPORTED_SUITES", "").split())
                self.assertTrue(set(live_map) <= supported, set(live_map) - supported)
                self.assertNotIn(env.get("DEFAULT_SUITE"), live_map,
                                  f"{base_dir.name}'s own DEFAULT_SUITE cannot back a Live image")


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
                self.assertIn(spec["distro"], ("ubuntu", "debian", "mozilla"))
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
