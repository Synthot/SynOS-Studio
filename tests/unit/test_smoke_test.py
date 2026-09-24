"""tools/smoke_test.py's assertions, tested against fixtures (a fake systemd
listing, a fake firewall dump, a fake `stat` line, a fake QEMU monitor, and
for the graphical check, real screenshots rendered with Pillow and read
with the real tesseract binary) rather than by booting anything: no test in
this file starts qemu-system-x86_64."""
from __future__ import annotations

import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TESSERACT_AVAILABLE = shutil.which("tesseract") is not None
try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False


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


def _render_screenshot(text: str, path: Path, size: tuple[int, int] = (900, 240)) -> None:
    """A fixed, deterministic stand-in for a QEMU screendump: real pixels
    with real text, rendered once with Pillow so the OCR tests below run
    the real tesseract binary against them -- nothing about matching is
    mocked, only "boot a VM and capture its screen" is out of scope here."""
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    font = None
    for candidate in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                       "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"):
        if Path(candidate).is_file():
            font = ImageFont.truetype(candidate, 40)
            break
    if font is None:
        font = ImageFont.load_default(size=40)
    draw.text((30, 80), text, fill="black", font=font)
    image.save(path, "PNG")


@unittest.skipUnless(TESSERACT_AVAILABLE and PIL_AVAILABLE, "tesseract and Pillow are required for OCR fixtures")
class GraphicalScreenshotMatchingTests(unittest.TestCase):
    """The matching rule (evaluate_graphical_screenshot), against three
    fixed screenshots: a good one, one with the wrong name, and one with
    no readable text at all. tesseract runs for real on each."""

    def test_a_good_screenshot_is_certified(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "good.png"
            _render_screenshot("SynOS NGINX Installer", path)
            text = smoke_test.ocr_text(path)
        result = smoke_test.evaluate_graphical_screenshot(text, "SynOS NGINX", settled=True, settle_timeout=240)
        self.assertEqual("certified", result["outcome"])
        self.assertTrue(result["passed"])
        self.assertTrue(result["name_found"])
        self.assertTrue(result["installer_found"])

    def test_a_screenshot_with_the_wrong_name_is_not_certified(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "wrong-name.png"
            _render_screenshot("SynOS Postgres Installer", path)
            text = smoke_test.ocr_text(path)
        result = smoke_test.evaluate_graphical_screenshot(text, "SynOS NGINX", settled=True, settle_timeout=240)
        self.assertEqual("name-not-found", result["outcome"])
        self.assertFalse(result["passed"])
        self.assertFalse(result["name_found"])
        self.assertTrue(result["installer_found"])  # the session clearly came up; only the name claim failed

    def test_an_unreadable_blank_screenshot_is_its_own_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "blank.png"
            Image.new("RGB", (900, 240), "white").save(path, "PNG")  # no text at all
            text = smoke_test.ocr_text(path)
        result = smoke_test.evaluate_graphical_screenshot(text, "SynOS NGINX", settled=True, settle_timeout=240)
        self.assertEqual("blank-or-unreadable-screen", result["outcome"])
        self.assertFalse(result["passed"])
        # name_found/installer_found are computed honestly either way (there is
        # genuinely no match in empty text); "outcome" is what says *why* this
        # failed -- a blank screen, not a legible one with the wrong name on it.
        self.assertFalse(result["name_found"])
        self.assertFalse(result["installer_found"])

    def test_matching_is_case_and_whitespace_insensitive(self) -> None:
        result = smoke_test.evaluate_graphical_screenshot(
            "  synos\n nginx   INSTALLER  ", "SynOS NGINX", settled=True, settle_timeout=240)
        self.assertEqual("certified", result["outcome"])


class EvaluateGraphicalScreenshotOutcomeTests(unittest.TestCase):
    """Every distinct way the certification can fall short, without OCR or
    Pillow -- evaluate_graphical_screenshot is pure, so these pass fixed
    strings straight in."""

    def test_not_settled_is_its_own_outcome_even_with_good_text(self) -> None:
        result = smoke_test.evaluate_graphical_screenshot(
            "SynOS NGINX Installer", "SynOS NGINX", settled=False, settle_timeout=240)
        self.assertEqual("not-settled", result["outcome"])
        self.assertFalse(result["passed"])

    def test_no_expected_name_is_its_own_outcome_not_a_pass(self) -> None:
        result = smoke_test.evaluate_graphical_screenshot(
            "SynOS NGINX Installer", None, settled=True, settle_timeout=240)
        self.assertEqual("no-expected-name", result["outcome"])
        self.assertFalse(result["passed"])
        self.assertIsNone(result["name_found"])
        self.assertTrue(result["installer_found"])

    def test_installer_word_missing_is_its_own_outcome(self) -> None:
        result = smoke_test.evaluate_graphical_screenshot(
            "SynOS NGINX", "SynOS NGINX", settled=True, settle_timeout=240)
        self.assertEqual("installer-word-not-found", result["outcome"])
        self.assertFalse(result["passed"])
        self.assertTrue(result["name_found"])
        self.assertFalse(result["installer_found"])

    def test_result_always_carries_the_same_keys(self) -> None:
        for settled, name in ((True, "X"), (False, "X"), (True, None)):
            with self.subTest(settled=settled, name=name):
                result = smoke_test.evaluate_graphical_screenshot("anything", name, settled, 240)
                self.assertEqual(
                    {"passed", "outcome", "name_found", "installer_found", "note"}, set(result.keys()))


class FakeQmp:
    """Stands in for QmpSession: screendump() writes the next entry of a
    scripted byte sequence to the target path each call (the last entry
    repeats once the script is exhausted), so wait_for_settled_screen's
    hashing/stability logic is exercised without a real QEMU monitor."""

    def __init__(self, script: list[bytes]):
        self.script = script
        self.calls = 0

    def screendump(self, path: Path) -> None:
        content = self.script[min(self.calls, len(self.script) - 1)]
        path.write_bytes(content)
        self.calls += 1


class FakeProc:
    def __init__(self, dies_immediately: bool = False):
        self.returncode = 1 if dies_immediately else None
        self._dies = dies_immediately

    def poll(self):
        return self.returncode


class SettleDetectionTests(unittest.TestCase):
    """wait_for_settled_screen: settles only after the screen both changed
    at least once (proves capture is really running, not stuck from frame
    one) and then held stable for GRAPHICAL_STABLE_SAMPLES consecutive
    samples -- never a fixed sleep."""

    def test_settles_once_the_screen_stops_changing(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            qmp = FakeQmp([b"boot0", b"boot1", b"final", b"final", b"final", b"final"])
            proc = FakeProc()
            path, settled, elapsed = smoke_test.wait_for_settled_screen(
                qmp, proc, Path(d), timeout=5, poll_interval=0, stable_samples=3)
            self.assertTrue(settled)
            self.assertEqual(5, qmp.calls)  # 2 changing frames + 3 identical ones to confirm stability
            self.assertEqual(b"final", path.read_bytes())

    def test_a_screen_frozen_from_the_first_frame_never_counts_as_settled(self) -> None:
        """The pathological case a naive "N identical samples" rule would
        get wrong: a hang on a single static frame is indistinguishable
        from "already settled" unless a real change is also required."""
        with tempfile.TemporaryDirectory() as d:
            qmp = FakeQmp([b"same"])
            proc = FakeProc()
            _path, settled, _elapsed = smoke_test.wait_for_settled_screen(
                qmp, proc, Path(d), timeout=0.05, poll_interval=0.01, stable_samples=3)
        self.assertFalse(settled)

    def test_a_screen_that_keeps_changing_times_out_unsettled(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            qmp = FakeQmp([f"frame{i}".encode() for i in range(1000)])
            proc = FakeProc()
            _path, settled, _elapsed = smoke_test.wait_for_settled_screen(
                qmp, proc, Path(d), timeout=0.05, poll_interval=0.01, stable_samples=3)
        self.assertFalse(settled)

    def test_qemu_dying_mid_poll_raises_instead_of_hanging(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            qmp = FakeQmp([b"x"])
            proc = FakeProc(dies_immediately=True)
            with self.assertRaises(smoke_test.QemuBootTimeout):
                smoke_test.wait_for_settled_screen(qmp, proc, Path(d), timeout=5, poll_interval=0, stable_samples=3)


class GraphicalSkipTests(unittest.TestCase):
    """No qemu, no xorriso, no tesseract, no Pillow: check_graphical_boot
    skips with a printed reason and never raises or fails."""

    def test_missing_tool_skips_cleanly(self) -> None:
        original = smoke_test.graphical_tools_available
        smoke_test.graphical_tools_available = lambda: (False, "tesseract not found on PATH")
        try:
            result = smoke_test.check_graphical_boot(
                Path("/nonexistent.iso"), Path("/nonexistent-kernel"), Path("/nonexistent-initrd"), "LABEL",
                output_dir=Path("/tmp"), expected_name="SynOS NGINX")
        finally:
            smoke_test.graphical_tools_available = original
        self.assertEqual("graphical-boot", result["name"])
        self.assertIsNone(result["passed"])
        self.assertEqual("skipped", result["outcome"])
        self.assertIn("tesseract", result["note"])

    def test_a_skipped_graphical_check_does_not_fail_the_overall_run(self) -> None:
        checks = [
            {"name": "default-target", "passed": True},
            {"name": "open-ports", "passed": True},
            {"name": "graphical-boot", "passed": None, "outcome": "skipped"},
        ]
        self.assertTrue(smoke_test.overall_passed(checks))

    def test_a_real_graphical_failure_does_fail_the_overall_run(self) -> None:
        checks = [
            {"name": "default-target", "passed": True},
            {"name": "graphical-boot", "passed": False, "outcome": "name-not-found"},
        ]
        self.assertFalse(smoke_test.overall_passed(checks))

    def test_no_checks_at_all_is_not_a_pass(self) -> None:
        self.assertFalse(smoke_test.overall_passed([]))


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
