"""mods/stack.sh: install_stack_group's optional-package handling, and
ensure_dracut_live_modules, the check that catches a missing Live-boot
dracut module in the first minutes of a build instead of 30+ minutes in,
when mod 80 hands the same gap to dracut while writing the initrd.

Which archive package carries the "dmsquash-live" family of dracut modules
is not fixed across suites (dracut-live on jammy/noble/Debian; folded into
dracut-core on Ubuntu resolute) — and unlike a control file's Depends field,
`apt install pkg-a pkg-b` has no alternatives syntax: naming a package that
does not exist on this suite at all aborts the whole install, not just that
one package. These tests run the real shell functions from mods/stack.sh
against fake dracut/apt-get/apt-cache/apt executables, not a container.
"""
from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SHARED_SH = ROOT / "shared.sh"
STACK_SH = ROOT / "mods/stack.sh"


def _write_executable(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/bash\n{body}", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


class _Harness:
    """A temp dir with fake dracut/apt-get/apt-cache/apt on PATH, and shared.sh
    plus mods/stack.sh sourced. `run(body)` executes a bash snippet after
    sourcing both, with the fakes ahead of the real ones on PATH."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.bin = tmp / "bin"
        self.bin.mkdir()
        self.state = tmp / "state"
        self.state.mkdir()
        (self.state / "existing-packages.txt").write_text("", encoding="utf-8")

        _write_executable(self.bin / "dracut", textwrap.dedent(f"""\
            echo "dracut $*" >> "{self.state}/dracut.log"
            if [[ " $* " == *" --list-modules "* ]]; then
                # The real dracut hard-fails listing modules for a kernel
                # this chroot has not installed yet ("Cannot find module
                # directory") unless told not to care which kernel — model
                # that instead of just always answering, so a regression
                # that drops --no-kernel shows up as "nothing found" here too.
                if [[ " $* " != *" --no-kernel "* ]]; then
                    echo "dracut: Cannot find module directory (fake: --no-kernel required)" >&2
                    exit 1
                fi
                if [ -f "{self.state}/dracut-live-installed" ]; then
                    cat "{self.state}/modules-after.txt" 2>/dev/null
                else
                    cat "{self.state}/modules-before.txt" 2>/dev/null
                fi
            fi
            exit 0
            """))
        _write_executable(self.bin / "apt-get", textwrap.dedent(f"""\
            echo "apt-get $*" >> "{self.state}/apt-get.log"
            if [ "$1" = "install" ]; then
                for a in "$@"; do
                    [ "$a" = "dracut-live" ] && touch "{self.state}/dracut-live-installed"
                done
            fi
            exit "${{FAKE_APT_GET_EXIT:-0}}"
            """))
        _write_executable(self.bin / "apt-cache", textwrap.dedent(f"""\
            echo "apt-cache $*" >> "{self.state}/apt-cache.log"
            if [ "$1" = "show" ]; then
                grep -qxF "$2" "{self.state}/existing-packages.txt"
                exit $?
            fi
            exit 0
            """))
        _write_executable(self.bin / "apt", textwrap.dedent(f"""\
            echo "apt $*" >> "{self.state}/apt.log"
            exit "${{FAKE_APT_EXIT:-0}}"
            """))

    def set_modules(self, before: list[str], after: list[str] | None = None) -> None:
        (self.state / "modules-before.txt").write_text("\n".join(before) + "\n", encoding="utf-8")
        (self.state / "modules-after.txt").write_text("\n".join(after or before) + "\n", encoding="utf-8")

    def set_existing_packages(self, names: list[str]) -> None:
        (self.state / "existing-packages.txt").write_text("\n".join(names) + "\n", encoding="utf-8")

    def log(self, name: str) -> str:
        path = self.state / name
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    def run(self, body: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        script = f'source "{SHARED_SH}"\nsource "{STACK_SH}"\n{body}\n'
        full_env = dict(os.environ)
        full_env["PATH"] = f"{self.bin}:{full_env.get('PATH', '')}"
        full_env.setdefault("TARGET_SUITE", "test-suite")
        full_env.setdefault("TARGET_ARCH", "amd64")
        full_env.update(env or {})
        return subprocess.run(
            ["bash", "-c", script], text=True, capture_output=True, env=full_env, cwd=ROOT,
        )


class EnsureDracutLiveModulesTests(unittest.TestCase):
    def test_every_module_already_present_does_not_touch_apt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            harness = _Harness(Path(tmp))
            harness.set_modules(["dmsquash-live", "dmsquash-live-autooverlay", "overlayfs", "synos-live-layers"])
            result = harness.run("ensure_dracut_live_modules")
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("has every Live module it needs", result.stdout)
            self.assertEqual("", harness.log("apt-get.log"))

    def test_missing_dmsquash_live_is_fixed_by_installing_dracut_live(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            harness = _Harness(Path(tmp))
            # dracut-core alone (before dracut-live installs): overlayfs and
            # our own module are there, dmsquash-live is not — the exact
            # shape of the noble failure this check exists for.
            harness.set_modules(
                before=["overlayfs", "synos-live-layers"],
                after=["dmsquash-live", "dmsquash-live-autooverlay", "overlayfs", "synos-live-layers"],
            )
            result = harness.run("ensure_dracut_live_modules")
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("dracut-live", harness.log("apt-get.log"))
            self.assertIn("provided the missing module", result.stdout)

    def test_a_gap_dracut_live_cannot_fill_fails_with_the_missing_module_named(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            harness = _Harness(Path(tmp))
            # synos-live-layers missing is a packaging error, not something
            # dracut-live (an upstream package) could ever provide.
            harness.set_modules(["dmsquash-live", "dmsquash-live-autooverlay", "overlayfs"])
            result = harness.run("ensure_dracut_live_modules")
            self.assertNotEqual(0, result.returncode)
            self.assertIn("synos-live-layers", result.stdout + result.stderr)
            self.assertIn("cannot build", result.stdout + result.stderr)
            # dracut-live provides none of what's missing, so it must not
            # have been reached for
            self.assertEqual("", harness.log("apt-get.log"))

    def test_still_missing_after_installing_dracut_live_fails_loudly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            harness = _Harness(Path(tmp))
            # dracut-live "installs" (apt-get succeeds) but the module set
            # never changes — e.g. the package exists but is the wrong
            # version, or apt silently no-ops. The check must not report
            # success just because the fix was attempted.
            harness.set_modules(before=["overlayfs", "synos-live-layers"])
            result = harness.run("ensure_dracut_live_modules")
            self.assertNotEqual(0, result.returncode)
            self.assertIn("dmsquash-live", result.stdout + result.stderr)
            self.assertIn("still missing", result.stdout + result.stderr)


class OptionalStackPackageTests(unittest.TestCase):
    """install_stack_group's optional-package handling: packages/stack.yml
    marks a package optional when it exists as its own installable package on
    most, but not every, suite of its base."""

    def _env(self, packages: str, optional: str = "") -> dict[str, str]:
        return {
            "STACK_LIVE_PACKAGES": packages,
            "STACK_LIVE_RECOMMENDS": "false",
            "STACK_LIVE_EXCLUDE": "",
            "STACK_LIVE_ONLY_ARCH": "",
            "STACK_LIVE_OPTIONAL": optional,
        }

    def test_an_available_optional_package_is_installed_normally(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            harness = _Harness(Path(tmp))
            harness.set_existing_packages(["dracut-live"])
            result = harness.run(
                "install_stack_group live",
                env=self._env("dracut-core dracut-live", optional="dracut-live"),
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("dracut-live", harness.log("apt.log"))
            self.assertNotIn("dropping", result.stdout)

    def test_an_unavailable_optional_package_is_dropped_not_fatal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            harness = _Harness(Path(tmp))
            harness.set_existing_packages([])  # dracut-install does not exist on this suite
            result = harness.run(
                "install_stack_group live",
                env=self._env("dracut-core dracut-install", optional="dracut-install"),
            )
            self.assertEqual(0, result.returncode, result.stderr)
            apt_log = harness.log("apt.log")
            self.assertIn("dracut-core", apt_log)
            self.assertNotIn("dracut-install", apt_log)
            self.assertIn("dropping it from stack group live", result.stdout)

    def test_a_non_optional_package_is_never_checked_or_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            harness = _Harness(Path(tmp))
            harness.set_existing_packages([])   # would fail apt-cache show, but it's not optional
            result = harness.run("install_stack_group live", env=self._env("dracut-core"))
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("dracut-core", harness.log("apt.log"))
            self.assertEqual("", harness.log("apt-cache.log"))

    def test_every_optional_package_missing_skips_the_group_instead_of_calling_apt_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            harness = _Harness(Path(tmp))
            harness.set_existing_packages([])
            result = harness.run(
                "install_stack_group live", env=self._env("dracut-install", optional="dracut-install"),
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("resolves to no packages", result.stdout)
            self.assertEqual("", harness.log("apt.log"))


class StackYamlOptionalDeclarationTests(unittest.TestCase):
    """packages/stack.yml declares the two known cross-release gaps; the
    render pipeline must carry them through to STACK_LIVE_OPTIONAL."""

    def test_dracut_install_and_dracut_live_are_declared_optional(self) -> None:
        import yaml
        stack = yaml.safe_load((ROOT / "packages/stack.yml").read_text(encoding="utf-8"))
        live = {e["role"]: e for e in stack["groups"]["live"]["packages"]}
        self.assertTrue(live["dracut-install"].get("optional"))
        self.assertTrue(live["dracut-live"].get("optional"))
        self.assertNotIn("only_base", live["dracut-live"], "dracut-live is attempted on every base now; optional carries the suite variance")

    def test_render_manifest_exports_the_optional_list(self) -> None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("render_manifest", ROOT / "tools/render_manifest.py")
        render_manifest = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(render_manifest)
        resolved = render_manifest.resolve_stack({"base": "ubuntu"})
        self.assertIn("dracut-install", resolved["live"]["optional"])
        self.assertIn("dracut-live", resolved["live"]["optional"])
        self.assertIn("dracut-install", resolved["live"]["packages"])
        self.assertIn("dracut-live", resolved["live"]["packages"])


if __name__ == "__main__":
    unittest.main()
