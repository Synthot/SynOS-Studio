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
