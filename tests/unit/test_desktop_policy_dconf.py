"""synos.workstation.desktop_policy's dconf-local.j2: policy.dconf keys may be
written as a dconf path ("org/gnome/desktop/lockdown/disable-command-line")
or as the more familiar dotted GSettings name
("org.gnome.desktop.lockdown.disable-command-line"). Before this template
existed the key was split inline in tasks/main.yml with key.rsplit('/', 1),
which raised IndexError on any dotted key carrying a real (non-"locked")
value, because rsplit('/', 1) on a string with no '/' returns a single-
element list and [1] is out of range. kiosk.yml's own policy.dconf entry
never triggered it only because its value is the "locked" sentinel, which
customize_chroot.yml diverts into desktop_policy_locks before this template
ever sees it. This test renders the fixed template directly, the same way
tests/unit/test_policy_ai.py renders syn_policy's template, so a future
regression fails here instead of only inside a real chroot build.
"""
from __future__ import annotations

import unittest
from pathlib import Path

import jinja2

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_PATH = (
    ROOT
    / "ansible/collections/ansible_collections/synos/workstation/roles/desktop_policy/templates/dconf-local.j2"
)


def render(desktop_policy_dconf: dict) -> str:
    template = jinja2.Template(TEMPLATE_PATH.read_text(encoding="utf-8"), trim_blocks=True, lstrip_blocks=True)
    return template.render(desktop_policy_dconf=desktop_policy_dconf)


def sections(text: str) -> dict[str, set[str]]:
    """Parse the rendered dconf keyfile into {group: {"key=value", ...}}."""
    groups: dict[str, set[str]] = {}
    current = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            groups[current] = set()
        elif current is not None:
            groups[current].add(line)
    return groups


class DconfLocalTemplateTests(unittest.TestCase):
    def test_a_dotted_gsettings_key_with_a_real_value_no_longer_crashes(self) -> None:
        """The exact shape that used to raise IndexError: a dotted key
        (no '/') paired with a real value, not the "locked" sentinel."""
        text = render({"org.gnome.desktop.lockdown.disable-command-line": "true"})
        self.assertEqual(
            {"org/gnome/desktop/lockdown": {"disable-command-line=true"}},
            sections(text),
        )

    def test_a_slash_path_key_renders_unchanged(self) -> None:
        text = render({"org/gnome/desktop/screensaver/lock-delay": "uint32 300"})
        self.assertEqual(
            {"org/gnome/desktop/screensaver": {"lock-delay=uint32 300"}},
            sections(text),
        )

    def test_dotted_and_slash_keys_mix_freely_in_the_same_policy(self) -> None:
        text = render({
            "org.gnome.desktop.lockdown.disable-command-line": "true",
            "org/gnome/desktop/screensaver/lock-enabled": "true",
        })
        result = sections(text)
        self.assertEqual({"disable-command-line=true"}, result["org/gnome/desktop/lockdown"])
        self.assertEqual({"lock-enabled=true"}, result["org/gnome/desktop/screensaver"])

    def test_two_keys_in_the_same_group_share_one_section(self) -> None:
        text = render({
            "org/gnome/desktop/lockdown/disable-command-line": "true",
            "org/gnome/desktop/lockdown/user-administration-disabled": "true",
        })
        result = sections(text)
        self.assertEqual(1, len(result))
        self.assertEqual(
            {"disable-command-line=true", "user-administration-disabled=true"},
            result["org/gnome/desktop/lockdown"],
        )

    def test_empty_policy_renders_no_sections(self) -> None:
        self.assertEqual({}, sections(render({})))

    def test_classroom_workstation_profile_dconf_renders_cleanly(self) -> None:
        """Integration check: the actual policy.dconf shipped by the
        classroom-workstation bundle-catalog entry renders without error
        and produces the expected lockdown keys."""
        import importlib.util

        spec = importlib.util.spec_from_file_location("render_manifest", ROOT / "tools" / "render_manifest.py")
        render_manifest = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(render_manifest)

        raw = render_manifest.load_yaml(
            ROOT / "bundle-catalog" / "classroom-workstation" / "profiles" / "classroom-workstation.yml"
        )
        dconf = raw["policy"]["dconf"]
        text = render(dconf)
        result = sections(text)
        self.assertIn("disable-command-line=true", result["org/gnome/desktop/lockdown"])
        self.assertIn("user-administration-disabled=true", result["org/gnome/desktop/lockdown"])


if __name__ == "__main__":
    unittest.main()
