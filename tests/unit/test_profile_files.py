"""software.files (schema/profile.schema.json, docs/ARCHITECTURE.md
"Configuration files a profile ships"): configuration a profile writes into
the image, rendered through tools/render_manifest.py and applied by the
synos.workstation.profile_files role next to container_services."""
from __future__ import annotations

import importlib.util
import json
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

ROLE = ROOT / "ansible/collections/ansible_collections/synos/workstation/roles/profile_files"


class ValidationTests(unittest.TestCase):
    """validate_profile_files: the render-time, byte-accurate re-check behind
    the schema's approximate one, plus the one thing (the total) a schema
    pattern cannot express."""

    def test_a_well_formed_entry_passes(self) -> None:
        render_manifest.validate_profile_files([{"path": "/etc/example/example.conf", "content": "hello\n"}])
        render_manifest.validate_profile_files([{"path": "/srv/www/index.html", "content": "hi", "mode": "0640"}])
        render_manifest.validate_profile_files([{"path": "/opt/app/app.cfg", "content": "x"}])
        render_manifest.validate_profile_files([{"path": "/usr/local/etc/app.cfg", "content": "x"}])

    def test_a_path_outside_the_allowlist_is_refused_with_a_clear_message(self) -> None:
        with self.assertRaises(render_manifest.ManifestError) as ctx:
            render_manifest.validate_profile_files([{"path": "/home/user/.bashrc", "content": "x"}])
        self.assertIn("/etc, /srv, /opt or /usr/local", str(ctx.exception))
        self.assertIn("/home/user/.bashrc", str(ctx.exception))

    def test_a_dot_dot_path_is_refused_with_a_clear_message(self) -> None:
        with self.assertRaises(render_manifest.ManifestError) as ctx:
            render_manifest.validate_profile_files([{"path": "/etc/../root/.ssh/authorized_keys", "content": "x"}])
        self.assertIn("'..'", str(ctx.exception))

    def test_an_oversized_file_is_refused_with_a_clear_message(self) -> None:
        with self.assertRaises(render_manifest.ManifestError) as ctx:
            render_manifest.validate_profile_files([{"path": "/etc/big.conf", "content": "x" * 20000}])
        self.assertIn("16384-byte per-file limit", str(ctx.exception))

    def test_the_total_across_a_profile_is_refused_with_a_clear_message(self) -> None:
        files = [{"path": f"/etc/f{i}.conf", "content": "x" * 15000} for i in range(5)]
        with self.assertRaises(render_manifest.ManifestError) as ctx:
            render_manifest.validate_profile_files(files)
        self.assertIn("65536-byte total limit", str(ctx.exception))

    def test_a_bad_mode_is_refused_with_a_clear_message(self) -> None:
        for bad in ("644", "0999", "07770", "rwx"):
            with self.assertRaises(render_manifest.ManifestError) as ctx:
                render_manifest.validate_profile_files([{"path": "/etc/x.conf", "content": "x", "mode": bad}])
            self.assertIn("mode", str(ctx.exception))

    def test_a_default_mode_is_accepted_without_being_specified(self) -> None:
        render_manifest.validate_profile_files([{"path": "/etc/x.conf", "content": "x"}])

    def test_a_conventional_symlink_on_this_build_host_is_refused(self) -> None:
        """Best-effort: /etc/mtab is a symlink on every Debian- and
        Ubuntu-family host, which is what this check can actually see."""
        if not Path("/etc/mtab").is_symlink():
            self.skipTest("this host's /etc/mtab is not a symlink")
        with self.assertRaises(render_manifest.ManifestError) as ctx:
            render_manifest.validate_profile_files([{"path": "/etc/mtab", "content": "x"}])
        self.assertIn("symlink", str(ctx.exception))


class RenderTests(unittest.TestCase):
    """A profile carrying software.files renders, and the resolved profile
    (what ansible-vars.json hands to the profile_files role) carries the
    files through unchanged."""

    def setUp(self) -> None:
        self.profile_path = ROOT / "profiles" / "unit-profile-files.yml"
        self.manifest_path = ROOT / "manifests" / "unit-profile-files.yml"
        self.addCleanup(lambda: [p.unlink(missing_ok=True) for p in (self.profile_path, self.manifest_path)])

    def test_a_rendered_profile_carries_its_files_and_modes(self) -> None:
        self.profile_path.write_text("""id: unit-profile-files
extends: server
description: unit test
software:
  files:
    - path: /etc/unit-test/explicit-mode.conf
      content: |
        explicit mode
      mode: "0600"
    - path: /etc/unit-test/default-mode.conf
      content: default mode
""", encoding="utf-8")
        base_manifest = (ROOT / "manifests" / "test-build.yml").read_text(encoding="utf-8")
        self.manifest_path.write_text(base_manifest.replace("profile: workstation", "profile: unit-profile-files"), encoding="utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            args = Path(tmp) / "args.sh"
            resolved = Path(tmp) / "resolved.json"
            code = render_manifest.main(["--manifest", str(self.manifest_path), "--output", str(args), "--resolved", str(resolved)])
            self.assertEqual(0, code)
            self.assertIn('export ANSIBLE_CHROOT_REQUIRED="true"', args.read_text(encoding="utf-8"))
            data = json.loads(resolved.read_text(encoding="utf-8"))
            files = {f["path"]: f for f in data["profile"]["software"]["files"]}
            self.assertEqual("explicit mode\n", files["/etc/unit-test/explicit-mode.conf"]["content"])
            self.assertEqual("0600", files["/etc/unit-test/explicit-mode.conf"]["mode"])
            self.assertEqual("default mode", files["/etc/unit-test/default-mode.conf"]["content"])
            self.assertNotIn("mode", files["/etc/unit-test/default-mode.conf"], "the default is applied by the role, not injected here")

    def test_an_invalid_entry_fails_the_render_with_a_manifest_error(self) -> None:
        self.profile_path.write_text("""id: unit-profile-files
extends: server
description: unit test
software:
  files:
    - path: /not/allowed/here.conf
      content: x
""", encoding="utf-8")
        base_manifest = (ROOT / "manifests" / "test-build.yml").read_text(encoding="utf-8")
        self.manifest_path.write_text(base_manifest.replace("profile: workstation", "profile: unit-profile-files"), encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "render_manifest.py"), "--manifest", str(self.manifest_path), "--check"],
            capture_output=True, text=True, check=False)
        self.assertEqual(1, result.returncode)
        # the profile schema catches this before validate_profile_files ever runs
        self.assertIn("software/files/0/path", result.stderr)
        self.assertIn("does not match", result.stderr)

    def test_existing_profiles_without_files_still_render(self) -> None:
        for profile in ("workstation", "developer", "ai-workstation", "server", "kiosk", "thin-client"):
            with self.subTest(profile=profile):
                p, chain = render_manifest.resolve_profile(profile)
                self.assertGreaterEqual(len(chain), 1)
        result = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "render_manifest.py"), "--check"],
            capture_output=True, text=True, check=False, cwd=ROOT)
        self.assertEqual(0, result.returncode, result.stderr)


class RoleTests(unittest.TestCase):
    """synos.workstation.profile_files: same wiring and style as container_services."""

    def test_role_files_are_well_formed(self) -> None:
        import yaml
        tasks = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text(encoding="utf-8"))
        self.assertIsInstance(tasks, list)
        self.assertTrue(all("name" in task for task in tasks))
        names = " ".join(json.dumps(t) for t in tasks)
        self.assertIn("item.path", names)
        self.assertIn("item.content", names)
        self.assertIn("item.mode", names)
        self.assertIn("default('0644')", names)
        defaults = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text(encoding="utf-8"))
        self.assertEqual([], defaults["profile_files"])

    def test_playbook_wires_the_role_in_next_to_container_services(self) -> None:
        playbook = (ROOT / "ansible/collections/ansible_collections/synos/workstation/playbooks/customize_chroot.yml").read_text(encoding="utf-8")
        self.assertIn("synos.workstation.profile_files", playbook)
        self.assertIn("profile.software.files | default([])", playbook)

    def test_role_renders_path_content_and_default_mode_with_jinja(self) -> None:
        import os
        import jinja2
        env = jinja2.Environment(trim_blocks=True, lstrip_blocks=True)
        env.filters["dirname"] = os.path.dirname  # ansible.builtin provides this; plain jinja2 does not
        template = env.from_string((ROLE / "tasks" / "main.yml").read_text(encoding="utf-8"))
        out = template.render(item={"path": "/etc/x.conf", "content": "hello", "mode": "0640"})
        self.assertIn('dest: "/etc/x.conf"', out)
        self.assertIn('mode: "0640"', out)
        out_default = template.render(item={"path": "/etc/y.conf", "content": "hello"})
        self.assertIn('mode: "0644"', out_default)


if __name__ == "__main__":
    unittest.main()
