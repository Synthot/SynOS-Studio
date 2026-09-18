"""policy.ai (contract C8): schema, the syn_policy role template, and the
variables the renderer hands to Ansible."""
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("render_manifest", ROOT / "tools" / "render_manifest.py")
render_manifest = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(render_manifest)

ROLE_DIR = ROOT / "ansible/collections/ansible_collections/synos/workstation/roles/syn_policy"

# The C8 example from docs/ai/01-contracts.md, minus the profile/policy
# wrapper: this is the value of profile.policy.ai.
C8_EXAMPLE = {
    "enabled": True,
    "providers": ["ollama", "kexyn"],
    "local_only": False,
    "tools_denied": ["destructive"],
    "memory": "enabled",
}


def _profile(policy_ai: dict) -> dict:
    return {
        "id": "unit-policy",
        "policy": {"ai": policy_ai},
    }


class PolicySchemaTests(unittest.TestCase):
    def test_schema_accepts_the_c8_example(self) -> None:
        render_manifest.validate_schema(_profile(C8_EXAMPLE), "profile.schema.json")  # raises on failure

    def test_schema_accepts_the_optional_facts_key(self) -> None:
        render_manifest.validate_schema(_profile({**C8_EXAMPLE, "facts": "grounded-only"}), "profile.schema.json")

    def test_schema_rejects_an_unknown_key(self) -> None:
        with self.assertRaises(render_manifest.ManifestError):
            render_manifest.validate_schema(_profile({**C8_EXAMPLE, "sneaky": True}), "profile.schema.json")

    def test_schema_rejects_an_unknown_memory_value(self) -> None:
        with self.assertRaises(render_manifest.ManifestError):
            render_manifest.validate_schema(_profile({**C8_EXAMPLE, "memory": "forever"}), "profile.schema.json")

    def test_schema_rejects_an_unknown_facts_value(self) -> None:
        with self.assertRaises(render_manifest.ManifestError):
            render_manifest.validate_schema(_profile({**C8_EXAMPLE, "facts": "sometimes"}), "profile.schema.json")


class PolicyTemplateTests(unittest.TestCase):
    def _render(self, **overrides) -> dict:
        import jinja2
        try:
            import tomllib
        except ImportError:  # Python < 3.11
            import tomli as tomllib
        template = jinja2.Template(
            (ROLE_DIR / "templates" / "policy.toml.j2").read_text(encoding="utf-8"),
            trim_blocks=True, lstrip_blocks=True,
        )
        variables = {
            "syn_policy_enabled": True,
            "syn_policy_providers": [],
            "syn_policy_local_only": False,
            "syn_policy_tools_denied": [],
            "syn_policy_memory": "enabled",
            "syn_policy_facts": "",
        }
        variables.update(overrides)
        text = template.render(**variables)
        return tomllib.loads(text)

    def test_template_renders_the_c8_example(self) -> None:
        data = self._render(
            syn_policy_providers=C8_EXAMPLE["providers"],
            syn_policy_tools_denied=C8_EXAMPLE["tools_denied"],
        )
        self.assertEqual(True, data["enabled"])
        self.assertEqual(["ollama", "kexyn"], data["providers"])
        self.assertEqual(False, data["local_only"])
        self.assertEqual(["destructive"], data["tools_denied"])
        self.assertEqual("enabled", data["memory"])
        self.assertNotIn("facts", data, "facts is omitted, not written empty, when the profile leaves it out")

    def test_template_omits_facts_when_blank_but_writes_it_when_set(self) -> None:
        blank = self._render()
        self.assertNotIn("facts", blank)
        grounded = self._render(syn_policy_facts="grounded-only")
        self.assertEqual("grounded-only", grounded["facts"])

    def test_template_disabled_and_local_only_round_trip(self) -> None:
        data = self._render(syn_policy_enabled=False, syn_policy_local_only=True)
        self.assertEqual(False, data["enabled"])
        self.assertEqual(True, data["local_only"])


class PolicyRenderTests(unittest.TestCase):
    def test_rendered_variables_carry_the_policy(self) -> None:
        profile_path = ROOT / "profiles" / "unit-policy.yml"
        manifest_path = ROOT / "manifests" / "unit-policy.yml"
        profile_path.write_text(
            "id: unit-policy\n"
            "extends: minimal\n"
            "description: unit test\n"
            "policy:\n"
            "  ai:\n"
            "    enabled: true\n"
            "    providers: [ollama, kexyn]\n"
            "    local_only: false\n"
            "    tools_denied: [destructive]\n"
            "    memory: enabled\n",
            encoding="utf-8",
        )
        manifest_path.write_text(
            (ROOT / "manifests" / "test-build.yml").read_text(encoding="utf-8").replace(
                "profile: workstation", "profile: unit-policy"
            ),
            encoding="utf-8",
        )
        try:
            with tempfile.TemporaryDirectory() as tmp:
                args = Path(tmp) / "args.sh"
                resolved = Path(tmp) / "resolved.json"
                code = render_manifest.main(["--manifest", str(manifest_path), "--output", str(args), "--resolved", str(resolved)])
                self.assertEqual(0, code)
                text = args.read_text(encoding="utf-8")
                self.assertIn('export ANSIBLE_CHROOT_REQUIRED="true"', text)
                data = json.loads(resolved.read_text(encoding="utf-8"))
                self.assertEqual(C8_EXAMPLE, data["profile"]["policy"]["ai"])
                ansible_vars = json.loads((Path(tmp) / "ansible-vars.json").read_text(encoding="utf-8"))
                self.assertEqual(C8_EXAMPLE, ansible_vars["synos_profile"]["policy"]["ai"])
        finally:
            profile_path.unlink(missing_ok=True)
            manifest_path.unlink(missing_ok=True)

    def test_no_policy_ai_does_not_force_ansible(self) -> None:
        # minimal.yml itself carries no policy.ai and only an empty
        # policy.dconf; render_manifest must not require Ansible for that.
        profile, _chain = render_manifest.resolve_profile("minimal")
        self.assertNotIn("ai", profile.get("policy", {}))


if __name__ == "__main__":
    unittest.main()
