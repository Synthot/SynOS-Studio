"""The builder images' Rust and GCC toolchains: pinned once, verified, and
--strict still fails the build on an unbuildable package. Nothing here
compiles anything; make packages inside a built image is the real proof
(see docs/BUNDLE.md and the package recipes under packages/*/prebuild.sh)."""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASES = ("ubuntu", "debian", "_template")
VERSION_FILE = ROOT / "bases" / "rust-toolchain.txt"


class RustToolchainPinTests(unittest.TestCase):
    def test_version_is_recorded_in_exactly_one_file(self) -> None:
        self.assertTrue(VERSION_FILE.is_file(), VERSION_FILE)
        version = VERSION_FILE.read_text(encoding="utf-8").strip()
        self.assertRegex(version, r"^\d+\.\d+\.\d+$", version)
        # No Containerfile hardcodes that version string itself; each one only
        # names the file and reads it at build time, so a bump is one edit.
        for base in BASES:
            text = (ROOT / "bases" / base / "Containerfile").read_text(encoding="utf-8")
            self.assertNotIn(version, text, f"{base}/Containerfile hardcodes the pinned Rust version")

    def test_pinned_rust_is_new_enough_for_edition_2024_and_lockfile_v4(self) -> None:
        # Cargo.lock format 4 needs Cargo >= 1.78 (readable) and edition 2024
        # (synos-btrfs-snapshots-manager) needs rustc >= 1.85: the pin must
        # clear both, whatever number it is bumped to later.
        major, minor, _ = (int(part) for part in VERSION_FILE.read_text(encoding="utf-8").strip().split("."))
        self.assertGreaterEqual((major, minor), (1, 85))

    def test_every_base_copies_and_reads_the_shared_version_file(self) -> None:
        for base in BASES:
            containerfile = ROOT / "bases" / base / "Containerfile"
            text = containerfile.read_text(encoding="utf-8")
            self.assertIn("COPY bases/rust-toolchain.txt", text, base)
            self.assertIn('cat /tmp/rust-toolchain.txt', text, base)

    def test_every_base_installs_the_pinned_toolchain_with_rustup(self) -> None:
        for base in BASES:
            text = (ROOT / "bases" / base / "Containerfile").read_text(encoding="utf-8")
            self.assertIn("rustup toolchain install", text, base)
            self.assertIn("--no-self-update", text, base)
            self.assertIn('rustup default "$version"', text, base)
            self.assertIn("rustup", re.findall(r"apt-get install.*?(?=RUN|\Z)", text, re.S)[0], base)

    def test_ubuntu_no_longer_takes_its_rust_from_the_suite_archive(self) -> None:
        text = (ROOT / "bases" / "ubuntu" / "Containerfile").read_text(encoding="utf-8")
        install_block = re.search(r"apt-get install.*?\\\n(?:.*\\\n)*.*", text).group(0)
        self.assertNotRegex(install_block, r"(?<![-\w])cargo(?![-\w])")
        self.assertNotRegex(install_block, r"(?<![-\w])rustc(?![-\w])")
        self.assertIn("build-essential", install_block)

    def test_containerfiles_still_install_no_other_undocumented_toolchain_source(self) -> None:
        # _template mirrors debian's layout (see docs/BUNDLE.md): a future base
        # copied from it inherits the same pinned rustup block, not "stable".
        debian = (ROOT / "bases" / "debian" / "Containerfile").read_text(encoding="utf-8")
        template = (ROOT / "bases" / "_template" / "Containerfile").read_text(encoding="utf-8")
        self.assertNotIn("rustup toolchain install stable", debian)
        self.assertNotIn("rustup toolchain install stable", template)


class WhisperWorkerToolchainTests(unittest.TestCase):
    SCRIPT = ROOT / "packages/synos-whisper-worker/upstream/scripts/build-state-metrics.sh"

    def test_script_is_well_formed_shell(self) -> None:
        result = subprocess.run(("bash", "-n", str(self.SCRIPT)), capture_output=True, text=True, check=False)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_every_fetched_deb_is_checksummed(self) -> None:
        text = self.SCRIPT.read_text(encoding="utf-8")
        calls = re.findall(r"fetch_deb (\S+) \\\s*\n\s*([0-9a-f]{64})", text)
        # amd64: gcc-15 driver, g++-15 frontend, libgcc-15-dev, libstdc++-15-dev,
        # libggml0, binutils-x86-64-linux-gnu, libbinutils, libsframe1 (8);
        # arm64 cross: the same minus the separate libbinutils (its libbfd
        # ships inside the cross binutils package itself) (7).
        self.assertEqual(15, len(calls), calls)
        for path, digest in calls:
            self.assertTrue(path.endswith(".deb"), path)
            self.assertEqual(64, len(digest))

    def test_gcc15_driver_is_fetched_and_pinned_not_assumed_from_the_host(self) -> None:
        text = self.SCRIPT.read_text(encoding="utf-8")
        self.assertIn("gcc-15-x86-64-linux-gnu_", text)
        self.assertIn("gcc-15-aarch64-linux-gnu_", text)
        self.assertIn("libgcc-15-dev_", text)
        self.assertIn("libgcc-15-dev-arm64-cross_", text)
        self.assertIn('driver="$toolchain/usr/bin/$triple-gcc-15"', text)
        # the old recipe required the *host's* plain "gcc"/cross-gcc to already
        # report version 15; that dependency on the base image is gone.
        self.assertNotIn("driver=gcc\n", text)
        self.assertNotIn("driver=aarch64-linux-gnu-gcc\n", text)
        self.assertIn('test -x "$driver"', text)
        # GCC 15 emits assembly a suite's own (older) binutils cannot read
        # (a ".base64" pseudo-op); that assembler is pinned too, and PATH is
        # how it actually gets used (GCC's -B search does not override the
        # assembler path it was configured with).
        self.assertIn("binutils-x86-64-linux-gnu_", text)
        self.assertIn("binutils-aarch64-linux-gnu_", text)
        self.assertIn('PATH="$as_dir:$PATH"', text)


class StrictPackageBuildTests(unittest.TestCase):
    """--strict must keep failing the build when a package cannot be built
    (SYNOS_SKIP_PREBUILD makes every prebuild.sh skip immediately, standing
    in here for a real toolchain gap without needing one)."""

    def test_strict_fails_when_any_package_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ, SYNOS_KEYS_DIR=str(Path(directory) / "keys"), SYNOS_SKIP_PREBUILD="1")
            result = subprocess.run(
                (sys.executable, str(ROOT / "tools/build_packages.py"), "--strict",
                 "--output", str(Path(directory) / "repo")),
                text=True, capture_output=True, check=False, cwd=ROOT, env=env)
            self.assertEqual(1, result.returncode, result.stderr)
            self.assertIn("could not be built (--strict)", result.stderr)

    def test_without_strict_the_same_skip_still_only_warns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ, SYNOS_KEYS_DIR=str(Path(directory) / "keys"), SYNOS_SKIP_PREBUILD="1")
            result = subprocess.run(
                (sys.executable, str(ROOT / "tools/build_packages.py"),
                 "--output", str(Path(directory) / "repo")),
                text=True, capture_output=True, check=False, cwd=ROOT, env=env)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("skipped on this host", result.stdout)


if __name__ == "__main__":
    unittest.main()
