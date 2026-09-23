"""tools/catalog_conformance.py: proving the *published* catalog builds.
Every test here uses a fake HTTP fetcher, a fake skopeo inspector and a fake
tools/synos; nothing shells out to a real build and nothing opens a real
network connection."""
from __future__ import annotations

import gzip
import importlib.util
import json
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load_module(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module  # dataclasses (Config) needs its module registered to resolve annotations
    spec.loader.exec_module(module)
    return module


cc = load_module("catalog_conformance_under_test", "tools/catalog_conformance.py")


_CATALOG_CACHE: dict | None = None


def real_catalog() -> dict:
    """tools/export_catalog.py's own output, shelled out once per test run
    (not once per test: it is not cheap) and reused read-only from here on."""
    global _CATALOG_CACHE
    if _CATALOG_CACHE is None:
        result = subprocess.run([sys.executable, str(ROOT / "tools" / "export_catalog.py")],
                                capture_output=True, text=True, check=True, cwd=ROOT)
        _CATALOG_CACHE = json.loads(result.stdout)
    return _CATALOG_CACHE


def entry_by_id(catalog: dict, entry_id: str) -> dict:
    return next(e for e in catalog["bundle_catalog"] if e["id"] == entry_id)


FAKE_SYNOS = textwrap.dedent("""\
    #!/usr/bin/env python3
    import json, os, pathlib, sys, time

    args = sys.argv[1:]

    def get(flag):
        return args[args.index(flag) + 1] if flag in args else None

    out, log = get("--output"), get("--log")
    mode = os.environ.get("FAKE_SYNOS_MODE", "success")
    if log:
        pathlib.Path(log).parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8") as handle:
            handle.write("+ fake build\\n")
            if mode == "fail":
                handle.write("simulated failure\\n")

    if mode == "fail":
        print(json.dumps({"ok": False, "exit_code": 3, "error": "simulated failure"}))
        sys.exit(3)

    iso = pathlib.Path(out) / "fake.iso"
    iso.parent.mkdir(parents=True, exist_ok=True)
    iso.write_bytes(b"FAKE-ISO-BYTES" * 4)
    print(json.dumps({"ok": True, "exit_code": 0, "iso": str(iso)}))
    sys.exit(0)
""")


class FakeSynos:
    def __init__(self, mode: str = "success"):
        self._tmpdir = tempfile.TemporaryDirectory(prefix="fake-synos-cc-")
        self.path = Path(self._tmpdir.name) / "fake_synos.py"
        self.path.write_text(FAKE_SYNOS, encoding="utf-8")
        self.path.chmod(self.path.stat().st_mode | stat.S_IEXEC)
        self.mode = mode

    def __enter__(self):
        import os
        self._old_synos = cc.SYNOS
        self._old_env = os.environ.get("FAKE_SYNOS_MODE")
        cc.SYNOS = self.path
        os.environ["FAKE_SYNOS_MODE"] = self.mode
        return self

    def __exit__(self, *exc):
        import os
        cc.SYNOS = self._old_synos
        if self._old_env is None:
            os.environ.pop("FAKE_SYNOS_MODE", None)
        else:
            os.environ["FAKE_SYNOS_MODE"] = self._old_env
        self._tmpdir.cleanup()


class ConfigTests(unittest.TestCase):
    def _write(self, tmp: Path, text: str) -> Path:
        path = tmp / "conformance.yml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_loads_a_minimal_config_with_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(Path(tmp), "catalog_url: https://example.com/catalog.json\nworkdir: /tmp/x\n")
            config = cc.Config.load(path)
        self.assertEqual("https://example.com/catalog.json", config.catalog_url)
        self.assertEqual(cc.DEFAULT_MAX_BUILDS, config.max_builds_per_run)
        self.assertTrue(config.smoke)

    def test_missing_catalog_url_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(Path(tmp), "workdir: /tmp/x\n")
            with self.assertRaises(cc.ConformanceError):
                cc.Config.load(path)

    def test_missing_workdir_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(Path(tmp), "catalog_url: https://example.com\n")
            with self.assertRaises(cc.ConformanceError):
                cc.Config.load(path)

    def test_unknown_key_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(Path(tmp), "catalog_url: https://example.com\nworkdir: /tmp/x\nbogus_key: 1\n")
            with self.assertRaises(cc.ConformanceError):
                cc.Config.load(path)


class FetchCatalogTests(unittest.TestCase):
    def test_parses_a_valid_catalog(self) -> None:
        payload = json.dumps({"bundle_catalog": [{"id": "a"}]}).encode("utf-8")
        catalog = cc.fetch_catalog("http://example", fetcher=lambda url: payload)
        self.assertEqual([{"id": "a"}], catalog["bundle_catalog"])

    def test_invalid_json_is_refused(self) -> None:
        with self.assertRaises(cc.ConformanceError):
            cc.fetch_catalog("http://example", fetcher=lambda url: b"not json")

    def test_missing_bundle_catalog_key_is_refused(self) -> None:
        with self.assertRaises(cc.ConformanceError):
            cc.fetch_catalog("http://example", fetcher=lambda url: b"{}")


class PrepareBundleDirTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = real_catalog()

    def test_a_real_catalog_entry_prepares_and_validates(self) -> None:
        entry = entry_by_id(self.catalog, "web-server-nginx")
        with tempfile.TemporaryDirectory() as tmp:
            bundle_dir = cc.prepare_bundle_dir(entry, Path(tmp) / "bundle")
            result = subprocess.run([sys.executable, str(ROOT / "tools" / "synos"), "--json", "bundle", "validate", str(bundle_dir)],
                                    capture_output=True, text=True, cwd=ROOT)
            payload = json.loads(result.stdout)
        self.assertEqual([], payload["report"]["errors"])

    def test_missing_manifest_key_is_unbuildable(self) -> None:
        entry = {"id": "broken", "files": {"bundle.json": json.dumps({"format": 1})}}
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(cc.EntryUnbuildable):
                cc.prepare_bundle_dir(entry, Path(tmp) / "bundle")

    def test_missing_manifest_file_is_unbuildable(self) -> None:
        entry = {"id": "broken", "files": {"bundle.json": json.dumps({"format": 1, "manifest": "manifests/broken.yml"})}}
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(cc.EntryUnbuildable):
                cc.prepare_bundle_dir(entry, Path(tmp) / "bundle")

    def test_a_missing_name_is_filled_in_from_the_entry_id(self) -> None:
        entry = {
            "id": "acme-widget",
            "files": {
                "bundle.json": json.dumps({"format": 1, "manifest": "manifests/acme-widget.yml"}),
                "manifests/acme-widget.yml": (
                    "schema_version: 1\nbase: ubuntu\nsuite: noble\narch: amd64\n"
                    "profile: server\nregions: [us]\nbrand: synos\n"
                ),
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            bundle_dir = cc.prepare_bundle_dir(entry, Path(tmp) / "bundle")
            bundle_json = json.loads((bundle_dir / "bundle.json").read_text())
            manifest = cc.render_manifest.load_yaml(bundle_dir / "manifests/acme-widget.yml")
        self.assertEqual("acme-widget", bundle_json["name"])
        self.assertEqual("acme-widget", manifest["name"])
        self.assertEqual("1.0.0", manifest["version"])

    def test_a_manifest_missing_base_is_unbuildable(self) -> None:
        entry = {
            "id": "acme-widget",
            "files": {
                "bundle.json": json.dumps({"format": 1, "manifest": "manifests/acme-widget.yml"}),
                "manifests/acme-widget.yml": "schema_version: 1\nsuite: noble\narch: amd64\nprofile: server\nregions: [us]\nbrand: synos\n",
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(cc.EntryUnbuildable):
                cc.prepare_bundle_dir(entry, Path(tmp) / "bundle")


class ResolveProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = real_catalog()

    def test_extends_chain_merges_like_the_engine_does(self) -> None:
        entry = entry_by_id(self.catalog, "web-server-nginx")
        text = entry["files"]["profiles/web-server-nginx.yml"]
        profile = cc.resolve_published_profile("web-server-nginx", text)
        self.assertEqual(["nginx"], [s["name"] for s in profile["software"]["services"]])
        self.assertIn("22/tcp", profile["security"]["open_ports"])

    def test_an_engine_shipped_profile_resolves_without_its_own_text(self) -> None:
        profile = cc.resolve_published_profile("server", None)
        self.assertIn("open_ports", profile["security"])


class ResolvedPackagesTests(unittest.TestCase):
    def test_returns_a_nonempty_concrete_list_for_a_real_entry(self) -> None:
        catalog = real_catalog()
        entry = entry_by_id(catalog, "web-server-nginx")
        import yaml
        manifest = yaml.safe_load(entry["files"]["manifests/web-server-nginx.yml"])
        profile = cc.resolve_published_profile("web-server-nginx", entry["files"]["profiles/web-server-nginx.yml"])
        packages = cc.resolved_install_packages(manifest, profile)
        self.assertGreater(len(packages), 0)
        self.assertNotIn("", packages)


class CheckModeTests(unittest.TestCase):
    def test_ok_when_every_package_and_image_resolves(self) -> None:
        catalog = real_catalog()
        entry = entry_by_id(catalog, "web-server-nginx")
        manifest = __import__("yaml").safe_load(entry["files"]["manifests/web-server-nginx.yml"])
        profile = cc.resolve_published_profile("web-server-nginx", entry["files"]["profiles/web-server-nginx.yml"])
        packages = cc.resolved_install_packages(manifest, profile)

        def fetcher(url: str) -> bytes:
            return gzip.compress("\n".join(f"Package: {p}" for p in packages).encode())

        result = cc.check_one(entry, archive_cache={}, image_cache={}, fetcher=fetcher, inspector=lambda image: True)
        self.assertEqual("ok", result["status"])
        self.assertEqual([], result["missing_packages"])
        self.assertEqual([], result["missing_images"])

    def test_drift_when_a_package_is_gone_from_the_archive(self) -> None:
        catalog = real_catalog()
        entry = entry_by_id(catalog, "web-server-nginx")

        def fetcher(url: str) -> bytes:
            return gzip.compress(b"Package: unrelated-package\n")

        result = cc.check_one(entry, archive_cache={}, image_cache={}, fetcher=fetcher, inspector=lambda image: True)
        self.assertEqual("drift", result["status"])
        self.assertGreater(len(result["missing_packages"]), 0)

    def test_drift_when_a_pinned_image_tag_is_gone(self) -> None:
        catalog = real_catalog()
        entry = entry_by_id(catalog, "web-server-nginx")

        def fetcher(url: str) -> bytes:
            return gzip.compress(b"")

        result = cc.check_one(entry, archive_cache={}, image_cache={}, fetcher=fetcher, inspector=lambda image: False)
        self.assertEqual("drift", result["status"])
        self.assertEqual(["docker.io/library/nginx:1.31.2"], result["missing_images"])

    def test_archive_is_fetched_once_per_base_and_suite(self) -> None:
        catalog = real_catalog()
        calls = []

        def fetcher(url: str) -> bytes:
            calls.append(url)
            return gzip.compress(b"Package: nginx\n")

        cache: dict = {}
        for entry_id in ("web-server-nginx", "web-server-apache", "web-server-caddy"):
            cc.check_one(entry_by_id(catalog, entry_id), archive_cache=cache, image_cache={}, fetcher=fetcher, inspector=lambda i: True)
        # all three ship on the same base/suite (ubuntu noble): the archive is fetched once, not three times
        components_fetched = len({u for u in calls})
        self.assertEqual(components_fetched, len(calls), "no duplicate URL should be fetched twice across entries sharing a base/suite")
        self.assertEqual(1, len(cache))

    def test_an_entry_with_no_service_checks_zero_images(self) -> None:
        catalog = real_catalog()
        entry = entry_by_id(catalog, "yocto-builder")  # no software.services
        result = cc.check_one(entry, archive_cache={}, image_cache={}, fetcher=lambda u: gzip.compress(b""), inspector=lambda i: True)
        self.assertEqual(0, result["images_checked"])


class BuildModeTests(unittest.TestCase):
    def test_successful_entry_report_shape(self) -> None:
        catalog = real_catalog()
        entry = entry_by_id(catalog, "web-server-nginx")
        with tempfile.TemporaryDirectory() as tmp, FakeSynos("success"):
            config = cc.Config(catalog_url="http://x", workdir=Path(tmp) / "work", smoke=False)
            record = cc.build_one(entry, config=config, work_dir=Path(tmp) / "work" / "t")
        self.assertEqual("success", record["status"])
        self.assertEqual("ubuntu", record["base"])
        self.assertEqual("noble", record["suite"])
        self.assertIsNotNone(record["iso"])
        json.dumps(record)  # report.json must serialize as-is

    def test_failing_build_is_recorded_not_raised(self) -> None:
        catalog = real_catalog()
        entry = entry_by_id(catalog, "web-server-nginx")
        with tempfile.TemporaryDirectory() as tmp, FakeSynos("fail"):
            config = cc.Config(catalog_url="http://x", workdir=Path(tmp) / "work", smoke=False)
            record = cc.build_one(entry, config=config, work_dir=Path(tmp) / "work" / "t")
        self.assertEqual("build_failed", record["status"])
        self.assertIsNone(record["iso"])

    def test_unbuildable_entry_is_recorded_honestly_not_skipped(self) -> None:
        entry = {"id": "broken", "files": {"bundle.json": json.dumps({"format": 1})}}
        with tempfile.TemporaryDirectory() as tmp:
            config = cc.Config(catalog_url="http://x", workdir=Path(tmp) / "work", smoke=False)
            record = cc.build_one(entry, config=config, work_dir=Path(tmp) / "work" / "t")
        self.assertEqual("unbuildable", record["status"])
        self.assertTrue(record["log_tail"])

    def test_run_build_respects_max_builds_per_run(self) -> None:
        catalog = real_catalog()
        with tempfile.TemporaryDirectory() as tmp, FakeSynos("success"):
            config = cc.Config(catalog_url="http://x", workdir=Path(tmp) / "work", smoke=False, max_builds_per_run=2)
            report = cc.run_build(config, catalog=catalog, max_builds=2)
        self.assertEqual(2, len(report["targets"]))


class DiffReportsTests(unittest.TestCase):
    def test_no_previous_report_marks_everything_new(self) -> None:
        current = {"targets": {"a": {"status": "success"}}}
        diff = cc.diff_reports(None, current)
        self.assertEqual(["a"], diff["newly_failed"])

    def test_transitions_are_categorized(self) -> None:
        previous = {"targets": {
            "was-ok-now-broken": {"status": "success"},
            "was-broken-now-ok": {"status": "build_failed"},
            "still-broken": {"status": "build_failed"},
            "still-ok": {"status": "success"},
        }}
        current = {"targets": {
            "was-ok-now-broken": {"status": "build_failed"},
            "was-broken-now-ok": {"status": "success"},
            "still-broken": {"status": "timeout"},
            "still-ok": {"status": "success"},
        }}
        diff = cc.diff_reports(previous, current)
        self.assertEqual(["was-ok-now-broken"], diff["newly_failed"])
        self.assertEqual(["was-broken-now-ok"], diff["recovered"])
        self.assertEqual(["still-broken"], diff["still_failing"])
        self.assertEqual(["still-ok"], diff["unchanged"])


class ReportDeliveryTests(unittest.TestCase):
    def test_post_report_failure_is_non_fatal(self) -> None:
        def bad_poster(url, body):
            raise ConnectionError("no route to host")

        cc.post_report({"targets": {}}, "http://example", poster=bad_poster)  # must not raise

    def test_post_report_calls_the_poster_with_json_bytes(self) -> None:
        calls = []
        cc.post_report({"targets": {"a": {}}}, "http://example", poster=lambda url, body: calls.append((url, body)))
        self.assertEqual(1, len(calls))
        self.assertEqual("http://example", calls[0][0])
        self.assertEqual({"targets": {"a": {}}}, json.loads(calls[0][1]))


class SummaryTests(unittest.TestCase):
    def test_summary_reports_counts_and_diff(self) -> None:
        report = {"mode": "check", "catalog_url": "http://x", "targets": {
            "a": {"status": "ok"}, "b": {"status": "drift"},
        }}
        diff = {"newly_failed": ["b"], "recovered": [], "still_failing": [], "unchanged": ["a"]}
        text = cc.summary_text(report, diff)
        self.assertIn("1 drift", text)
        self.assertIn("1 ok", text)
        self.assertIn("newly failing: b", text)


class DryRunCliTests(unittest.TestCase):
    def test_dry_run_lists_entries_without_building(self) -> None:
        fake_catalog = {"bundle_catalog": [{"id": "a"}, {"id": "b"}, {"id": "c"}]}
        original = cc.fetch_catalog
        cc.fetch_catalog = lambda url, **kwargs: fake_catalog
        try:
            with tempfile.TemporaryDirectory() as tmp:
                config_path = Path(tmp) / "c.yml"
                config_path.write_text(f"catalog_url: http://x\nworkdir: {tmp}/work\nmax_builds_per_run: 2\n", encoding="utf-8")
                code = cc.main(["build", "--config", str(config_path), "--dry-run"])
        finally:
            cc.fetch_catalog = original
        self.assertEqual(0, code)


if __name__ == "__main__":
    unittest.main()
