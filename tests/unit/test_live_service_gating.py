"""docs/ARCHITECTURE.md, "Live session vs. installed system": a live boot
(rd.synos.live=1 on the kernel command line, every live entry in
build.sh's LIVE_BOOT_ARGS) must never start a service the profile itself
brought in -- a web server, a database, a container registry -- on the
live session's deliberately passwordless machine, while an installed
system starts every one of them normally with nothing extra done to it.

Two mechanisms cover the two ways a profile brings a unit in:
  - software.services (containers): service.container.j2 carries the
    Condition itself (covered by tests/unit/test_ai_workstation.py's
    test_quadlet_unit_never_starts_in_the_live_session).
  - software.packages.add, resolved across the whole extends chain (never
    a curated profiles/bundles.yml group -- see
    tests/unit/test_manifest_render.py's ApplianceLiveGatingTests for why
    cups and podman must stay out of this list even though they are
    genuinely installed): synos.workstation.live_service_gating, wired
    into customize_chroot.yml next to container_services, covered here.

Plus the one on-screen note (synos-live-session-setup's motd line) for
whoever never runs systemctl status."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / "ansible/collections/ansible_collections/synos/workstation/roles/live_service_gating"
PLAYBOOK = ROOT / "ansible/collections/ansible_collections/synos/workstation/playbooks/customize_chroot.yml"
LIVE_SESSION_SETUP = ROOT / "packages/synos-live-settings/assets/usr/libexec/synos-live-session-setup"


class RoleTests(unittest.TestCase):
    """synos.workstation.live_service_gating: same wiring and style as
    container_services and profile_files."""

    def test_role_files_are_well_formed(self) -> None:
        import yaml
        tasks = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text(encoding="utf-8"))
        self.assertIsInstance(tasks, list)
        self.assertTrue(all("name" in task for task in tasks))
        defaults = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text(encoding="utf-8"))
        self.assertEqual([], defaults["live_service_gating_packages"])

    def test_playbook_wires_the_role_in_next_to_container_services(self) -> None:
        playbook = PLAYBOOK.read_text(encoding="utf-8")
        self.assertIn("synos.workstation.container_services", playbook)
        self.assertIn("synos.workstation.live_service_gating", playbook)
        self.assertIn("synos_appliance_packages", playbook)
        # The role only ever runs when the profile's own software.packages.add
        # actually resolved to something; nothing to gate is not an error.
        self.assertIn("when: appliance_packages | length > 0", playbook)

    def test_unit_discovery_command_covers_both_service_and_socket_units(self) -> None:
        """A socket-activated appliance daemon (a future software.packages.add
        entry that ships a .socket alongside its .service) must be caught the
        same way as a plain .service -- a .socket would otherwise sit
        listening whether or not its backend ever starts. Both extensions
        must be caught, from either of Debian's pre- and post-usrmerge unit
        paths."""
        tasks_text = (ROLE / "tasks" / "main.yml").read_text(encoding="utf-8")
        self.assertIn(r"\.(service|socket)$", tasks_text)
        self.assertIn("/(usr/)?lib/systemd/system/", tasks_text)

    def test_the_drop_in_content_carries_the_live_gate_and_explains_itself(self) -> None:
        import shlex
        import jinja2
        env = jinja2.Environment(trim_blocks=True, lstrip_blocks=True)
        env.filters["quote"] = shlex.quote  # ansible.builtin provides this; plain jinja2 does not
        tasks = env.from_string((ROLE / "tasks" / "main.yml").read_text(encoding="utf-8"))
        # Render with a fake registered-command result standing in for
        # synos_live_gated_units, the way Ansible would after the shell task.
        # containerd (kubernetes-server's own software.packages.add) is a
        # real package this role gates today; .socket is exercised with a
        # synthetic name since no catalog entry currently adds one.
        out = tasks.render(item="containerd.service", live_service_gating_packages=["containerd"],
                            synos_live_gated_units={"stdout_lines": ["containerd.service"]})
        self.assertIn("containerd.service.d", out)
        self.assertIn("ConditionKernelCommandLine=!rd.synos.live", out)
        socket_out = tasks.render(item="example.socket", live_service_gating_packages=["example"],
                                  synos_live_gated_units={"stdout_lines": ["example.socket"]})
        self.assertIn("example.socket.d", socket_out)
        self.assertIn("90-synos-live-suppress.conf", out)


class SystemdSyntaxTests(unittest.TestCase):
    """The exact drop-in this role writes must be syntax the installed
    systemd actually accepts, including for a unit only ever pulled in via
    another unit's Wants= (Condition= is evaluated per activation attempt,
    not only for a directly-started unit)."""

    def test_systemd_analyze_verify_accepts_the_generated_drop_in(self) -> None:
        import shutil
        if not shutil.which("systemd-analyze"):
            self.skipTest("systemd-analyze is not on PATH on this machine")
        with tempfile.TemporaryDirectory() as tmp:
            unit_dir = Path(tmp) / "example.service.d"
            unit_dir.mkdir()
            (Path(tmp) / "example.service").write_text(
                "[Unit]\nDescription=test\n[Service]\nExecStart=/bin/true\n[Install]\nWantedBy=multi-user.target\n",
                encoding="utf-8")
            (unit_dir / "90-synos-live-suppress.conf").write_text(
                "[Unit]\nConditionKernelCommandLine=!rd.synos.live\n", encoding="utf-8")
            wanter = Path(tmp) / "example-wanter.service"
            wanter.write_text(
                "[Unit]\nDescription=pulls in example.service\nWants=example.service\n"
                "[Service]\nExecStart=/bin/true\n", encoding="utf-8")
            result = subprocess.run(
                ["systemd-analyze", "verify", "--recursive-errors=no", str(Path(tmp) / "example.service"), str(wanter)],
                capture_output=True, text=True, check=False)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)


class LiveSessionMotdTests(unittest.TestCase):
    """synos-live-session-setup: the one on-screen note, for whoever never
    runs systemctl status."""

    def _run_setup(self, root: Path, *, with_marker: bool) -> subprocess.CompletedProcess[str]:
        (root / "run" / "synos-live").mkdir(parents=True, exist_ok=True)
        if with_marker:
            (root / "run" / "synos-live" / "environment").write_text("SYNOS_LIVE=1\n", encoding="utf-8")
        cmdline_file = root / "cmdline"
        cmdline_file.write_text("root=live:CDLABEL=synos rd.synos.live=1 quiet splash\n", encoding="utf-8")
        env = dict(os.environ)
        env.update({
            "SYNOS_LIVE_ROOT": str(root),
            "SYNOS_LIVE_CMDLINE_FILE": str(cmdline_file),
            "SYNOS_LIVE_TEST_MODE": "1",
        })
        return subprocess.run(["/bin/sh", str(LIVE_SESSION_SETUP)], capture_output=True, text=True, env=env, check=False)

    def test_the_live_session_gets_the_note_in_motd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = self._run_setup(root, with_marker=True)
            self.assertEqual(0, result.returncode, result.stderr)
            motd = (root / "etc" / "motd").read_text(encoding="utf-8")
            self.assertIn("live SynOS session", motd)
            self.assertIn("passwordless", motd)
            self.assertIn("Install it", motd)

    def test_an_installed_system_never_gets_the_note(self) -> None:
        """No /run/synos-live/environment marker (an installed system's real
        boot never creates it) means the whole script -- including the motd
        note -- is a no-op."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = self._run_setup(root, with_marker=False)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertFalse((root / "etc" / "motd").exists())


if __name__ == "__main__":
    unittest.main()
