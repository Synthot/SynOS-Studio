"""tools/smoke_test.py's assertions, tested against fixtures (a fake systemd
listing, a fake firewall dump, a fake `stat` line) rather than by booting
anything: no test in this file starts qemu-system-x86_64."""
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


smoke_test = load_module("smoke_test_under_test", "tools/smoke_test.py")


class FakeSession:
    """A fixed table of command -> (output, exit_code), standing in for
    SerialSession.run without a QEMU guest behind it."""

    def __init__(self, table: dict[str, tuple[str, int]]):
        self.table = table
        self.calls: list[str] = []

    def run(self, command: str, timeout: float = 0) -> tuple[str, int]:
        self.calls.append(command)
        for prefix, result in self.table.items():
            if command.startswith(prefix):
                return result
        return "", 127


class CommandMarkerParsingTests(unittest.TestCase):
    def test_recovers_output_between_markers_with_echo_noise(self) -> None:
        transcript = (
            "echo SMOKE-START-abc; systemctl is-active foo; echo SMOKE-END-abc $?\r\n"
            "SMOKE-START-abc\r\n"
            "active\r\n"
            "SMOKE-END-abc 0\r\n"
        )
        body, code = smoke_test.parse_command_output(transcript, "SMOKE-START-abc", "SMOKE-END-abc")
        self.assertEqual("active", body)
        self.assertEqual(0, code)

    def test_nonzero_exit_code_is_recovered(self) -> None:
        transcript = "SMOKE-START-x\r\nSMOKE-END-x 3\r\n"
        body, code = smoke_test.parse_command_output(transcript, "SMOKE-START-x", "SMOKE-END-x")
        self.assertEqual("", body)
        self.assertEqual(3, code)

    def test_missing_end_marker_is_reported_as_failure(self) -> None:
        transcript = "SMOKE-START-x\r\nstill booting...\r\n"
        body, code = smoke_test.parse_command_output(transcript, "SMOKE-START-x", "SMOKE-END-x")
        self.assertEqual(-1, code)

    def test_multiline_output_is_preserved(self) -> None:
        transcript = "SMOKE-START-y\r\nline one\r\nline two\r\nSMOKE-END-y 0\r\n"
        body, code = smoke_test.parse_command_output(transcript, "SMOKE-START-y", "SMOKE-END-y")
        self.assertEqual("line one\r\nline two", body)
        self.assertEqual(0, code)


class FirewallPortParsingTests(unittest.TestCase):
    FAKE_SCRIPT = (
        "#!/bin/sh\n"
        "set -u\n"
        "command -v ufw >/dev/null 2>&1 && ufw allow 22/tcp >/dev/null || true\n"
        "command -v ufw >/dev/null 2>&1 && ufw allow 80/tcp >/dev/null || true\n"
        "command -v ufw >/dev/null 2>&1 && ufw allow 443/tcp >/dev/null || true\n"
        "mkdir -p /var/lib/synos && touch /var/lib/synos/first-boot-services.done\n"
    )

    def test_extracts_every_allowed_port(self) -> None:
        ports = smoke_test.parse_ufw_allow_ports(self.FAKE_SCRIPT)
        self.assertEqual(["22/tcp", "80/tcp", "443/tcp"], ports)

    def test_udp_port_keeps_its_protocol(self) -> None:
        script = "command -v ufw >/dev/null 2>&1 && ufw allow 51820/udp >/dev/null || true\n"
        self.assertEqual(["51820/udp"], smoke_test.parse_ufw_allow_ports(script))

    def test_bare_port_number_defaults_to_tcp(self) -> None:
        self.assertEqual(["9090/tcp"], smoke_test.normalize_ports(["9090"]))

    def test_no_ports_in_an_empty_script(self) -> None:
        self.assertEqual([], smoke_test.parse_ufw_allow_ports("#!/bin/sh\nexit 0\n"))

    def test_check_open_ports_matches_the_profile(self) -> None:
        session = FakeSession({"cat /usr/libexec/synos-first-boot-services": (self.FAKE_SCRIPT, 0)})
        result = smoke_test.check_open_ports(session, ["22/tcp", "80/tcp", "443/tcp"])
        self.assertTrue(result["passed"])

    def test_check_open_ports_flags_a_mismatch(self) -> None:
        session = FakeSession({"cat /usr/libexec/synos-first-boot-services": (self.FAKE_SCRIPT, 0)})
        result = smoke_test.check_open_ports(session, ["22/tcp"])  # profile promised fewer ports than shipped
        self.assertFalse(result["passed"])
        self.assertEqual(["22/tcp"], result["expected"])
        self.assertEqual(["22/tcp", "443/tcp", "80/tcp"], result["found"])

    def test_check_open_ports_with_no_script_and_no_expectation_passes(self) -> None:
        session = FakeSession({"cat /usr/libexec/synos-first-boot-services": ("", 1)})
        result = smoke_test.check_open_ports(session, [])
        self.assertTrue(result["passed"])


class ServiceUnitParsingTests(unittest.TestCase):
    def test_enabled_unit_passes(self) -> None:
        self.assertEqual("enabled", smoke_test.parse_is_enabled("enabled\n", 0))

    def test_static_unit_counts_as_enabled_by_dependency(self) -> None:
        session = FakeSession({"systemctl is-enabled nginx.service": ("static\n", 0)})
        result = smoke_test.check_service_unit(session, "nginx")
        self.assertTrue(result["passed"])
        self.assertEqual("static", result["state"])

    def test_disabled_unit_fails(self) -> None:
        session = FakeSession({"systemctl is-enabled registry.service": ("disabled\n", 1)})
        result = smoke_test.check_service_unit(session, "registry")
        self.assertFalse(result["passed"])

    def test_missing_unit_fails(self) -> None:
        session = FakeSession({"systemctl is-enabled ghost.service": ("", 4)})
        result = smoke_test.check_service_unit(session, "ghost")
        self.assertFalse(result["passed"])
        self.assertEqual("not-found", result["state"])


class ShippedFileParsingTests(unittest.TestCase):
    def test_matching_mode_passes(self) -> None:
        session = FakeSession({"stat -c '%a %s' /srv/www/index.html": ("644 128\n", 0)})
        result = smoke_test.check_shipped_file(session, "/srv/www/index.html", "0644")
        self.assertTrue(result["passed"])
        self.assertEqual(128, result["size"])

    def test_wrong_mode_fails(self) -> None:
        session = FakeSession({"stat -c '%a %s' /etc/registry/htpasswd": ("777 42\n", 0)})
        result = smoke_test.check_shipped_file(session, "/etc/registry/htpasswd", "0644")
        self.assertFalse(result["passed"])

    def test_missing_file_fails(self) -> None:
        session = FakeSession({"stat -c '%a %s' /nowhere": ("", 1)})
        result = smoke_test.check_shipped_file(session, "/nowhere", "0644")
        self.assertFalse(result["passed"])
        self.assertEqual("missing or unreadable", result["reason"])


class DefaultTargetTests(unittest.TestCase):
    def test_reaches_default_target_immediately(self) -> None:
        session = FakeSession({
            "systemctl get-default": ("graphical.target\n", 0),
            "systemctl is-active graphical.target": ("active\n", 0),
        })
        result = smoke_test.wait_default_target(session, timeout=5)
        self.assertTrue(result["passed"])
        self.assertEqual("graphical.target", result["target"])

    def test_never_becomes_active_fails_after_timeout(self) -> None:
        session = FakeSession({
            "systemctl get-default": ("multi-user.target\n", 0),
            "systemctl is-active multi-user.target": ("activating\n", 0),
        })
        result = smoke_test.wait_default_target(session, timeout=0.5)
        self.assertFalse(result["passed"])


class ProfileResolutionTests(unittest.TestCase):
    """Bug: the expectation for open-ports came from a bundle's own leaf
    profile (which declares no ports of its own, inheriting them from the
    catalog "kind" it extends) instead of the resolved configuration the
    engine actually built. These test the two sources directly, against
    this checkout's real profiles/server.yml -> profiles/minimal.yml chain
    (the same chain synosnginx-server -> server -> minimal the bug report
    hit), not a fake stand-in for it."""

    def test_resolved_json_path_shares_the_isos_stem(self) -> None:
        self.assertEqual(
            Path("dist/SynOS-NGINX-1.0.0-amd64.resolved.json"),
            smoke_test.resolved_json_path(Path("dist/SynOS-NGINX-1.0.0-amd64.iso")))

    def test_load_resolved_profile_reads_the_profile_key(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "x.resolved.json"
            path.write_text(json.dumps({
                "profile_chain": ["minimal", "server", "synosnginx-server"],
                "profile": {"security": {"open_ports": ["22/tcp", "9090/tcp"]}},
            }), encoding="utf-8")
            profile = smoke_test.load_resolved_profile(path)
        self.assertEqual(["22/tcp", "9090/tcp"], profile["security"]["open_ports"])

    def test_load_resolved_profile_defaults_to_empty_when_the_key_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "x.resolved.json"
            path.write_text(json.dumps({"manifest": {}}), encoding="utf-8")
            self.assertEqual({}, smoke_test.load_resolved_profile(path))

    def test_resolve_profile_chain_merges_the_leafs_extends_parent(self) -> None:
        """The exact shape of the bundle's own profiles/synosnginx-server.yml:
        extends server (-> minimal), declares no open_ports of its own."""
        with tempfile.TemporaryDirectory() as d:
            leaf = Path(d) / "synosnginx-server.yml"
            leaf.write_text(
                "id: synosnginx-server\n"
                "extends: server\n"
                "software:\n"
                "  services:\n"
                "    - name: nginx\n"
                "      image: docker.io/library/nginx:1.31.2\n",
                encoding="utf-8")
            profile, chain = smoke_test.resolve_profile_chain(leaf)
        self.assertEqual(["minimal", "server", "synosnginx-server"], chain)
        self.assertEqual(["22/tcp", "9090/tcp"], profile["security"]["open_ports"])
        self.assertEqual("nginx", profile["software"]["services"][0]["name"])

    def test_resolve_profile_chain_with_no_extends_returns_just_the_leaf(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            leaf = Path(d) / "standalone.yml"
            leaf.write_text("id: standalone\nsecurity:\n  open_ports: [80/tcp]\n", encoding="utf-8")
            profile, chain = smoke_test.resolve_profile_chain(leaf)
        self.assertEqual(["standalone"], chain)
        self.assertEqual(["80/tcp"], profile["security"]["open_ports"])


class MainProfileSourceTests(unittest.TestCase):
    """main()'s CLI-level source selection: resolved.json (auto-discovered
    next to the ISO, or --resolved) wins over --profile's own extends
    chain, which wins over nothing at all — and every case records which
    source was used, instead of a silent guess."""

    def _capture_run(self, argv: list[str]) -> dict:
        captured: dict = {}

        def fake_run(iso_path, resolved_profile, **kwargs):
            captured["resolved_profile"] = resolved_profile
            captured["profile_source"] = kwargs.get("profile_source")
            return {"status": "skipped", "reason": "test stub"}

        original = smoke_test.run
        smoke_test.run = fake_run
        try:
            smoke_test.main(argv)
        finally:
            smoke_test.run = original
        return captured

    def test_prefers_resolved_json_found_next_to_the_iso(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            work = Path(d)
            iso = work / "image.iso"
            iso.touch()
            (work / "image.resolved.json").write_text(
                json.dumps({"profile": {"security": {"open_ports": ["9090/tcp"]}}}), encoding="utf-8")
            with tempfile.TemporaryDirectory() as outd:
                captured = self._capture_run([str(iso), "--output", outd])
        self.assertEqual(["9090/tcp"], captured["resolved_profile"]["security"]["open_ports"])
        self.assertIn("resolved configuration", captured["profile_source"])
        self.assertIn("image.resolved.json", captured["profile_source"])

    def test_falls_back_to_the_profile_extends_chain_when_no_resolved_json_exists(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            work = Path(d)
            iso = work / "image.iso"
            iso.touch()
            leaf = work / "synosnginx-server.yml"
            leaf.write_text("id: synosnginx-server\nextends: server\n", encoding="utf-8")
            with tempfile.TemporaryDirectory() as outd:
                captured = self._capture_run([str(iso), "--profile", str(leaf), "--output", outd])
        self.assertEqual(["22/tcp", "9090/tcp"], captured["resolved_profile"]["security"]["open_ports"])
        self.assertIn("extends chain resolved", captured["profile_source"])

    def test_no_source_at_all_is_reported_honestly_not_silently_guessed(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            iso = Path(d) / "image.iso"
            iso.touch()
            with tempfile.TemporaryDirectory() as outd:
                captured = self._capture_run([str(iso), "--output", outd])
        self.assertEqual({}, captured["resolved_profile"])
        self.assertIn("no --resolved", captured["profile_source"])
        self.assertIn("no --profile", captured["profile_source"])


class AvailabilityTests(unittest.TestCase):
    def test_run_skips_cleanly_when_qemu_is_missing(self) -> None:
        original = smoke_test.shutil.which
        smoke_test.shutil.which = lambda name: None
        try:
            result = smoke_test.run(Path("/nonexistent.iso"), {}, output_dir=Path("/tmp"))
        finally:
            smoke_test.shutil.which = original
        self.assertEqual("skipped", result["status"])

    def test_run_skips_cleanly_when_the_iso_is_missing(self) -> None:
        original = smoke_test.shutil.which
        smoke_test.shutil.which = lambda name: f"/usr/bin/{name}"
        try:
            result = smoke_test.run(Path("/definitely/not/an/iso.iso"), {}, output_dir=Path("/tmp"))
        finally:
            smoke_test.shutil.which = original
        self.assertEqual("skipped", result["status"])


if __name__ == "__main__":
    unittest.main()
