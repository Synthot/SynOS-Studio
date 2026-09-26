"""build.sh's own guard against shipping a stale local package repository.

tools/build_packages.py's recipe_fingerprint() already hashes a package's
own control file (and everything else under packages/<name>/) and correctly
rebuilds it -- and refreshes .build/repo's own Packages index -- whenever
that content changes (verified directly against the real function). But
none of that runs unless `make packages` is actually invoked again before
`./build.sh`; nothing previously checked that it had been. A
packages/<name>/control edited after the last `make packages` run left
.build/repo (and everything build.sh copies from it into the chroot) out of
step with this checkout, with no error anywhere -- exactly the shape of the
real gap a build of this engine hit: synos-desktop-core's own control names
apt-transport-https as a hard Depends, but the image that shipped never
installed it and the entire build log never mentions the name once.

Two things are tested: that build.sh's mount_folders() actually contains
this guard (a contract test, the same style test_apt_cache_order.py uses for
build.sh's own phase ordering), and that the underlying `find ... -newer`
idiom it uses genuinely tells a stale repository from a fresh one -- a real
functional test of the mechanism, not just its presence in the script.
"""
from __future__ import annotations

import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class BuildShContractTests(unittest.TestCase):
    def test_mount_folders_checks_the_repository_is_not_older_than_packages(self) -> None:
        build = (ROOT / "build.sh").read_text(encoding="utf-8")
        mount_folders = build.split("function mount_folders()")[1].split("\nfunction ")[0]
        self.assertIn("find \"$SCRIPT_DIR/packages\" -type f -newer", mount_folders)
        self.assertIn("run 'make packages' again", mount_folders)
        # The exit must happen before the stale repository is ever copied into the chroot.
        copy_repo = 'cp -r "$SCRIPT_DIR/${LOCAL_REPO_DIR:-.build/repo}" new_building_os/root/repo'
        self.assertIn(copy_repo, mount_folders)
        self.assertLess(mount_folders.index("stale_source="), mount_folders.index(copy_repo))


class StalenessDetectionTests(unittest.TestCase):
    """The real `find -newer` idiom build.sh's guard uses, against a real
    filesystem with controlled mtimes -- not a mock of `find`."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.packages = self.root / "packages"
        self.repo = self.root / "repo"
        (self.packages / "synos-desktop-core").mkdir(parents=True)
        self.repo.mkdir()
        self.control = self.packages / "synos-desktop-core" / "control"
        self.control.write_text("Package: synos-desktop-core\n", encoding="utf-8")
        self.packages_index = self.repo / "Packages"
        self.packages_index.write_text("Package: synos-desktop-core\n", encoding="utf-8")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _stale_source(self) -> str:
        completed = subprocess.run(
            ["bash", "-c", 'find "$1/packages" -type f -newer "$2/Packages" 2>/dev/null | head -n1',
             "--", str(self.root), str(self.repo)],
            capture_output=True, text=True, check=True)
        return completed.stdout.strip()

    def test_a_repository_built_after_every_package_input_is_not_stale(self) -> None:
        # Packages index already newer than control (as build_repository()
        # leaves it after a real `make packages` run) -- nothing to report.
        self.assertEqual("", self._stale_source())

    def test_a_control_file_edited_after_the_last_make_packages_is_caught(self) -> None:
        # Simulate exactly the real gap: someone edits control (e.g. adding
        # apt-transport-https to Depends) after .build/repo/Packages was
        # last written by `make packages`.
        time.sleep(0.01)
        self.control.write_text("Package: synos-desktop-core\nDepends: apt-transport-https\n", encoding="utf-8")
        self.assertEqual(str(self.control), self._stale_source())

    def test_a_brand_new_package_folder_with_no_repo_rebuild_is_also_caught(self) -> None:
        time.sleep(0.01)
        new_pkg = self.packages / "synos-new-thing"
        new_pkg.mkdir()
        (new_pkg / "control").write_text("Package: synos-new-thing\n", encoding="utf-8")
        self.assertEqual(str(new_pkg / "control"), self._stale_source())


if __name__ == "__main__":
    unittest.main()
