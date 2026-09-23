"""The AI workstation pieces: hardware.gpu, container services, flat repositories."""
from __future__ import annotations

import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("render_manifest", ROOT / "tools" / "render_manifest.py")
render_manifest = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(render_manifest)


class AiWorkstationTests(unittest.TestCase):
    def test_archetype_and_catalog_agree(self) -> None:
        profile = render_manifest.load_yaml(ROOT / "profiles" / "ai-workstation.yml")
        self.assertEqual("developer", profile["extends"])
        self.assertEqual("none", profile["hardware"]["gpu"])
        catalog = render_manifest.load_yaml(ROOT / "profiles" / "catalog.yml")
        self.assertEqual({"none", "nvidia", "amd", "intel"}, {g["id"] for g in catalog["gpus"]})
        services = {s["id"]: s for s in catalog["services"]}
        # The AI inference services this archetype uses; the catalog also carries
        # non-AI appliance services (bundle-catalog, docs/BUNDLE.md) that this
        # test does not concern itself with.
        self.assertLessEqual({"ollama", "open-webui", "vllm"}, set(services))
        self.assertEqual(["nvidia", "amd"], services["vllm"]["gpu_required"])
        self.assertIn("amd", services["ollama"]["images"])
        for base in ("ubuntu", "debian"):
            pkg_map = render_manifest.load_package_map(ROOT / "bases" / base / "packages.map")
            for name in ("gpu-nvidia", "gpu-amd", "gpu-intel", "ai-dev"):
                self.assertTrue(pkg_map.get(name), f"{base}: {name}")
        self.assertTrue((ROOT / "keys" / "nvidia-container-toolkit.asc").read_text(encoding="utf-8").startswith("-----BEGIN PGP PUBLIC KEY BLOCK-----"))

    def test_derived_profile_with_gpu_services_and_flat_repository_renders(self) -> None:
        profile_path = ROOT / "profiles" / "unit-ai.yml"
        manifest_path = ROOT / "manifests" / "unit-ai.yml"
        profile_path.write_text("""id: unit-ai
extends: ai-workstation
description: unit test
hardware:
  gpu: nvidia
software:
  repositories:
    - name: nvidia-container-toolkit
      url: https://nvidia.github.io/libnvidia-container/stable/deb/${ARCH}/
      suite: "/"
      key: keys/nvidia-container-toolkit.asc
      packages: [nvidia-container-toolkit]
  services:
    - name: ollama
      image: docker.io/ollama/ollama:latest
      ports: ["11434:11434"]
      volumes: ["ollama:/root/.ollama"]
      gpu: true
    - name: vllm
      image: docker.io/vllm/vllm-openai:latest
      ports: ["8000:8000"]
      gpu: true
      exec: "--model Qwen/Qwen2.5-1.5B-Instruct"
""", encoding="utf-8")
        manifest_path.write_text((ROOT / "manifests" / "test-build.yml").read_text(encoding="utf-8").replace("profile: workstation", "profile: unit-ai"),
                                 encoding="utf-8")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                args = Path(tmp) / "args.sh"
                resolved = Path(tmp) / "resolved.json"
                code = render_manifest.main(["--manifest", str(manifest_path), "--output", str(args), "--resolved", str(resolved)])
                self.assertEqual(0, code)
                text = args.read_text(encoding="utf-8")
                self.assertIn('export ANSIBLE_CHROOT_REQUIRED="true"', text)
                data = json.loads(resolved.read_text(encoding="utf-8"))
                self.assertEqual("nvidia", data["profile"]["hardware"]["gpu"])
                self.assertEqual(["ollama", "vllm"], [s["name"] for s in data["profile"]["software"]["services"]])
                sources = (Path(tmp) / "software" / "repos" / "nvidia-container-toolkit.sources").read_text(encoding="utf-8")
                self.assertIn("URIs: https://nvidia.github.io/libnvidia-container/stable/deb/amd64/\n", sources)
                self.assertIn("Suites: /\n", sources)
                self.assertNotIn("Components:", sources, "a flat repository has no components")
                self.assertIn("Signed-By: /etc/apt/keyrings/nvidia-container-toolkit.asc", sources)
                install = data["packages"]["install"]
                self.assertIn("nvidia-driver-580-open", install)
                self.assertIn("nvidia-cuda-toolkit", install)
                self.assertIn("gpu-nvidia", data["packages"]["abstract"])
        finally:
            profile_path.unlink(missing_ok=True)
            manifest_path.unlink(missing_ok=True)

    def test_quadlet_template_maps_the_gpu_per_vendor(self) -> None:
        import jinja2
        template = jinja2.Template((ROOT / "ansible/collections/ansible_collections/synos/workstation/roles/container_services/templates/service.container.j2").read_text(encoding="utf-8"),
                                   trim_blocks=True, lstrip_blocks=True)
        item = {"name": "ollama", "image": "docker.io/ollama/ollama:latest", "ports": ["11434:11434"], "gpu": True}
        nvidia = template.render(item=item, container_services_gpu="nvidia")
        self.assertIn("AddDevice=nvidia.com/gpu=all", nvidia)
        self.assertIn("After=network-online.target synos-nvidia-cdi.service", nvidia)
        amd = template.render(item=item, container_services_gpu="amd")
        self.assertIn("AddDevice=/dev/kfd", amd)
        self.assertIn("AddDevice=/dev/dri", amd)
        self.assertNotIn("nvidia", amd)
        none = template.render(item=dict(item, gpu=False), container_services_gpu="none")
        self.assertNotIn("AddDevice", none)
        self.assertIn("PublishPort=11434:11434", none)


if __name__ == "__main__":
    unittest.main()
