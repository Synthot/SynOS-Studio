"""The two conformance units' own privilege posture, which is a decision
rather than an accident and so is worth pinning.

An image build needs real root inside the container runtime (debootstrap,
mknod, the mounts), and tools/bundle_launcher.sh — what actually performs
the build — is written for that: it runs as an ordinary user and escalates
only the runtime. NoNewPrivileges=true blocks sudo outright, so a build
unit that sets it ends every entry at the launcher with "sudo is required
to run podman with root privileges". That was measured on a real server,
for every entry.

User=root on the build unit was the alternative and was rejected: it would
also run the Python runner, a headless Chrome and the unpacking of a
bundle downloaded through a web page as root, to solve a problem only the
runtime has.
"""
from __future__ import annotations

import unittest
from pathlib import Path

UNITS = Path(__file__).resolve().parents[2] / "packaging" / "catalog-conformance"


def _settings(name: str) -> list[str]:
    text = (UNITS / name).read_text(encoding="utf-8")
    return [line for line in text.splitlines() if line and not line.startswith("#")]


class ConformanceUnitPrivilegeTests(unittest.TestCase):
    def test_neither_unit_runs_as_root(self) -> None:
        for unit in ("synos-conformance-build.service", "synos-conformance-check.service"):
            with self.subTest(unit=unit):
                self.assertIn("User=synos-conformance", _settings(unit))
                self.assertNotIn("User=root", _settings(unit))

    def test_check_keeps_no_new_privileges(self) -> None:
        """check needs no container runtime at all, so nothing here has any
        reason to gain privileges."""
        self.assertIn("NoNewPrivileges=true", _settings("synos-conformance-check.service"))

    def test_build_does_not_set_no_new_privileges(self) -> None:
        """...and if this ever comes back, every build dies at the launcher."""
        active = _settings("synos-conformance-build.service")
        self.assertFalse([l for l in active if l.startswith("NoNewPrivileges")], active)

    def test_the_build_unit_explains_why_rather_than_leaving_a_silent_gap(self) -> None:
        """A weakened setting with no reason next to it reads like an
        oversight, and the next person to harden the file will put it
        back."""
        text = (UNITS / "synos-conformance-build.service").read_text(encoding="utf-8")
        self.assertIn("NoNewPrivileges= is deliberately NOT set", text)
        self.assertIn("User=root on this unit. That was rejected", text)
        self.assertIn("root-equivalent in principle", text)

    def test_both_units_keep_the_protections_that_still_apply(self) -> None:
        for unit in ("synos-conformance-build.service", "synos-conformance-check.service"):
            with self.subTest(unit=unit):
                active = _settings(unit)
                self.assertIn("ProtectSystem=strict", active)
                self.assertIn("PrivateTmp=true", active)
                self.assertTrue([l for l in active if l.startswith("ReadWritePaths=")], active)


if __name__ == "__main__":
    unittest.main()
