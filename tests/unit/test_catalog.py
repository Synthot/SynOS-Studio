"""The application catalog: generated from the archives, merged with the curated list."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class CatalogTests(unittest.TestCase):
    def test_generated_catalog_is_committed_and_covers_both_bases(self) -> None:
        import yaml
        data = yaml.safe_load((ROOT / "profiles" / "catalog.apps.yml").read_text(encoding="utf-8"))
        self.assertTrue(data["generated"])
        packages = {p["package"]: p for p in data["packages"]}
        self.assertGreater(len(packages), 1500)
        for name in ("gimp", "inkscape", "vlc", "audacity", "blender"):
            self.assertIn(name, packages, name)
            self.assertIn("ubuntu", packages[name]["available"], name)
            self.assertIn("debian", packages[name]["available"], name)
            self.assertTrue(packages[name]["title"] and packages[name]["category"] != "Other", name)
        self.assertFalse(any(p.startswith("lib") for p in packages))
        self.assertIn("resolute", packages["gimp"]["available"]["ubuntu"])
        self.assertIn("trixie", packages["gimp"]["available"]["debian"])

    def test_category_mapping_prefers_the_specific_category(self) -> None:
        bc = load("build_catalog")
        self.assertEqual("Graphics", bc.category_for(["Graphics", "2DGraphics", "RasterGraphics", "GTK"]))
        self.assertEqual("Audio & video", bc.category_for(["AudioVideo", "Player"]))
        self.assertEqual("Security", bc.category_for(["Utility", "Security"]))
        self.assertEqual("Other", bc.category_for(None))
        self.assertEqual("GNU Image Manipulation Program", bc._text({"C": "GNU Image Manipulation Program", "fr": "GIMP"}))

    def test_export_merges_recommended_first_with_availability(self) -> None:
        catalog = json.loads(subprocess.run([sys.executable, str(ROOT / "tools" / "export_catalog.py")],
                                            capture_output=True, text=True, check=True).stdout)["catalog"]["packages"]
        self.assertGreater(len(catalog), 1500)
        recommended = [p for p in catalog if p["recommended"]]
        self.assertTrue(recommended and catalog[: len(recommended)] == recommended, "recommended entries come first")
        names = [p["name"] for p in catalog]
        self.assertEqual(len(names), len(set(names)), "no package twice")
        keepass = next(p for p in catalog if p["name"] == "keepassxc")
        self.assertTrue(keepass["recommended"])
        self.assertIn("ubuntu", keepass["available"], "a curated entry inherits the archive availability")
        blender = next(p for p in catalog if p["name"] == "blender")
        self.assertFalse(blender["recommended"])
        self.assertEqual("Graphics", blender["category"])
        self.assertEqual("debian", "debian" if "debian" in blender["available"] else "")
        # the archives' metadata has gaps (snaps on Ubuntu, components with metadata errors):
        # the curated list carries such packages with no availability, meaning unknown, never hidden
        self.assertIn("keepassxc", names)


if __name__ == "__main__":
    unittest.main()


class ExportedSuiteTests(unittest.TestCase):
    """export_catalog.py's `bases[].suites` is the Studio page's own suite
    choice list (tools/export_catalog.py's module docstring); a suite
    tools/render_manifest.py refuses at render time (bases/*/live.map) must
    never be offered there, or the page sends a person into the same wall."""

    def test_a_suite_that_cannot_back_a_live_image_is_not_offered(self) -> None:
        data = json.loads(subprocess.run([sys.executable, str(ROOT / "tools" / "export_catalog.py")],
                                         capture_output=True, text=True, check=True).stdout)
        by_id = {b["id"]: b for b in data["bases"]}
        self.assertNotIn("jammy", by_id["ubuntu"]["suites"], "jammy cannot back a Live image; see bases/ubuntu/live.map")
        self.assertIn("noble", by_id["ubuntu"]["suites"])
        self.assertIn("resolute", by_id["ubuntu"]["suites"])

    def test_every_offered_suite_agrees_with_live_capable_suites(self) -> None:
        """Not just the known jammy case: every base's exported suite list must
        exactly match render_manifest.live_capable_suites — the one function
        every consumer that offers or builds suites is required to call."""
        sys.path.insert(0, str(ROOT / "tools"))
        import render_manifest as rm  # noqa: E402
        data = json.loads(subprocess.run([sys.executable, str(ROOT / "tools" / "export_catalog.py")],
                                         capture_output=True, text=True, check=True).stdout)
        for entry in data["bases"]:
            base_dir = ROOT / "bases" / entry["id"]
            env = rm.load_env(base_dir / "base.env")
            with self.subTest(base=entry["id"]):
                self.assertEqual(rm.live_capable_suites(base_dir, env), entry["suites"])


class DerivedProfileTests(unittest.TestCase):
    def test_derived_profiles_are_marked_and_archetypes_are_not(self) -> None:
        import json, subprocess, sys
        data = json.loads(subprocess.run([sys.executable, str(ROOT / "tools" / "export_catalog.py")], capture_output=True, text=True, check=True).stdout)
        by_id = {p["id"]: p for p in data["profiles"]}
        for archetype in ("minimal", "workstation", "developer", "ai-workstation", "thin-client", "kiosk", "server"):
            self.assertFalse(by_id[archetype]["derived"], archetype)
        self.assertTrue(by_id["example-acme-finance"]["derived"])

