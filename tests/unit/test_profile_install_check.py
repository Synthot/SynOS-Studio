"""mods/06-profile-software/install.sh's resolve_installable_profile_packages:
one batched `apt-get install -s -y` simulate answers "can every candidate
install together" far more cheaply than asking apt about each name in its
own process (measured on a real archive: ~8s for 39 packages together vs
~109s asking about each of the 39 individually — every apt invocation
repays the same archive-index load the previous one already paid for). A
failed batch falls back to the original name-by-name simulate loop, which
is the only way to say *which* candidate is the problem, since a single
"apt install pkg-a pkg-b" transaction aborts entirely the instant one name
cannot be resolved and never says which one.

These tests extract the real function from the mod's install.sh (not a
hand-copied restatement of it) and run it for real against fake
apt-get/dpkg executables, covering the three paths that matter: every
candidate resolves (fast path only, one apt-get call), one candidate does
not (fallback fires and names exactly that one), and every candidate fails
(fallback fires for all of them, none aborts the build)."""
from __future__ import annotations

import re
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INSTALL_SH = ROOT / "mods/06-profile-software/install.sh"


def _write_executable(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/bash\n{body}", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _extract_function() -> str:
    text = INSTALL_SH.read_text(encoding="utf-8")
    match = re.search(
        r"^resolve_installable_profile_packages\(\) \{\n.*?\n\}\n",
        text, re.DOTALL | re.MULTILINE,
    )
    assert match, "resolve_installable_profile_packages() not found in mods/06-profile-software/install.sh"
    return match.group(0)


class _Harness:
    """A temp dir with a fake apt-get on PATH that succeeds for a batched
    or single call only when every named package is in its "good" list,
    plus the real resolve_installable_profile_packages function sourced
    from the mod's actual install.sh."""

    def __init__(self, tmp: Path, good_packages: list[str]) -> None:
        self.tmp = tmp
        self.bin = tmp / "bin"
        self.bin.mkdir()
        self.log = tmp / "apt-get.log"
        good_list = tmp / "good-packages.txt"
        good_list.write_text("\n".join(good_packages) + "\n", encoding="utf-8")
        # Real apt aborts an entire "install a b c" transaction the moment
        # one name cannot be resolved -- model exactly that, not a partial
        # success, so a regression that assumed apt reports per-package
        # results inside a batch call shows up here too.
        _write_executable(self.bin / "apt-get", textwrap.dedent(f"""\
            echo "apt-get $*" >> "{self.log}"
            shift 3  # install -s -y
            for pkg in "$@"; do
                grep -qxF "$pkg" "{good_list}" || exit 1
            done
            exit 0
            """))

    def run_function(self, *candidates: str) -> subprocess.CompletedProcess[str]:
        script = (
            "INSTALL=()\nSKIPPED=()\n"
            + _extract_function()
            + "resolve_installable_profile_packages " + " ".join(candidates) + "\n"
            "printf 'INSTALL=%s\\n' \"${INSTALL[*]:-}\"\n"
            "printf 'SKIPPED=%s\\n' \"${SKIPPED[*]:-}\"\n"
        )
        env = {"PATH": f"{self.bin}:/usr/bin:/bin"}
        return subprocess.run(
            ["bash", "-c", script], text=True, capture_output=True, env=env,
        )

    def call_count(self) -> int:
        if not self.log.is_file():
            return 0
        return len(self.log.read_text(encoding="utf-8").splitlines())


class AllGoodFastPathTests(unittest.TestCase):
    def test_every_candidate_resolves_with_a_single_apt_get_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            harness = _Harness(Path(tmp), good_packages=["alpha", "beta", "gamma"])
            result = harness.run_function("alpha", "beta", "gamma")
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("INSTALL=alpha beta gamma", result.stdout)
            self.assertIn("SKIPPED=\n", result.stdout)
            self.assertEqual(1, harness.call_count(),
                              "the fast path must resolve every good candidate in one apt-get call")

    def test_an_empty_candidate_list_touches_apt_not_at_all(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            harness = _Harness(Path(tmp), good_packages=[])
            result = harness.run_function()
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(0, harness.call_count())


class OneBadFallbackTests(unittest.TestCase):
    def test_one_unresolvable_candidate_falls_back_and_names_only_that_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            harness = _Harness(Path(tmp), good_packages=["alpha", "gamma"])
            result = harness.run_function("alpha", "bogus-package", "gamma")
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("INSTALL=alpha gamma", result.stdout)
            self.assertIn("SKIPPED=bogus-package", result.stdout)
            # one failed batch call, then one call per candidate in the fallback
            self.assertEqual(4, harness.call_count(),
                              "fallback must be the batch call plus one call per candidate, not fewer")

    def test_the_build_is_not_aborted_by_the_one_bad_candidate(self) -> None:
        """The whole point of the fallback: a single unresolvable name must
        not take the good candidates down with it."""
        with tempfile.TemporaryDirectory() as tmp:
            harness = _Harness(Path(tmp), good_packages=["alpha", "gamma"])
            result = harness.run_function("alpha", "bogus-package", "gamma")
            self.assertEqual(0, result.returncode)
            install = next(line for line in result.stdout.splitlines() if line.startswith("INSTALL="))
            self.assertEqual({"alpha", "gamma"}, set(install.removeprefix("INSTALL=").split()))


class EverythingBadTests(unittest.TestCase):
    def test_every_candidate_unresolvable_skips_all_of_them_without_aborting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            harness = _Harness(Path(tmp), good_packages=[])
            result = harness.run_function("bogus-one", "bogus-two", "bogus-three")
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("INSTALL=\n", result.stdout)
            skipped = next(line for line in result.stdout.splitlines() if line.startswith("SKIPPED="))
            self.assertEqual({"bogus-one", "bogus-two", "bogus-three"},
                              set(skipped.removeprefix("SKIPPED=").split()))
            # one failed batch call, then one call per candidate in the fallback -- none skipped
            self.assertEqual(4, harness.call_count())


if __name__ == "__main__":
    unittest.main()
