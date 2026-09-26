"""tools/build_saturation.py: the generator for the saturation-<base>
bundle — internal test-engine machinery, never a product. These tests cover
the union/skip/exclusion logic and, deliberately, the one invariant the
owner asked for by name: nothing here is ever reachable through
bundle-catalog/index.yml or tools/export_catalog.py's own output, so a
future change that starts exporting one fails a test, not a code review."""
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]


def load_module(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


build_saturation = load_module("build_saturation_under_test", "tools/build_saturation.py")
render_manifest = load_module("render_manifest_under_test", "tools/render_manifest.py")


def _independent_union(base_id: str) -> set[str]:
    """The same union, computed straight from profiles/bundles.yml and
    profiles/*.yml without going through build_saturation.plan() at all —
    so a test comparing against this cannot pass just because both sides
    share a bug. Resolved against the same suite plan() itself now resolves
    against (build_saturation.target_suite) — packages.map can carry a
    per-suite override (render_manifest.load_package_map), so this must
    pick the same suite plan() does or the two sides would disagree for a
    reason that has nothing to do with a real bug in either."""
    base_dir = ROOT / "bases" / base_id
    pkg_map = render_manifest.load_package_map(base_dir / "packages.map")
    bundles = render_manifest.load_bundles()
    suite = build_saturation.target_suite(base_id, ROOT)
    union: set[str] = set()
    for gid, abstract in bundles.items():
        try:
            concrete, _ = render_manifest.resolve_packages(abstract, pkg_map, "amd64", ["en"], base_id, suite=suite)
        except render_manifest.ManifestError:
            continue
        union.update(concrete)
    for path in build_saturation.machine_profile_files(ROOT):
        _pid, names = build_saturation.profile_own_add(path)
        if not names:
            continue
        try:
            concrete, _ = render_manifest.resolve_packages(names, pkg_map, "amd64", ["en"], base_id, suite=suite)
        except render_manifest.ManifestError:
            continue
        union.update(concrete)
    return union


class PlanTests(unittest.TestCase):
    def test_ubuntu_and_debian_union_matches_an_independently_computed_union(self) -> None:
        for base_id in ("ubuntu", "debian"):
            result = build_saturation.plan(base_id)
            self.assertEqual(_independent_union(base_id), set(result["concrete_union"]),
                             f"base {base_id}: plan()'s union must match resolving "
                             f"profiles/bundles.yml + profiles/*.yml independently")

    def test_every_bundles_yml_group_is_either_included_skipped_or_excluded_on_every_base(self) -> None:
        bundle_ids = set(render_manifest.load_bundles())
        for base_id in build_saturation.real_bases(ROOT):
            result = build_saturation.plan(base_id)
            accounted = (set(result["included_bundles"]) | {g for g, _ in result["skipped_bundles"]}
                        | {g for g, _ in result["excluded_groups"]})
            self.assertEqual(bundle_ids, accounted,
                             f"base {base_id}: every group in profiles/bundles.yml must be accounted for, "
                             f"named as included, skipped (unavailable) or excluded — never silently dropped")

    def test_test_engine_group_is_skipped_on_ubuntu_named_with_a_reason(self) -> None:
        """browser-headless is unavailable on ubuntu (bases/ubuntu/packages.map);
        the whole test-engine group must be skipped, not silently short one
        package — the exact case profiles/bundles.yml's own test-engine comment
        describes."""
        result = build_saturation.plan("ubuntu")
        self.assertNotIn("test-engine", result["included_bundles"])
        skipped = dict(result["skipped_bundles"])
        self.assertIn("test-engine", skipped)
        self.assertIn("browser-headless", skipped["test-engine"])
        self.assertIn("unavailable", skipped["test-engine"])

    def test_test_engine_group_is_included_on_debian(self) -> None:
        result = build_saturation.plan("debian")
        self.assertIn("test-engine", result["included_bundles"])
        self.assertEqual([], result["skipped_bundles"])

    def test_every_machine_profiles_own_add_list_is_covered(self) -> None:
        profile_ids = {build_saturation.profile_own_add(p)[0] for p in build_saturation.machine_profile_files(ROOT)
                       if build_saturation.profile_own_add(p)[1]}
        for base_id in build_saturation.real_bases(ROOT):
            result = build_saturation.plan(base_id)
            covered = set(result["add_by_profile"]) | {pid for pid, _ in result["skipped_add"]}
            self.assertEqual(profile_ids, covered)


class ExclusionsMapTests(unittest.TestCase):
    def test_absent_file_means_nothing_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual({}, build_saturation.load_exclusions("nosuchbase", Path(tmp)))

    def test_parses_reason_after_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bases" / "fake").mkdir(parents=True)
            (root / "bases" / "fake" / "saturation-exclusions.map").write_text(
                "# a comment\nsome-package = excluded: it Conflicts: with another union member\n",
                encoding="utf-8")
            mapping = build_saturation.load_exclusions("fake", root)
            self.assertEqual({"some-package": "it Conflicts: with another union member"}, mapping)

    def test_a_line_without_the_excluded_marker_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bases" / "fake").mkdir(parents=True)
            (root / "bases" / "fake" / "saturation-exclusions.map").write_text(
                "some-package = just dropped, no reason\n", encoding="utf-8")
            with self.assertRaises(build_saturation.SaturationError):
                build_saturation.load_exclusions("fake", root)

    def test_both_real_exclusions_maps_are_currently_empty_and_documented_as_checked(self) -> None:
        """bases/ubuntu/saturation-exclusions.map and bases/debian/saturation-exclusions.map:
        the real archive check (docs/BUILD_MATRIX.md) found zero genuine
        collisions as of this writing. This test pins that fact so a future
        change that adds an entry is a deliberate, reviewed edit, not a
        silent one, and so the file's header claim of "checked, found none"
        cannot quietly go stale into "never checked"."""
        for base_id in ("ubuntu", "debian"):
            path = ROOT / "bases" / base_id / "saturation-exclusions.map"
            self.assertTrue(path.is_file())
            self.assertEqual({}, build_saturation.load_exclusions(base_id, ROOT))

    def test_a_stale_exclusion_that_matches_nothing_is_refused(self) -> None:
        with mock.patch.object(build_saturation, "load_exclusions",
                               return_value={"totally-not-a-real-package": "made up"}):
            with self.assertRaises(build_saturation.SaturationError):
                build_saturation.plan("ubuntu")

    def test_a_group_level_exclusion_removes_the_whole_group(self) -> None:
        # load_exclusions() already strips the `excluded:` marker before plan()
        # ever sees the reason; the mock stands in for that already-parsed shape.
        with mock.patch.object(build_saturation, "load_exclusions",
                               return_value={"retro-gaming": "test fixture"}):
            result = build_saturation.plan("ubuntu")
        self.assertNotIn("retro-gaming", result["included_bundles"])
        self.assertIn(("retro-gaming", "test fixture"), result["excluded_groups"])

    def test_a_package_level_exclusion_is_stripped_from_the_union_and_from_remove(self) -> None:
        result = build_saturation.plan("ubuntu")
        # pick a package that is genuinely in today's union, so the fixture
        # exercises the real strip-and-record path rather than a fake name.
        victim = result["concrete_union"][0]
        with mock.patch.object(build_saturation, "load_exclusions",
                               return_value={victim: "test fixture"}):
            excluded_result = build_saturation.plan("ubuntu")
        self.assertNotIn(victim, excluded_result["concrete_union"])
        self.assertIn(victim, excluded_result["remove"])
        self.assertIn((victim, "test fixture"), excluded_result["package_exclusions"])


class WriteBundleTests(unittest.TestCase):
    def test_write_bundle_produces_a_schema_shaped_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "saturation-ubuntu"
            result = build_saturation.write_bundle("ubuntu", dest)
            descriptor = json.loads((dest / "bundle.json").read_text(encoding="utf-8"))
            self.assertEqual(1, descriptor["format"])
            self.assertEqual("manifests/saturation-ubuntu.yml", descriptor["manifest"])
            self.assertTrue((dest / descriptor["manifest"]).is_file())
            self.assertTrue((dest / "profiles" / "saturation-ubuntu.yml").is_file())
            self.assertEqual("saturation-ubuntu", result["profile_id"])
            self.assertGreater(len(result["concrete_union"]), 0)

    def test_write_bundle_validates_through_tools_synos_bundle_validate(self) -> None:
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "saturation-debian"
            build_saturation.write_bundle("debian", dest)
            result = subprocess.run([sys.executable, str(ROOT / "tools" / "synos"), "bundle", "validate", str(dest)],
                                    capture_output=True, text=True, check=False)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn("valid", result.stdout)

    def test_generated_profile_names_every_skip_in_its_own_description(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            import yaml
            dest = Path(tmp) / "saturation-ubuntu"
            build_saturation.write_bundle("ubuntu", dest)
            profile = yaml.safe_load((dest / "profiles" / "saturation-ubuntu.yml").read_text(encoding="utf-8"))
            self.assertIn("test-engine", profile["description"])
            self.assertIn("browser-headless", profile["description"])


class NeverCatalogedTests(unittest.TestCase):
    """The owner's own rule: this is internal test-engine machinery. A
    saturation bundle must never be reachable through the real bundle
    catalog a customer browses on the Studio page."""

    def test_index_yml_names_no_saturation_entry(self) -> None:
        import yaml
        index = yaml.safe_load((ROOT / "bundle-catalog" / "index.yml").read_text(encoding="utf-8"))
        ids = [e["id"] for e in index.get("bundle_catalog", [])]
        self.assertFalse(any(i.startswith("saturation") for i in ids),
                         "bundle-catalog/index.yml must never name a saturation bundle: it is what "
                         "tools/export_catalog.py exports to the Studio page a customer browses")

    def test_export_catalog_never_emits_a_saturation_entry(self) -> None:
        export_catalog = load_module("export_catalog_under_test", "tools/export_catalog.py")
        catalog = export_catalog.bundle_catalog()
        ids = [item["id"] for item in catalog]
        self.assertFalse(any(i.startswith("saturation") for i in ids),
                         "tools/export_catalog.py's bundle_catalog() must never emit a saturation entry — "
                         "this is what the Studio page actually renders as catalogue cards")

    def test_default_output_and_build_matrix_output_are_never_under_bundle_catalog(self) -> None:
        build_matrix = load_module("build_matrix_never_catalog_test", "tools/build_matrix.py")
        bundle_catalog_dir = (ROOT / "bundle-catalog").resolve()

        def outside_bundle_catalog(path: Path) -> bool:
            resolved = path.resolve()
            return bundle_catalog_dir != resolved and bundle_catalog_dir not in resolved.parents

        parser_default = build_saturation.ROOT / ".build" / "saturation"
        self.assertTrue(outside_bundle_catalog(parser_default))
        self.assertTrue(outside_bundle_catalog(build_matrix.SATURATION_DIR))


if __name__ == "__main__":
    unittest.main()
