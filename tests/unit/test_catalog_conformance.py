"""tools/catalog_conformance.py: proving the *published* catalog builds
through the real Studio page and its real launcher. Every test here uses a
fake browser (a `browser_factory` returning fixed, in-memory `.tar.gz`
bytes built with Python's own `tarfile` module — never a real
`google-chrome`), a fake `build.sh` script (never a real launcher or a real
container build), a fake HTTP fetcher and a fake skopeo inspector for
`check`. Nothing here starts a real browser, a real build, or opens a real
network connection."""
from __future__ import annotations

import gzip
import importlib.util
import io
import json
import subprocess
import sys
import tarfile
import tempfile
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


def _git_status(cwd: Path) -> str:
    return subprocess.run(["git", "status", "--porcelain"], cwd=cwd, capture_output=True, text=True, check=True).stdout


def _git_worktree_list(cwd: Path) -> str:
    return subprocess.run(["git", "worktree", "list", "--porcelain"], cwd=cwd, capture_output=True, text=True, check=True).stdout


# ------------------------------------------------------ fake bundle archives
FAKE_BUILD_SH_SUCCESS = """#!/bin/bash
mkdir -p dist
echo "fake launcher output" > dist/build.log
echo "FAKE-ISO-BYTES" > dist/fake.iso
exit 0
"""

FAKE_BUILD_SH_LAUNCHER_FAIL = """#!/bin/bash
echo "podman or docker is required" >&2
exit 2
"""

FAKE_BUILD_SH_ENGINE_FAIL = """#!/bin/bash
mkdir -p dist
echo "debootstrap failed inside the container" > dist/build.log
exit 3
"""

FAKE_BUILD_SH_ENV_CAPTURE = """#!/bin/bash
mkdir -p dist
env | grep '^SYNOS_' | sort > dist/env.txt
echo "log" > dist/build.log
echo "FAKE-ISO-BYTES" > dist/fake.iso
exit 0
"""


def make_bundle_archive(*, entry_id: str = "web-server-nginx", base: str = "ubuntu", suite: str = "noble",
                        profile: str | None = None, build_sh: str | None = None,
                        extra_files: dict | None = None, omit: set | None = None) -> bytes:
    """A minimal but realistic bundle .tar.gz — the same shape
    tools/devtools_browser.py's StudioSession would capture from the real
    page — built directly with Python's own tarfile module."""
    profile = profile or entry_id
    omit = omit or set()
    manifest_yaml = (f"schema_version: 1\nname: {entry_id}\nversion: 1.0.0\nbase: {base}\nsuite: {suite}\n"
                     f"arch: amd64\nprofile: {profile}\nregions: [us]\nbrand: synos\nmirrors: {{}}\n"
                     f"packages: {{repository: '', takeover: all}}\noverrides: {{}}\n")
    profile_yaml = ("id: " + profile + "\nextends: server\nsoftware:\n  services:\n    - name: nginx\n"
                    "      image: docker.io/library/nginx:1.31.2\n      ports: ['80:80']\n"
                    "security:\n  open_ports: [22/tcp, 80/tcp]\n")
    bundle_json = json.dumps({"format": 1, "name": entry_id, "manifest": f"manifests/{entry_id}.yml"})
    files = {
        "bundle.json": bundle_json,
        f"manifests/{entry_id}.yml": manifest_yaml,
        f"profiles/{profile}.yml": profile_yaml,
        "build.sh": FAKE_BUILD_SH_SUCCESS if build_sh is None else build_sh,
    }
    if extra_files:
        files.update(extra_files)
    for name in omit:
        files.pop(name, None)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in files.items():
            data = content.encode("utf-8")
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mode = 0o755 if name == "build.sh" else 0o644
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class FakeSession:
    """A fake StudioSession: `browser_factory()` returns one of these, used
    as `with browser_factory() as session: session.download_bundle(id)`."""

    def __init__(self, archives_by_id: dict | None = None, default_archive: bytes | None = None,
                unavailable: bool = False, page_error: str | None = None):
        self.archives_by_id = archives_by_id or {}
        self.default_archive = default_archive
        self.unavailable = unavailable
        self.page_error = page_error
        self.requested_ids: list[str] = []

    def __call__(self):  # used directly as a browser_factory
        return self

    def __enter__(self):
        if self.unavailable:
            raise cc.devtools_browser.BrowserUnavailable("no browser available (test fixture)")
        return self

    def __exit__(self, *exc):
        return False

    def download_bundle(self, entry_id: str, name: str | None = None) -> tuple[bytes, str]:
        self.requested_ids.append(entry_id)
        if self.page_error:
            raise cc.devtools_browser.PageError(self.page_error)
        raw = self.archives_by_id.get(entry_id, self.default_archive)
        if raw is None:
            raise cc.devtools_browser.PageError(f"the catalog does not offer {entry_id!r} (test fixture)")
        return raw, f"{entry_id}-bundle.tar.gz"


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
        self.assertIsNone(config.site_url)
        self.assertTrue(config.smoke)

    def test_site_url_is_loaded_when_given(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(Path(tmp), "catalog_url: https://example.com\nworkdir: /tmp/x\nsite_url: https://studio.example\n")
            config = cc.Config.load(path)
        self.assertEqual("https://studio.example", config.site_url)

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


class ArchiveExtractionTests(unittest.TestCase):
    """extract_bundle_archive / read_bundle_manifest: what happens to the
    real bytes downloaded through the page, good and bad."""

    def test_a_well_formed_archive_extracts_and_its_manifest_reads(self) -> None:
        raw = make_bundle_archive(entry_id="web-server-nginx")
        with tempfile.TemporaryDirectory() as tmp:
            into = Path(tmp) / "bundle"
            cc.extract_bundle_archive(raw, into)
            self.assertTrue((into / "bundle.json").is_file())
            self.assertTrue((into / "build.sh").is_file())
            manifest_rel, manifest = cc.read_bundle_manifest(into)
        self.assertEqual("manifests/web-server-nginx.yml", manifest_rel)
        self.assertEqual("ubuntu", manifest["base"])
        self.assertEqual("noble", manifest["suite"])

    def test_not_a_tar_gz_at_all_is_an_archive_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(cc.ArchiveError):
                cc.extract_bundle_archive(b"not a tar.gz file", Path(tmp) / "bundle")

    def test_a_path_that_escapes_the_target_directory_is_refused(self) -> None:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            data = b"malicious"
            info = tarfile.TarInfo(name="../../etc/passwd")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(cc.ArchiveError):
                cc.extract_bundle_archive(buf.getvalue(), Path(tmp) / "bundle")

    def test_missing_bundle_json_is_an_archive_error(self) -> None:
        raw = make_bundle_archive(entry_id="x", omit={"bundle.json"})
        with tempfile.TemporaryDirectory() as tmp:
            into = Path(tmp) / "bundle"
            cc.extract_bundle_archive(raw, into)
            with self.assertRaises(cc.ArchiveError):
                cc.read_bundle_manifest(into)

    def test_manifest_named_by_bundle_json_but_missing_is_an_archive_error(self) -> None:
        raw = make_bundle_archive(entry_id="x", omit={"manifests/x.yml"})
        with tempfile.TemporaryDirectory() as tmp:
            into = Path(tmp) / "bundle"
            cc.extract_bundle_archive(raw, into)
            with self.assertRaises(cc.ArchiveError):
                cc.read_bundle_manifest(into)


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
    """Unchanged by the browser/launcher rewrite (item 64): `check` still
    reads the catalog JSON's own embedded files directly."""

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
        components_fetched = len({u for u in calls})
        self.assertEqual(components_fetched, len(calls), "no duplicate URL should be fetched twice across entries sharing a base/suite")
        self.assertEqual(1, len(cache))

    def test_an_entry_with_no_service_checks_zero_images(self) -> None:
        catalog = real_catalog()
        entry = entry_by_id(catalog, "yocto-builder")  # no software.services
        result = cc.check_one(entry, archive_cache={}, image_cache={}, fetcher=lambda u: gzip.compress(b""), inspector=lambda i: True)
        self.assertEqual(0, result["images_checked"])


class BuildOneStageTests(unittest.TestCase):
    """build_one's three-stage split (item 65): page, launcher, engine —
    plus smoke — each pinned with a fake browser and a fake build.sh."""

    def _config(self, tmp: str, **kwargs) -> "cc.Config":
        kwargs.setdefault("smoke", False)
        return cc.Config(catalog_url="http://x", workdir=Path(tmp) / "work", site_url="http://fake-studio", **kwargs)

    def test_a_successful_entry_has_no_stage_and_an_iso(self) -> None:
        raw = make_bundle_archive(entry_id="web-server-nginx")
        session = FakeSession(default_archive=raw)
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(tmp)
            record = cc.build_one({"id": "web-server-nginx"}, config=config, work_dir=Path(tmp) / "work" / "t",
                                  browser_factory=session)
        self.assertEqual("success", record["status"])
        self.assertIsNone(record["stage"])
        self.assertEqual("ubuntu", record["base"])
        self.assertEqual("noble", record["suite"])
        self.assertIsNotNone(record["iso"])
        self.assertEqual(64, len(record["checksum"]))
        self.assertEqual("web-server-nginx-bundle.tar.gz", record["download"]["filename"])
        self.assertEqual(["web-server-nginx"], session.requested_ids)
        json.dumps(record)  # report.json must serialize as-is

    def test_browser_unavailable_is_skipped_not_a_crash(self) -> None:
        session = FakeSession(unavailable=True)
        with tempfile.TemporaryDirectory() as tmp:
            record = cc.build_one({"id": "x"}, config=self._config(tmp), work_dir=Path(tmp) / "work" / "t",
                                  browser_factory=session)
        self.assertEqual("skipped", record["status"])
        self.assertEqual(cc.STAGE_PAGE, record["stage"])

    def test_page_refuses_the_entry_is_a_page_stage_failure(self) -> None:
        session = FakeSession(page_error="the page's own validation refuses this entry")
        with tempfile.TemporaryDirectory() as tmp:
            record = cc.build_one({"id": "x"}, config=self._config(tmp), work_dir=Path(tmp) / "work" / "t",
                                  browser_factory=session)
        self.assertEqual("failed", record["status"])
        self.assertEqual(cc.STAGE_PAGE, record["stage"])

    def test_entry_not_offered_by_the_page_is_a_page_stage_failure(self) -> None:
        session = FakeSession(archives_by_id={})  # this entry id is simply not offered
        with tempfile.TemporaryDirectory() as tmp:
            record = cc.build_one({"id": "does-not-exist"}, config=self._config(tmp), work_dir=Path(tmp) / "work" / "t",
                                  browser_factory=session)
        self.assertEqual("failed", record["status"])
        self.assertEqual(cc.STAGE_PAGE, record["stage"])

    def test_a_corrupt_downloaded_archive_is_a_page_stage_failure(self) -> None:
        session = FakeSession(default_archive=b"not actually a tar.gz")
        with tempfile.TemporaryDirectory() as tmp:
            record = cc.build_one({"id": "x"}, config=self._config(tmp), work_dir=Path(tmp) / "work" / "t",
                                  browser_factory=session)
        self.assertEqual("failed", record["status"])
        self.assertEqual(cc.STAGE_PAGE, record["stage"])

    def test_launcher_failure_before_any_containerized_build_is_a_launcher_stage_failure(self) -> None:
        raw = make_bundle_archive(entry_id="x", build_sh=FAKE_BUILD_SH_LAUNCHER_FAIL)
        session = FakeSession(default_archive=raw)
        with tempfile.TemporaryDirectory() as tmp:
            record = cc.build_one({"id": "x"}, config=self._config(tmp), work_dir=Path(tmp) / "work" / "t",
                                  browser_factory=session)
        self.assertEqual("host", record["status"])
        self.assertEqual(cc.STAGE_LAUNCHER, record["stage"])
        self.assertEqual(2, record["exit_code"])

    def test_engine_failure_inside_the_container_is_an_engine_stage_failure(self) -> None:
        raw = make_bundle_archive(entry_id="x", build_sh=FAKE_BUILD_SH_ENGINE_FAIL)
        session = FakeSession(default_archive=raw)
        with tempfile.TemporaryDirectory() as tmp:
            record = cc.build_one({"id": "x"}, config=self._config(tmp), work_dir=Path(tmp) / "work" / "t",
                                  browser_factory=session)
        self.assertEqual("build_failed", record["status"])
        self.assertEqual(cc.STAGE_ENGINE, record["stage"])
        self.assertEqual(3, record["exit_code"])
        self.assertIn("debootstrap failed inside the container", record["log_tail"])

    def test_a_smoke_test_failure_is_recorded_as_the_smoke_stage_without_changing_status(self) -> None:
        raw = make_bundle_archive(entry_id="web-server-nginx")
        session = FakeSession(default_archive=raw)
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(tmp, smoke=True)
            old_smoke_run = cc.smoke_test.run
            cc.smoke_test.run = lambda *a, **k: {"status": "failed", "checks": []}
            try:
                record = cc.build_one({"id": "web-server-nginx"}, config=config, work_dir=Path(tmp) / "work" / "t",
                                      browser_factory=session)
            finally:
                cc.smoke_test.run = old_smoke_run
        self.assertEqual("success", record["status"], "a smoke failure must not change the build's own status")
        self.assertEqual(cc.STAGE_SMOKE, record["stage"])

    def test_a_missing_iso_after_success_skips_smoke_honestly(self) -> None:
        build_sh_no_iso = "#!/bin/bash\nmkdir -p dist\necho log > dist/build.log\nexit 0\n"
        raw = make_bundle_archive(entry_id="x", build_sh=build_sh_no_iso)
        session = FakeSession(default_archive=raw)
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(tmp, smoke=True)
            record = cc.build_one({"id": "x"}, config=config, work_dir=Path(tmp) / "work" / "t", browser_factory=session)
        self.assertEqual("success", record["status"])
        self.assertIsNone(record["iso"])
        self.assertEqual("skipped", record["smoke"]["status"])


class LauncherEnvironmentTests(unittest.TestCase):
    """The environment ./build.sh actually runs under (item 62): SYNOS_YES=1
    and nothing else unless the config says so — no update-check or channel
    suppression, ever."""

    def test_synos_yes_is_set_and_no_update_check_or_channel_override_is(self) -> None:
        raw = make_bundle_archive(entry_id="x", build_sh=FAKE_BUILD_SH_ENV_CAPTURE)
        session = FakeSession(default_archive=raw)
        with tempfile.TemporaryDirectory() as tmp:
            config = cc.Config(catalog_url="http://x", workdir=Path(tmp) / "work", site_url="http://fake", smoke=False)
            record = cc.build_one({"id": "x"}, config=config, work_dir=Path(tmp) / "work" / "t", browser_factory=session)
            env_text = (Path(tmp) / "work" / "t" / "bundle" / "dist" / "env.txt").read_text()
        self.assertIn("SYNOS_YES=1", env_text)
        self.assertNotIn("SYNOS_NO_UPDATE_CHECK", env_text)
        self.assertNotIn("SYNOS_CHANNEL", env_text)
        self.assertEqual("success", record["status"])

    def test_container_root_and_runroot_and_image_and_engine_source_reach_the_launcher(self) -> None:
        raw = make_bundle_archive(entry_id="x", build_sh=FAKE_BUILD_SH_ENV_CAPTURE)
        session = FakeSession(default_archive=raw)
        with tempfile.TemporaryDirectory() as tmp:
            config = cc.Config(catalog_url="http://x", workdir=Path(tmp) / "work", site_url="http://fake", smoke=False,
                               container_root="/mnt/big/synos-storage", container_runroot="/mnt/big/synos-runroot",
                               image="ghcr.io/example/synos-builder:ubuntu-noble", engine_source="/opt/synos-engine")
            cc.build_one({"id": "x"}, config=config, work_dir=Path(tmp) / "work" / "t", browser_factory=session)
            env_text = (Path(tmp) / "work" / "t" / "bundle" / "dist" / "env.txt").read_text()
        self.assertIn("SYNOS_CONTAINER_ROOT=/mnt/big/synos-storage", env_text)
        self.assertIn("SYNOS_CONTAINER_RUNROOT=/mnt/big/synos-runroot", env_text)
        self.assertIn("SYNOS_BUILDER_IMAGE=ghcr.io/example/synos-builder:ubuntu-noble", env_text)
        self.assertIn("SYNOS_ENGINE_SOURCE=/opt/synos-engine", env_text)

    def test_unset_optional_settings_are_simply_absent(self) -> None:
        raw = make_bundle_archive(entry_id="x", build_sh=FAKE_BUILD_SH_ENV_CAPTURE)
        session = FakeSession(default_archive=raw)
        with tempfile.TemporaryDirectory() as tmp:
            config = cc.Config(catalog_url="http://x", workdir=Path(tmp) / "work", site_url="http://fake", smoke=False)
            cc.build_one({"id": "x"}, config=config, work_dir=Path(tmp) / "work" / "t", browser_factory=session)
            env_text = (Path(tmp) / "work" / "t" / "bundle" / "dist" / "env.txt").read_text()
        for key in ("SYNOS_CONTAINER_ROOT", "SYNOS_CONTAINER_RUNROOT", "SYNOS_BUILDER_IMAGE", "SYNOS_ENGINE_SOURCE"):
            self.assertNotIn(key, env_text)


class RunBuildTests(unittest.TestCase):
    def test_run_build_respects_max_builds_per_run(self) -> None:
        catalog = real_catalog()
        raw = make_bundle_archive(entry_id="placeholder")  # download() below ignores the requested id anyway
        session = FakeSession(default_archive=raw)
        with tempfile.TemporaryDirectory() as tmp:
            config = cc.Config(catalog_url="http://x", workdir=Path(tmp) / "work", site_url="http://fake",
                               smoke=False, max_builds_per_run=2)
            report = cc.run_build(config, catalog=catalog, max_builds=2, browser_factory=session)
        self.assertEqual(2, len(report["targets"]))

    def test_only_selects_exactly_those_entries(self) -> None:
        catalog = real_catalog()
        raw = make_bundle_archive(entry_id="placeholder")
        session = FakeSession(default_archive=raw)
        with tempfile.TemporaryDirectory() as tmp:
            config = cc.Config(catalog_url="http://x", workdir=Path(tmp) / "work", site_url="http://fake", smoke=False)
            report = cc.run_build(config, catalog=catalog, only=["web-server-nginx", "git-server"], browser_factory=session)
        self.assertEqual({"web-server-nginx", "git-server"}, set(report["targets"]))

    def test_only_with_an_unknown_id_is_refused(self) -> None:
        catalog = real_catalog()
        with tempfile.TemporaryDirectory() as tmp:
            config = cc.Config(catalog_url="http://x", workdir=Path(tmp) / "work", site_url="http://fake", smoke=False)
            with self.assertRaises(cc.ConformanceError):
                cc.run_build(config, catalog=catalog, only=["does-not-exist"], browser_factory=FakeSession())


class ParallelismTests(unittest.TestCase):
    """run_build's own use of tools/job_queue.py: above jobs=1, a worker
    without an explicit container_root gets its own podman storage root, so
    two workers building the same base/suite never share the launcher's own
    cache-volume name."""

    def _archive_for(self, entry_id: str) -> bytes:
        return make_bundle_archive(entry_id=entry_id, build_sh=FAKE_BUILD_SH_ENV_CAPTURE)

    def test_jobs_above_one_gives_each_worker_its_own_container_root(self) -> None:
        ids = ["web-server-nginx", "web-server-apache", "web-server-caddy", "git-server"]
        catalog = real_catalog()
        entries = [entry_by_id(catalog, i) for i in ids]
        sub_catalog = {"bundle_catalog": entries}
        session = FakeSession(archives_by_id={i: self._archive_for(i) for i in ids})

        with tempfile.TemporaryDirectory() as tmp:
            config = cc.Config(catalog_url="http://x", workdir=Path(tmp) / "work", site_url="http://fake", smoke=False)
            report = cc.run_build(config, catalog=sub_catalog, max_builds=None, jobs=4, browser_factory=session)
            roots_used = set()
            for entry_id in ids:
                env_text = (Path(tmp) / "work" / "work" / entry_id / "bundle" / "dist" / "env.txt").read_text()
                for line in env_text.splitlines():
                    if line.startswith("SYNOS_CONTAINER_ROOT="):
                        roots_used.add(line.split("=", 1)[1])

        self.assertEqual(4, len(report["targets"]))
        self.assertTrue(all(r["status"] == "success" for r in report["targets"].values()))
        self.assertGreaterEqual(len(roots_used), 2, "four entries across four workers must use more than one podman storage root")

    def test_jobs_1_sets_no_container_root_when_none_was_configured(self) -> None:
        ids = ["web-server-nginx", "web-server-apache"]
        catalog = real_catalog()
        entries = [entry_by_id(catalog, i) for i in ids]
        sub_catalog = {"bundle_catalog": entries}
        session = FakeSession(archives_by_id={i: self._archive_for(i) for i in ids})

        with tempfile.TemporaryDirectory() as tmp:
            config = cc.Config(catalog_url="http://x", workdir=Path(tmp) / "work", site_url="http://fake", smoke=False)
            report = cc.run_build(config, catalog=sub_catalog, max_builds=None, jobs=1, browser_factory=session)
            for entry_id in ids:
                env_text = (Path(tmp) / "work" / "work" / entry_id / "bundle" / "dist" / "env.txt").read_text()
                self.assertNotIn("SYNOS_CONTAINER_ROOT", env_text)

        self.assertEqual(2, len(report["targets"]))

    def test_an_explicit_container_root_is_shared_by_every_worker(self) -> None:
        ids = ["web-server-nginx", "web-server-apache"]
        catalog = real_catalog()
        entries = [entry_by_id(catalog, i) for i in ids]
        sub_catalog = {"bundle_catalog": entries}
        session = FakeSession(archives_by_id={i: self._archive_for(i) for i in ids})

        with tempfile.TemporaryDirectory() as tmp:
            config = cc.Config(catalog_url="http://x", workdir=Path(tmp) / "work", site_url="http://fake", smoke=False,
                               container_root="/mnt/big/synos-storage")
            cc.run_build(config, catalog=sub_catalog, max_builds=None, jobs=2, browser_factory=session)
            for entry_id in ids:
                env_text = (Path(tmp) / "work" / "work" / entry_id / "bundle" / "dist" / "env.txt").read_text()
                self.assertIn("SYNOS_CONTAINER_ROOT=/mnt/big/synos-storage", env_text)

    def test_resolve_jobs_is_wired_to_host_resources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = cc.Config(catalog_url="http://x", workdir=Path(tmp) / "work", jobs="auto")
            jobs, problems = cc.resolve_jobs(config)
        self.assertEqual([], problems)
        self.assertGreaterEqual(jobs, 0)

    def test_resolve_jobs_passes_container_root_to_host_resources(self) -> None:
        original = cc.host_resources.resolve_jobs
        captured: dict = {}

        def fake_resolve_jobs(requested, root, **kwargs):
            captured.update(kwargs)
            return 2, []

        cc.host_resources.resolve_jobs = fake_resolve_jobs
        try:
            with tempfile.TemporaryDirectory() as tmp:
                config = cc.Config(catalog_url="http://x", workdir=Path(tmp) / "work", jobs="auto",
                                   container_root="/mnt/big/synos-storage")
                cc.resolve_jobs(config)
        finally:
            cc.host_resources.resolve_jobs = original
        self.assertEqual("/mnt/big/synos-storage", captured.get("container_root"))


class TrackedFileIsolationTests(unittest.TestCase):
    """tools/catalog_conformance.py never calls tools/synos any more, so it
    has no engine checkout to accidentally apply a bundle into — but it
    still must never write anything into this checkout. Proven through the
    real CLI entry point, the same way tests/unit/test_build_matrix.py's
    own IsolationTests does for build_matrix.py."""

    def test_a_full_build_run_through_main_touches_no_tracked_file(self) -> None:
        catalog = real_catalog()
        entry = entry_by_id(catalog, "web-server-nginx")
        fake_catalog = {"bundle_catalog": [entry]}
        before_status = _git_status(ROOT)
        before_worktrees = _git_worktree_list(ROOT)

        raw = make_bundle_archive(entry_id="web-server-nginx")
        session = FakeSession(default_archive=raw)

        original_fetch = cc.fetch_catalog
        cc.fetch_catalog = lambda url, **kwargs: fake_catalog
        original_container_store = cc.host_resources.container_store
        cc.host_resources.container_store = lambda engine, container_root=None: None
        original_studio_session = cc.devtools_browser.StudioSession
        cc.devtools_browser.StudioSession = lambda *a, **k: session
        try:
            with tempfile.TemporaryDirectory() as tmp:
                config_path = Path(tmp) / "conformance.yml"
                config_path.write_text(f"catalog_url: http://x\nsite_url: http://fake\nworkdir: {tmp}/work\nsmoke: false\n",
                                       encoding="utf-8")
                code = cc.main(["build", "--config", str(config_path)])
                self.assertTrue((Path(tmp) / "work" / "build-report.json").is_file())
        finally:
            cc.fetch_catalog = original_fetch
            cc.host_resources.container_store = original_container_store
            cc.devtools_browser.StudioSession = original_studio_session

        self.assertEqual(0, code)
        self.assertEqual(before_status, _git_status(ROOT), "a conformance run must leave every tracked file exactly as it found it")
        self.assertEqual(before_worktrees, _git_worktree_list(ROOT))

    def test_build_without_site_url_is_refused_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "conformance.yml"
            config_path.write_text(f"catalog_url: http://x\nworkdir: {tmp}/work\n", encoding="utf-8")
            original_fetch = cc.fetch_catalog
            cc.fetch_catalog = lambda url, **kwargs: {"bundle_catalog": []}
            try:
                code = cc.main(["build", "--config", str(config_path)])
            finally:
                cc.fetch_catalog = original_fetch
        self.assertEqual(2, code)


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

    def test_a_skipped_entrys_reason_is_printed_not_only_recorded(self) -> None:
        report = {"mode": "build", "catalog_url": "http://x", "targets": {
            "a": {"status": "skipped", "log_tail": ["no browser available to drive the page: no chrome"]},
            "b": {"status": "success", "log_tail": []},
        }}
        diff = {"newly_failed": [], "recovered": [], "still_failing": [], "unchanged": []}
        text = cc.summary_text(report, diff)
        self.assertIn("skipped a: no browser available to drive the page: no chrome", text)


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

    def test_dry_run_with_only_lists_exactly_that_entry(self) -> None:
        fake_catalog = {"bundle_catalog": [{"id": "a"}, {"id": "b"}, {"id": "c"}]}
        original = cc.fetch_catalog
        cc.fetch_catalog = lambda url, **kwargs: fake_catalog
        try:
            with tempfile.TemporaryDirectory() as tmp:
                config_path = Path(tmp) / "c.yml"
                config_path.write_text(f"catalog_url: http://x\nworkdir: {tmp}/work\n", encoding="utf-8")
                code = cc.main(["build", "--config", str(config_path), "--dry-run", "--only", "b"])
        finally:
            cc.fetch_catalog = original
        self.assertEqual(0, code)


if __name__ == "__main__":
    unittest.main()
