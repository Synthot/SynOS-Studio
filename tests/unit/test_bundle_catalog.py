"""The bundle catalog: bundle-catalog/, docs/BUNDLE.md "The bundle catalog",
and the bundle_catalog key tools/export_catalog.py exports for it.

A catalogued bundle is an ordinary bundle (schema/bundle.schema.json,
schema/manifest.schema.json, schema/profile.schema.json) kept in this
repository as a ready-made starting point."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CATALOG_DIR = ROOT / "bundle-catalog"
SYNOS = ROOT / "tools" / "synos"


def load_module(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


render_manifest = load_module("render_manifest", "tools/render_manifest.py")
export_catalog = load_module("export_catalog", "tools/export_catalog.py")


def run_synos(*args: str) -> tuple[int, dict]:
    result = subprocess.run([sys.executable, str(SYNOS), "--json", *args], capture_output=True, text=True, check=False, cwd=ROOT)
    return result.returncode, json.loads(result.stdout)


def index_entries() -> list[dict]:
    data = render_manifest.load_yaml(CATALOG_DIR / "index.yml")
    return data.get("bundle_catalog", [])


class IndexAndFoldersTests(unittest.TestCase):
    def test_index_and_folders_agree(self) -> None:
        entries = index_entries()
        self.assertTrue(entries, "bundle-catalog/index.yml lists no entries")
        indexed_folders = {e["folder"] for e in entries}
        on_disk = {p.name for p in CATALOG_DIR.iterdir() if p.is_dir()}
        self.assertEqual(on_disk, indexed_folders, "an orphan folder or a missing folder")
        ids = [e["id"] for e in entries]
        self.assertEqual(len(ids), len(set(ids)), "duplicate id in bundle-catalog/index.yml")

    def test_every_entry_has_the_required_index_fields(self) -> None:
        for entry in index_entries():
            with self.subTest(entry=entry["id"]):
                for field in ("id", "name", "summary", "tags", "folder"):
                    self.assertIn(field, entry)
                self.assertRegex(entry["id"], r"^[a-z0-9][a-z0-9-]*$")
                self.assertTrue(entry["tags"], "no tags")
                for tag in entry["tags"]:
                    self.assertEqual(tag, tag.lower(), f"tag {tag!r} is not lowercase")


class CatalogedBundleValidationTests(unittest.TestCase):
    """Every catalogued bundle validates exactly like a person's downloaded bundle."""

    def test_every_entry_validates_as_a_bundle(self) -> None:
        for entry in index_entries():
            folder = CATALOG_DIR / entry["folder"]
            with self.subTest(entry=entry["id"]):
                code, payload = run_synos("bundle", "validate", str(folder))
                self.assertEqual(0, code, payload)
                self.assertEqual([], payload["report"]["errors"])

    def test_every_manifest_renders_on_its_declared_base_and_suite(self) -> None:
        for entry in index_entries():
            folder = CATALOG_DIR / entry["folder"]
            bundle = json.loads((folder / "bundle.json").read_text(encoding="utf-8"))
            manifest_path = folder / bundle["manifest"]
            with self.subTest(entry=entry["id"]):
                result = subprocess.run(
                    [sys.executable, str(ROOT / "tools" / "render_manifest.py"), "--manifest", str(manifest_path), "--check"],
                    capture_output=True, text=True, check=False,
                )
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_every_bundle_json_matches_its_schema(self) -> None:
        schema = json.loads((ROOT / "schema" / "bundle.schema.json").read_text(encoding="utf-8"))
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema is not installed")
        for entry in index_entries():
            folder = CATALOG_DIR / entry["folder"]
            descriptor = json.loads((folder / "bundle.json").read_text(encoding="utf-8"))
            with self.subTest(entry=entry["id"]):
                jsonschema.Draft202012Validator(schema).validate(descriptor)

    def test_every_shipped_profile_matches_its_schema_and_resolves(self) -> None:
        """Every extends and every package group a catalogued bundle's profile chain
        names exists, and it resolves without error (profiles ship either in the
        bundle or, like every machine kind added here, with the engine)."""
        for entry in index_entries():
            folder = CATALOG_DIR / entry["folder"]
            bundle = json.loads((folder / "bundle.json").read_text(encoding="utf-8"))
            manifest = render_manifest.load_yaml(folder / bundle["manifest"])
            for profile_path in sorted(folder.glob("profiles/*.yml")):
                with self.subTest(entry=entry["id"], profile=profile_path.name):
                    data = render_manifest.load_yaml(profile_path)
                    render_manifest.validate_schema(data, "profile.schema.json")
            with self.subTest(entry=entry["id"], profile=manifest["profile"]):
                profile, chain = render_manifest.resolve_profile(manifest["profile"])
                self.assertGreaterEqual(len(chain), 1)


class PackageResolutionTests(unittest.TestCase):
    """Every package group a catalogued bundle's profile chain uses resolves to a
    non-empty concrete package list on both bases."""

    def test_bundle_groups_resolve_on_both_bases(self) -> None:
        bundles = render_manifest.load_bundles()
        pkg_maps = {
            base_dir.name: render_manifest.load_package_map(base_dir / "packages.map")
            for base_dir in sorted(p for p in (ROOT / "bases").iterdir() if p.is_dir() and not p.name.startswith("_"))
        }
        for entry in index_entries():
            folder = CATALOG_DIR / entry["folder"]
            bundle = json.loads((folder / "bundle.json").read_text(encoding="utf-8"))
            manifest = render_manifest.load_yaml(folder / bundle["manifest"])
            profile, _ = render_manifest.resolve_profile(manifest["profile"])
            bundle_ids = list((profile.get("software") or {}).get("bundles", []))
            for bundle_id in bundle_ids:
                self.assertIn(bundle_id, bundles, f"{entry['id']}: unknown bundle {bundle_id!r}")
                for base_id, pkg_map in pkg_maps.items():
                    with self.subTest(entry=entry["id"], bundle=bundle_id, base=base_id):
                        concrete, _ = render_manifest.resolve_packages(bundles[bundle_id], pkg_map, "amd64", ["en"], base_id)
                        self.assertTrue(concrete, f"{bundle_id} resolves to nothing on {base_id}")

    def test_yocto_build_group_is_mapped_on_both_bases(self) -> None:
        bundles = render_manifest.load_bundles()
        self.assertIn("yocto-build", bundles)
        abstract = bundles["yocto-build"]
        self.assertIn("yocto-host-tools", abstract)
        for base_dir in sorted(p for p in (ROOT / "bases").iterdir() if p.is_dir() and not p.name.startswith("_")):
            pkg_map = render_manifest.load_package_map(base_dir / "packages.map")
            with self.subTest(base=base_dir.name):
                self.assertIn("yocto-host-tools", pkg_map)
                concrete, unmapped = render_manifest.resolve_packages(abstract, pkg_map, "amd64", ["en"], base_dir.name)
                self.assertEqual([], unmapped)
                self.assertGreaterEqual(len(concrete), 20)
                self.assertIn("build-essential", concrete)
                self.assertIn("lz4", concrete)
                self.assertNotIn("liblz4-tool", concrete)


class YoctoProfileTests(unittest.TestCase):
    def test_yocto_builder_extends_developer_and_mentions_locale_and_disk(self) -> None:
        data = render_manifest.load_yaml(ROOT / "profiles" / "yocto-builder.yml")
        self.assertEqual("developer", data.get("extends"))
        description = data.get("description", "")
        self.assertIn("UTF-8", description)
        self.assertRegex(description.lower(), r"disk|gb|space")
        profile, chain = render_manifest.resolve_profile("yocto-builder")
        self.assertEqual(["minimal", "workstation", "developer", "yocto-builder"], chain)
        self.assertIn("yocto-build", profile["software"]["bundles"])

    def test_yocto_builder_is_exported_as_an_archetype(self) -> None:
        data = json.loads(subprocess.run([sys.executable, str(ROOT / "tools" / "export_catalog.py")],
                                          capture_output=True, text=True, check=True).stdout)
        by_id = {p["id"]: p for p in data["profiles"]}
        self.assertIn("yocto-builder", by_id)
        self.assertFalse(by_id["yocto-builder"]["derived"])


class ExportedCatalogTests(unittest.TestCase):
    """tools/export_catalog.py's bundle_catalog key: the contract the Studio's
    own half of this feature is built against."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.data = export_catalog.collect()

    def test_bundle_catalog_key_has_the_documented_shape(self) -> None:
        catalog = self.data["bundle_catalog"]
        entries = index_entries()
        self.assertEqual(len(entries), len(catalog))
        self.assertEqual([e["id"] for e in entries], [c["id"] for c in catalog], "export order must follow index.yml")
        for entry in catalog:
            with self.subTest(entry=entry["id"]):
                for field in ("id", "name", "summary", "tags", "kind", "files"):
                    self.assertIn(field, entry)
                self.assertIsInstance(entry["tags"], list)
                self.assertIsInstance(entry["files"], dict)
                self.assertIn("bundle.json", entry["files"])
                manifest_rel = json.loads(entry["files"]["bundle.json"])["manifest"]
                self.assertIn(manifest_rel, entry["files"])

    def test_kind_is_the_profile_the_manifest_names(self) -> None:
        by_id = {e["id"]: e for e in self.data["bundle_catalog"]}
        self.assertEqual("yocto-builder", by_id["yocto-builder"]["kind"])
        self.assertEqual("workstation", by_id["office-workstation"]["kind"])
        self.assertEqual("server", by_id["small-server"]["kind"])

    def test_files_are_byte_identical_to_the_folder(self) -> None:
        index_by_id = {e["id"]: e for e in index_entries()}
        for entry in self.data["bundle_catalog"]:
            folder = CATALOG_DIR / index_by_id[entry["id"]]["folder"]
            on_disk = {p.relative_to(folder).as_posix(): p.read_bytes() for p in folder.rglob("*") if p.is_file()}
            with self.subTest(entry=entry["id"]):
                self.assertEqual(set(on_disk), set(entry["files"]), "files map does not list exactly the folder's files")
                for path, raw in on_disk.items():
                    self.assertEqual(raw.decode("utf-8"), entry["files"][path], path)

    def test_no_starter_wording_leaks_into_the_export(self) -> None:
        text = json.dumps(self.data["bundle_catalog"])
        self.assertNotIn("starter", text.lower())


if __name__ == "__main__":
    unittest.main()
