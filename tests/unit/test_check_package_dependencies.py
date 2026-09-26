"""tools/check_package_dependencies.py's pure parsing and its end-to-end
check_lock(), against synthetic packages/bases fixtures (never this
checkout's own real ~70 packages/*/control files) so a real repo edit can
never make this test's own expectations drift.

The motivating defect: an alternative dependency (`pkgA | pkgB`) where a
build's own package selection ends up with neither side actually installed
-- exactly what happened, transiently, when a Conflicts/Provides package
swap left a build log with an alarming-looking (but ultimately
self-resolving) dpkg warning for firmware-sof-synos | firmware-sof-signed.
This suite proves the check would fail loudly if that swap ever did NOT
resolve, and passes cleanly when it does.
"""
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load_module(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


check_deps = load_module("check_package_dependencies_under_test", "tools/check_package_dependencies.py")


class PureParsingTests(unittest.TestCase):
    def test_parse_lock_reads_name_equals_version_lines(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "x.packages.lock"
            lock_path.write_text("apt=3.0.3\nfirmware-sof-signed=2025.01-1\n\n", encoding="utf-8")
            self.assertEqual({"apt": "3.0.3", "firmware-sof-signed": "2025.01-1"},
                             check_deps.parse_lock(lock_path))

    def test_strip_constraint_removes_a_version_clause(self) -> None:
        self.assertEqual("apt", check_deps.strip_constraint("apt (>= 1.5~alpha4)"))
        self.assertEqual("bare-name", check_deps.strip_constraint("bare-name"))

    def test_resolved_json_path_shares_the_locks_own_stem(self) -> None:
        lock = Path("/dist/SynOS-1.0.0-debian-trixie-amd64.packages.lock")
        self.assertEqual(Path("/dist/SynOS-1.0.0-debian-trixie-amd64.resolved.json"),
                         check_deps.resolved_json_path(lock))

    def test_dependency_clauses_splits_commas_then_pipes(self) -> None:
        control = (
            "Package: synos-desktop-core\n"
            "Depends: synos-core-system, firmware-sof-synos | firmware-sof-signed\n"
            "Pre-Depends: dpkg (>= 1.19)\n"
        )
        clauses = check_deps.dependency_clauses(control)
        self.assertIn(("Depends", ["synos-core-system"]), clauses)
        self.assertIn(("Depends", ["firmware-sof-synos", "firmware-sof-signed"]), clauses)
        self.assertIn(("Pre-Depends", ["dpkg"]), clauses)

    def test_collect_provides_gathers_virtual_names_by_real_package(self) -> None:
        provides = check_deps.collect_provides({
            "firmware-sof-synos": "Package: firmware-sof-synos\nProvides: firmware-sof-signed\n",
            "unrelated": "Package: unrelated\n",
        })
        self.assertEqual({"firmware-sof-signed": {"firmware-sof-synos"}}, provides)


class CheckLockFixtureTests(unittest.TestCase):
    """check_lock() against a synthetic packages/bases tree (this test's own
    ROOT monkeypatch), never this repo's real packages/*."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_root = Path(self._tmp.name)
        (self.tmp_root / "bases" / "testbase").mkdir(parents=True)
        (self.tmp_root / "bases" / "testbase" / "base.env").write_text(
            "BASE_ID=testbase\nAPT_MIRROR=http://example.invalid/\nDEFAULT_SUITE=testsuite\n",
            encoding="utf-8")
        (self.tmp_root / "packages").mkdir()
        self._original_root = check_deps.ROOT
        check_deps.ROOT = self.tmp_root

    def tearDown(self) -> None:
        check_deps.ROOT = self._original_root
        self._tmp.cleanup()

    def _write_control(self, name: str, body: str) -> None:
        package_dir = self.tmp_root / "packages" / name
        package_dir.mkdir(parents=True)
        (package_dir / "control").write_text(f"Package: {name}\n{body}", encoding="utf-8")

    def _write_lock(self, entries: dict[str, str]) -> Path:
        lock_path = self.tmp_root / "x.packages.lock"
        lock_path.write_text("".join(f"{n}={v}\n" for n, v in entries.items()), encoding="utf-8")
        return lock_path

    def test_an_alternative_satisfied_by_the_other_side_passes(self) -> None:
        """The firmware-sof-synos | firmware-sof-signed shape, satisfied by
        the side that is actually locked -- the real, verified outcome of
        the build this check was written against."""
        self._write_control("synos-desktop-core", "Depends: firmware-sof-synos | firmware-sof-signed\n")
        lock = self._write_lock({"firmware-sof-signed": "2025.01-1", "synos-desktop-core": "2.0.2-1"})
        result = check_deps.check_lock(lock, base_override="testbase", suite_override="testsuite")
        self.assertEqual("passed", result["status"])
        self.assertEqual([], result["failures"])

    def test_an_alternative_satisfied_only_via_provides_passes(self) -> None:
        self._write_control("synos-desktop-core", "Depends: firmware-sof-synos | firmware-sof-signed\n")
        self._write_control("firmware-sof-synos", "Provides: firmware-sof-signed\n")
        lock = self._write_lock({"firmware-sof-synos": "2.0.2-1", "synos-desktop-core": "2.0.2-1"})
        result = check_deps.check_lock(lock, base_override="testbase", suite_override="testsuite")
        self.assertEqual("passed", result["status"])

    def test_an_alternative_with_neither_side_locked_fails(self) -> None:
        """The exact class of bug this check exists for: neither
        alternative ends up installed."""
        self._write_control("synos-desktop-core", "Depends: firmware-sof-synos | firmware-sof-signed\n")
        lock = self._write_lock({"synos-desktop-core": "2.0.2-1"})
        result = check_deps.check_lock(lock, base_override="testbase", suite_override="testsuite")
        self.assertEqual("failed", result["status"])
        self.assertEqual(1, len(result["failures"]))
        self.assertEqual("synos-desktop-core", result["failures"][0]["package"])
        self.assertEqual(["firmware-sof-synos", "firmware-sof-signed"], result["failures"][0]["alternatives"])

    def test_a_bare_hard_depend_missing_from_the_lock_fails(self) -> None:
        """The real gap this check found in the owner's own build: a hard
        (non-alternative) Depends on a package that never got installed."""
        self._write_control("synos-desktop-core", "Depends: apt-transport-https\n")
        lock = self._write_lock({"synos-desktop-core": "2.0.2-1"})
        result = check_deps.check_lock(lock, base_override="testbase", suite_override="testsuite")
        self.assertEqual("failed", result["status"])
        self.assertEqual(["apt-transport-https"], result["failures"][0]["alternatives"])

    def test_a_package_not_actually_installed_is_never_checked(self) -> None:
        """Only packages that ended up in the lock are checked -- a
        meta-package this profile never pulled in must not be flagged for
        Depends this image never needed to satisfy."""
        self._write_control("synos-never-installed", "Depends: something-nonexistent\n")
        lock = self._write_lock({"unrelated-package": "1.0"})
        result = check_deps.check_lock(lock, base_override="testbase", suite_override="testsuite")
        self.assertEqual("passed", result["status"])
        self.assertNotIn("synos-never-installed", result["checked_packages"])

    def test_a_base_specific_override_replaces_the_bare_field(self) -> None:
        """Depends[testbase]: replaces the bare Depends: for that base,
        mirroring tools/build_packages.py's own apply_base_fields()."""
        self._write_control(
            "synos-desktop-core",
            "Depends: something-only-on-other-bases\nDepends[testbase]: apt-transport-https\n")
        lock = self._write_lock({"synos-desktop-core": "2.0.2-1", "apt-transport-https": "3.0.3"})
        result = check_deps.check_lock(lock, base_override="testbase", suite_override="testsuite")
        self.assertEqual("passed", result["status"])

    def test_missing_lock_file_is_skipped_not_failed(self) -> None:
        result = check_deps.check_lock(self.tmp_root / "does-not-exist.packages.lock",
                                       base_override="testbase", suite_override="testsuite")
        self.assertEqual("skipped", result["status"])

    def test_no_base_or_resolved_json_is_skipped_not_failed(self) -> None:
        lock = self._write_lock({"anything": "1.0"})
        result = check_deps.check_lock(lock)
        self.assertEqual("skipped", result["status"])

    def test_a_resolved_json_next_to_the_lock_supplies_the_base_without_flags(self) -> None:
        self._write_control("synos-desktop-core", "Depends: apt-transport-https\n")
        lock = self._write_lock({"synos-desktop-core": "2.0.2-1", "apt-transport-https": "3.0.3"})
        resolved = check_deps.resolved_json_path(lock)
        resolved.write_text(json.dumps({
            "manifest": {"version": "1.0.0", "suite": "testsuite", "arch": "amd64"},
            "base": {"BASE_ID": "testbase", "APT_MIRROR": "http://example.invalid/"},
        }), encoding="utf-8")
        result = check_deps.check_lock(lock)  # no --base/--suite given at all
        self.assertEqual("passed", result["status"])
        self.assertEqual("testbase", result["base"])


class MainCLITests(unittest.TestCase):
    def test_main_returns_nonzero_when_any_lock_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tmp_root = Path(directory)
            (tmp_root / "packages").mkdir()
            (tmp_root / "bases" / "testbase").mkdir(parents=True)
            (tmp_root / "bases" / "testbase" / "base.env").write_text(
                "BASE_ID=testbase\nAPT_MIRROR=http://example.invalid/\n", encoding="utf-8")
            lock = tmp_root / "x.packages.lock"
            lock.write_text("synos-x=1.0\n", encoding="utf-8")
            (tmp_root / "packages" / "synos-x").mkdir()
            (tmp_root / "packages" / "synos-x" / "control").write_text(
                "Package: synos-x\nDepends: missing-thing\n", encoding="utf-8")
            original_root = check_deps.ROOT
            check_deps.ROOT = tmp_root
            try:
                rc = check_deps.main([str(lock), "--base", "testbase", "--suite", "testsuite"])
            finally:
                check_deps.ROOT = original_root
        self.assertEqual(1, rc)


if __name__ == "__main__":
    unittest.main()
