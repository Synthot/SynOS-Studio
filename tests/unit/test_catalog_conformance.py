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


def archive_with_files(entry_id: str, **kwargs) -> bytes:
    """make_bundle_archive(), but bundle.json carries a real "files" list —
    the manifest and profile it actually ships — the shape a real download
    has (tools/export_catalog.py's bundle_catalog(), whose own schema
    comment says "every file in the bundle except bundle.json itself").
    make_bundle_archive()'s own default bundle.json omits "files" entirely,
    which is fine for every test that does not care; --shared-workdir's own
    clear_shared_folder() acts on exactly that list, so tests for it need
    the real shape."""
    files_list = [f"manifests/{entry_id}.yml", f"profiles/{kwargs.get('profile') or entry_id}.yml"]
    bundle_json = json.dumps({"format": 1, "name": entry_id, "manifest": f"manifests/{entry_id}.yml",
                              "files": files_list})
    extra_files = dict(kwargs.pop("extra_files", None) or {})
    extra_files["bundle.json"] = bundle_json
    return make_bundle_archive(entry_id=entry_id, extra_files=extra_files, **kwargs)


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

    def test_a_smoke_test_failure_is_recorded_as_its_own_smoke_failed_status(self) -> None:
        """Item 110: a build that succeeded but whose boot check did not is
        its own named outcome, "smoke_failed" - never left as "success"
        (which is what let cleanup wrongly delete the only evidence for
        the thing that actually failed, item 109)."""
        raw = make_bundle_archive(entry_id="web-server-nginx")
        session = FakeSession(default_archive=raw)
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(tmp, smoke=True)
            old_smoke_run = cc.smoke_test.run
            cc.smoke_test.run = lambda *a, **k: {"status": "failed", "checks": [{"name": "open-ports", "passed": False}]}
            try:
                record = cc.build_one({"id": "web-server-nginx"}, config=config, work_dir=Path(tmp) / "work" / "t",
                                      browser_factory=session)
            finally:
                cc.smoke_test.run = old_smoke_run
        self.assertEqual("smoke_failed", record["status"])
        self.assertEqual(cc.STAGE_SMOKE, record["stage"])
        self.assertNotIn(record["status"], cc.OK_STATUSES)
        self.assertIn("open-ports", record["log_tail"][0])
        # item 109: cleanup never ran for this - the whole bundle directory
        # (evidence for the only thing that failed) survives
        self.assertFalse(record["cleanup"]["performed"])

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
            config = cc.Config(catalog_url="http://x", workdir=Path(tmp) / "work", site_url="http://fake", smoke=False, cleanup=False)
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
                               cleanup=False,
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
            config = cc.Config(catalog_url="http://x", workdir=Path(tmp) / "work", site_url="http://fake", smoke=False, cleanup=False)
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


class BaseVariantTests(unittest.TestCase):
    """Item 112: covering an entry on more than the base its manifest
    pins - a config key/flag naming which bases, defaulting to the
    entry's own (no override at all, today's behavior, unchanged)."""

    def test_cover_bases_unset_behaves_exactly_like_before_plain_entry_id_key(self) -> None:
        entry_id = "web-server-nginx"
        catalog = real_catalog()
        session = FakeSession(default_archive=make_bundle_archive(entry_id=entry_id, base="ubuntu", suite="noble"))
        with tempfile.TemporaryDirectory() as tmp:
            config = cc.Config(catalog_url="http://x", workdir=Path(tmp) / "work", site_url="http://fake", smoke=False)
            report = cc.run_build(config, catalog=catalog, only=[entry_id], browser_factory=session)
        self.assertEqual([entry_id], list(report["targets"]))
        self.assertEqual("ubuntu", report["targets"][entry_id]["base"])

    def test_cover_bases_builds_the_entry_once_per_named_base(self) -> None:
        entry_id = "web-server-nginx"
        catalog = real_catalog()
        session = FakeSession(default_archive=make_bundle_archive(entry_id=entry_id, base="ubuntu", suite="noble"))
        with tempfile.TemporaryDirectory() as tmp:
            config = cc.Config(catalog_url="http://x", workdir=Path(tmp) / "work", site_url="http://fake", smoke=False,
                               cover_bases=["ubuntu", "debian"])
            report = cc.run_build(config, catalog=catalog, only=[entry_id], browser_factory=session)
        self.assertEqual({f"{entry_id}@ubuntu", f"{entry_id}@debian"}, set(report["targets"]))
        self.assertEqual("ubuntu", report["targets"][f"{entry_id}@ubuntu"]["base"])
        self.assertEqual("noble", report["targets"][f"{entry_id}@ubuntu"]["suite"])
        self.assertEqual("debian", report["targets"][f"{entry_id}@debian"]["base"])
        self.assertEqual("trixie", report["targets"][f"{entry_id}@debian"]["suite"])
        self.assertEqual("success", report["targets"][f"{entry_id}@ubuntu"]["status"])
        self.assertEqual("success", report["targets"][f"{entry_id}@debian"]["status"])

    def test_cover_bases_records_two_separate_bases_in_build_status_json(self) -> None:
        entry_id = "web-server-nginx"
        catalog = real_catalog()
        session = FakeSession(default_archive=make_bundle_archive(entry_id=entry_id, base="ubuntu", suite="noble"))
        with tempfile.TemporaryDirectory() as tmp:
            config = cc.Config(catalog_url="http://x", workdir=Path(tmp) / "work", site_url="http://fake", smoke=False,
                               cover_bases=["ubuntu", "debian"])
            cc.run_build(config, catalog=catalog, only=[entry_id], browser_factory=session)
            status = json.loads((Path(tmp) / "work" / "build-status.json").read_text(encoding="utf-8"))
        bases = status["entries"][entry_id]["bases"]
        self.assertEqual({"ubuntu", "debian"}, set(bases))
        self.assertEqual("success", bases["ubuntu"]["state"])
        self.assertEqual("noble", bases["ubuntu"]["suite"])
        self.assertEqual("success", bases["debian"]["state"])
        self.assertEqual("trixie", bases["debian"]["suite"])

    def test_leaves_everything_but_base_and_suite_alone(self) -> None:
        """Item 112: "change only the base and its default release, leave
        everything else the bundle carries alone" - the profile/name/arch
        the manifest also carries survive an override untouched."""
        entry_id = "web-server-nginx"
        catalog = real_catalog()
        session = FakeSession(default_archive=make_bundle_archive(entry_id=entry_id, base="ubuntu", suite="noble"))
        with tempfile.TemporaryDirectory() as tmp:
            config = cc.Config(catalog_url="http://x", workdir=Path(tmp) / "work", site_url="http://fake",
                               smoke=False, cleanup=False, cover_bases=["debian"])
            cc.run_build(config, catalog=catalog, only=[entry_id], browser_factory=session)
            manifest_path = Path(tmp) / "work" / "work" / f"{entry_id}@debian" / "bundle" / "manifests" / f"{entry_id}.yml"
            text = manifest_path.read_text(encoding="utf-8")
        self.assertIn("base: debian", text)
        self.assertIn("suite: trixie", text)
        self.assertIn(f"name: {entry_id}", text)  # untouched
        self.assertIn("profile: web-server-nginx", text)  # untouched (make_bundle_archive's own default)

    def test_covering_an_unknown_base_is_refused_by_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.yml"
            path.write_text(f"catalog_url: http://x\nworkdir: {tmp}/work\ncover_bases: [ubuntu, arch]\n",
                            encoding="utf-8")
            with self.assertRaises(cc.ConformanceError):
                cc.Config.load(path)

    def test_covering_an_unknown_base_is_refused_when_the_base_has_no_adapter_at_build_time(self) -> None:
        """Belt and suspenders under run_build() itself, in case a caller
        builds a Config directly (bypassing Config.load()'s own check)."""
        entry_id = "web-server-nginx"
        catalog = real_catalog()
        session = FakeSession(default_archive=make_bundle_archive(entry_id=entry_id, base="ubuntu", suite="noble"))
        with tempfile.TemporaryDirectory() as tmp:
            config = cc.Config(catalog_url="http://x", workdir=Path(tmp) / "work", site_url="http://fake",
                               smoke=False, cover_bases=["not-a-real-base"])
            report = cc.run_build(config, catalog=catalog, only=[entry_id], browser_factory=session)
        result = report["targets"][f"{entry_id}@not-a-real-base"]
        self.assertNotEqual("success", result["status"])
        self.assertIn("not-a-real-base", result["log_tail"][0])

    def test_the_cli_cover_bases_flag_overrides_the_config_files_own_setting(self) -> None:
        entry_id = "web-server-nginx"
        catalog = real_catalog()
        entry = entry_by_id(catalog, entry_id)
        fake_catalog = {"bundle_catalog": [entry], "defaults": catalog.get("defaults", {})}
        session = FakeSession(default_archive=make_bundle_archive(entry_id=entry_id, base="ubuntu", suite="noble"))

        original_fetch = cc.fetch_catalog
        cc.fetch_catalog = lambda url, **kwargs: fake_catalog
        original_container_store = cc.host_resources.container_store
        cc.host_resources.container_store = lambda engine, container_root=None: None
        original_studio_session = cc.devtools_browser.StudioSession
        cc.devtools_browser.StudioSession = lambda *a, **k: session
        try:
            with tempfile.TemporaryDirectory() as tmp:
                work = Path(tmp) / "work"
                config_path = Path(tmp) / "c.yml"
                config_path.write_text(f"catalog_url: http://x\nworkdir: {work}\nsite_url: http://fake\nsmoke: false\n",
                                       encoding="utf-8")
                code = cc.main(["build", "--config", str(config_path), "--only", entry_id,
                               "--cover-bases", "ubuntu,debian"])
                report = json.loads((work / "build-report.json").read_text(encoding="utf-8"))
        finally:
            cc.fetch_catalog = original_fetch
            cc.host_resources.container_store = original_container_store
            cc.devtools_browser.StudioSession = original_studio_session
        self.assertEqual(0, code)
        self.assertEqual({f"{entry_id}@ubuntu", f"{entry_id}@debian"}, set(report["targets"]))

    def test_an_unknown_base_on_the_cli_is_refused_cleanly(self) -> None:
        original = cc.fetch_catalog
        cc.fetch_catalog = lambda url, **kwargs: {"bundle_catalog": []}
        try:
            with tempfile.TemporaryDirectory() as tmp:
                config_path = Path(tmp) / "c.yml"
                config_path.write_text(f"catalog_url: http://x\nworkdir: {tmp}/work\nsite_url: http://fake\n",
                                       encoding="utf-8")
                code = cc.main(["build", "--config", str(config_path), "--cover-bases", "arch"])
        finally:
            cc.fetch_catalog = original
        self.assertEqual(2, code)


class ResolvedConfigurationReachesSmokeTestTests(unittest.TestCase):
    """Item 108: the launcher's own dist/<name>.resolved.json (or a lack of
    one) reaches tools/smoke_test.py as expected_distro_name, so the
    graphical boot check can certify the name it promised instead of
    having nothing to check against."""

    FAKE_BUILD_SH_WITH_RESOLVED_CONFIG = """#!/bin/bash
mkdir -p dist
echo "log" > dist/build.log
echo "FAKE-ISO-BYTES" > dist/fake.iso
cat > dist/fake.resolved.json <<'EOF'
{"brand": {"id": "synosnginx", "display_name": "SynOS Nginx"}}
EOF
exit 0
"""

    def _capture_smoke_run(self):
        captured = {}

        def fake_run(iso_path, resolved_profile, **kwargs):
            captured["expected_distro_name"] = kwargs.get("expected_distro_name")
            captured["output_dir"] = kwargs.get("output_dir")
            return {"status": "passed", "checks": [{"name": "default-target", "passed": True}]}

        return captured, fake_run

    def test_a_resolved_configs_brand_reaches_smoke_test_as_expected_distro_name(self) -> None:
        entry_id = "web-server-nginx"
        raw = make_bundle_archive(entry_id=entry_id, build_sh=self.FAKE_BUILD_SH_WITH_RESOLVED_CONFIG)
        session = FakeSession(default_archive=raw)
        captured, fake_run = self._capture_smoke_run()
        original = cc.smoke_test.run
        cc.smoke_test.run = fake_run
        try:
            with tempfile.TemporaryDirectory() as tmp:
                config = cc.Config(catalog_url="http://x", workdir=Path(tmp) / "work", site_url="http://fake",
                                   smoke=True, cleanup=False)
                result = cc.build_one({"id": entry_id}, config=config, work_dir=Path(tmp) / "work" / "t",
                                      browser_factory=session)
                self.assertIsNotNone(result["resolved_config_path"])
                self.assertTrue(Path(result["resolved_config_path"]).exists())
        finally:
            cc.smoke_test.run = original
        self.assertEqual("SynOS Nginx", captured["expected_distro_name"])

    def test_no_resolved_config_means_expected_distro_name_is_none_not_invented(self) -> None:
        entry_id = "web-server-nginx"
        raw = make_bundle_archive(entry_id=entry_id, build_sh=FAKE_BUILD_SH_SUCCESS)
        session = FakeSession(default_archive=raw)
        captured, fake_run = self._capture_smoke_run()
        original = cc.smoke_test.run
        cc.smoke_test.run = fake_run
        try:
            with tempfile.TemporaryDirectory() as tmp:
                config = cc.Config(catalog_url="http://x", workdir=Path(tmp) / "work", site_url="http://fake",
                                   smoke=True, cleanup=False)
                result = cc.build_one({"id": entry_id}, config=config, work_dir=Path(tmp) / "work" / "t",
                                      browser_factory=session)
        finally:
            cc.smoke_test.run = original
        self.assertIsNone(captured["expected_distro_name"])
        self.assertIsNone(result["resolved_config_path"])

    def test_the_resolved_config_is_kept_even_when_the_build_fails(self) -> None:
        """Item 109: kept alongside the log and the serial transcript in
        every case, not only on success."""
        entry_id = "web-server-nginx"
        fail_and_write_resolved = self.FAKE_BUILD_SH_WITH_RESOLVED_CONFIG.replace("exit 0", "exit 3")
        raw = make_bundle_archive(entry_id=entry_id, build_sh=fail_and_write_resolved)
        session = FakeSession(default_archive=raw)
        with tempfile.TemporaryDirectory() as tmp:
            config = cc.Config(catalog_url="http://x", workdir=Path(tmp) / "work", site_url="http://fake",
                               smoke=False, cleanup=True)
            result = cc.build_one({"id": entry_id}, config=config, work_dir=Path(tmp) / "work" / "t",
                                  browser_factory=session)
            self.assertNotEqual("success", result["status"])
            self.assertIsNotNone(result["resolved_config_path"])
            self.assertTrue(Path(result["resolved_config_path"]).exists())


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
            config = cc.Config(catalog_url="http://x", workdir=Path(tmp) / "work", site_url="http://fake", smoke=False,
                               cleanup=False)
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
            config = cc.Config(catalog_url="http://x", workdir=Path(tmp) / "work", site_url="http://fake", smoke=False,
                               cleanup=False)
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
                               cleanup=False, container_root="/mnt/big/synos-storage")
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
    def test_no_previous_report_and_a_successful_entry_is_not_newly_failed(self) -> None:
        """Item 110's own bug: a first-ever run (no previous report on
        disk at all) used to mark *every* entry "newly_failed"
        unconditionally, regardless of status - "1 success" printed right
        next to "newly failing: <that same entry>". A first-ever success
        is "unchanged" (nothing to compare against, and it is fine), not
        a failure that just started."""
        current = {"targets": {"a": {"status": "success"}}}
        diff = cc.diff_reports(None, current)
        self.assertEqual([], diff["newly_failed"])
        self.assertEqual(["a"], diff["unchanged"])

    def test_no_previous_report_and_a_failed_entry_is_newly_failed(self) -> None:
        current = {"targets": {"a": {"status": "build_failed"}}}
        diff = cc.diff_reports(None, current)
        self.assertEqual(["a"], diff["newly_failed"])

    def test_no_previous_report_treats_smoke_failed_the_same_as_any_other_failure(self) -> None:
        current = {"targets": {"a": {"status": "smoke_failed"}}}
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


class BuildStatusWiringTests(unittest.TestCase):
    """Items 71-73: run_build's own use of tools/build_status.py and
    tools/status_uploader.py — the small public build-status.json, written
    after every entry, uploaded (through a fake transport; never a real
    network call) only when an entry's state changed, plus once more,
    unconditionally, when the run finishes."""

    def test_build_status_json_is_written_with_the_expected_entries(self) -> None:
        catalog = real_catalog()
        raw = make_bundle_archive(entry_id="placeholder")
        session = FakeSession(default_archive=raw)
        with tempfile.TemporaryDirectory() as tmp:
            config = cc.Config(catalog_url="http://x", workdir=Path(tmp) / "work", site_url="http://fake", smoke=False)
            cc.run_build(config, catalog=catalog, only=["web-server-nginx", "git-server"], browser_factory=session)
            status_path = Path(tmp) / "work" / "build-status.json"
            status = json.loads(status_path.read_text(encoding="utf-8"))
        self.assertEqual(cc.build_status.SCHEMA_VERSION, status["schema_version"])
        self.assertEqual({"web-server-nginx", "git-server"}, set(status["entries"]))
        nginx_ubuntu = status["entries"]["web-server-nginx"]["bases"]["ubuntu"]
        self.assertEqual("success", nginx_ubuntu["state"])
        self.assertIsNotNone(nginx_ubuntu["checksum"])

    def test_a_failed_entry_is_recorded_with_state_failed(self) -> None:
        catalog = real_catalog()
        entry_id = "web-server-nginx"
        raw = make_bundle_archive(entry_id=entry_id, build_sh=FAKE_BUILD_SH_ENGINE_FAIL)
        session = FakeSession(default_archive=raw)
        with tempfile.TemporaryDirectory() as tmp:
            config = cc.Config(catalog_url="http://x", workdir=Path(tmp) / "work", site_url="http://fake", smoke=False)
            cc.run_build(config, catalog=catalog, only=[entry_id], browser_factory=session)
            status = json.loads((Path(tmp) / "work" / "build-status.json").read_text(encoding="utf-8"))
        self.assertEqual("failed", status["entries"][entry_id]["bases"]["ubuntu"]["state"])

    def test_upload_is_called_for_every_live_transition_plus_once_at_the_end(self) -> None:
        """Item 93: each entry moves queued -> testing -> a real outcome,
        and each of those three live-state writes is itself a change worth
        uploading (a run in progress must not look stale) - so two brand
        new entries produce 2*3 = 6 change-triggered uploads, plus one
        more, unconditionally, at the end of the run."""
        ids = ["web-server-nginx", "git-server"]
        catalog = real_catalog()
        session = FakeSession(archives_by_id={i: make_bundle_archive(entry_id=i) for i in ids})
        calls: list = []
        upload_config = cc.status_uploader.UploadConfig(protocol="sftp", host="h", remote_path="/p", retries=1)
        with tempfile.TemporaryDirectory() as tmp:
            config = cc.Config(catalog_url="http://x", workdir=Path(tmp) / "work", site_url="http://fake", smoke=False)
            report = cc.run_build(config, catalog=catalog, only=ids, browser_factory=session,
                                  upload_config=upload_config,
                                  upload_transport=lambda c, p: calls.append(p), upload_sleeper=lambda s: None)
        self.assertEqual(7, len(calls))
        self.assertNotIn("upload_warnings", report)

    def test_an_entry_whose_final_outcome_did_not_change_still_uploads_its_live_transitions(self) -> None:
        """An entry that was already recorded as "success" still moves
        through "queued" and "testing" for real on this run - both are
        live-progress changes item 93 wants visible - but the *final*
        record() call, seeing the outcome is still "success" same as
        history, is the one call item 73's original "not every entry"
        intent skips. So 2 (queue, start) + 1 (end-of-run) = 3, never a
        3rd change-triggered upload for the repeated success."""
        entry_id = "web-server-nginx"
        catalog = real_catalog()
        session = FakeSession(default_archive=make_bundle_archive(entry_id=entry_id))
        calls: list = []
        upload_config = cc.status_uploader.UploadConfig(protocol="sftp", host="h", remote_path="/p", retries=1)
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "work"
            work.mkdir(parents=True)
            pre = cc.build_status.empty_status()
            pre, _ = cc.build_status.apply_result(pre, entry_id, "ubuntu", {
                "id": entry_id, "status": "success", "engine": "0.2.0", "base": "ubuntu", "suite": "noble",
                "end": "2020-01-01T00:00:00+00:00", "duration_s": 1.0, "stage": None,
                "iso": {"size": 1, "sha256": "x"}, "smoke": {"status": "passed"}, "log_tail": []})
            cc.build_status.write_status_atomic(work / "build-status.json", pre)

            config = cc.Config(catalog_url="http://x", workdir=work, site_url="http://fake", smoke=False)
            cc.run_build(config, catalog=catalog, only=[entry_id], browser_factory=session,
                        upload_config=upload_config,
                        upload_transport=lambda c, p: calls.append(p), upload_sleeper=lambda s: None)
        self.assertEqual(3, len(calls))

    def test_a_failing_upload_is_a_warning_never_a_failed_build(self) -> None:
        entry_id = "web-server-nginx"
        catalog = real_catalog()
        session = FakeSession(default_archive=make_bundle_archive(entry_id=entry_id))
        upload_config = cc.status_uploader.UploadConfig(protocol="sftp", host="h", remote_path="/p", retries=1)

        def always_fails(config, path):
            raise RuntimeError("network unreachable")

        with tempfile.TemporaryDirectory() as tmp:
            config = cc.Config(catalog_url="http://x", workdir=Path(tmp) / "work", site_url="http://fake", smoke=False)
            report = cc.run_build(config, catalog=catalog, only=[entry_id], browser_factory=session,
                                  upload_config=upload_config,
                                  upload_transport=always_fails, upload_sleeper=lambda s: None)
        self.assertEqual("success", report["targets"][entry_id]["status"])
        self.assertGreaterEqual(len(report.get("upload_warnings", [])), 1)

    def test_dry_run_upload_never_calls_the_transport(self) -> None:
        entry_id = "web-server-nginx"
        catalog = real_catalog()
        session = FakeSession(default_archive=make_bundle_archive(entry_id=entry_id))
        calls: list = []
        upload_config = cc.status_uploader.UploadConfig(protocol="sftp", host="h", remote_path="/p", retries=1)
        with tempfile.TemporaryDirectory() as tmp:
            config = cc.Config(catalog_url="http://x", workdir=Path(tmp) / "work", site_url="http://fake", smoke=False)
            cc.run_build(config, catalog=catalog, only=[entry_id], browser_factory=session,
                        upload_config=upload_config, dry_run_upload=True,
                        upload_transport=lambda c, p: calls.append(p), upload_sleeper=lambda s: None)
        self.assertEqual([], calls)

    def test_no_upload_config_means_no_status_file_upload_attempt_at_all(self) -> None:
        entry_id = "web-server-nginx"
        catalog = real_catalog()
        session = FakeSession(default_archive=make_bundle_archive(entry_id=entry_id))
        with tempfile.TemporaryDirectory() as tmp:
            config = cc.Config(catalog_url="http://x", workdir=Path(tmp) / "work", site_url="http://fake", smoke=False)
            report = cc.run_build(config, catalog=catalog, only=[entry_id], browser_factory=session)
        self.assertNotIn("upload_warnings", report)

    def test_credentials_never_appear_in_the_report(self) -> None:
        entry_id = "web-server-nginx"
        catalog = real_catalog()
        session = FakeSession(default_archive=make_bundle_archive(entry_id=entry_id))
        secret = "hunter2-super-secret"
        upload_config = cc.status_uploader.UploadConfig(protocol="sftp", host="h", remote_path="/p",
                                                        username="u", password=secret, retries=1)

        def always_fails(config, path):
            raise RuntimeError(f"auth failed with password={secret}")

        with tempfile.TemporaryDirectory() as tmp:
            config = cc.Config(catalog_url="http://x", workdir=Path(tmp) / "work", site_url="http://fake", smoke=False)
            report = cc.run_build(config, catalog=catalog, only=[entry_id], browser_factory=session,
                                  upload_config=upload_config,
                                  upload_transport=always_fails, upload_sleeper=lambda s: None)
        self.assertNotIn(secret, json.dumps(report))

    def test_a_run_resolves_an_entry_left_testing_by_a_previous_interrupted_run(self) -> None:
        entry_id = "web-server-nginx"
        catalog = real_catalog()
        session = FakeSession(default_archive=make_bundle_archive(entry_id=entry_id))
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "work"
            work.mkdir(parents=True)
            stuck, _ = cc.build_status.mark_testing(cc.build_status.empty_status(), "some-other-entry", "ubuntu")
            cc.build_status.write_status_atomic(work / "build-status.json", stuck)

            config = cc.Config(catalog_url="http://x", workdir=work, site_url="http://fake", smoke=False)
            report = cc.run_build(config, catalog=catalog, only=[entry_id], browser_factory=session)
            status = json.loads((work / "build-status.json").read_text(encoding="utf-8"))
        self.assertEqual(["some-other-entry@ubuntu"], report.get("resolved_interrupted"))
        stuck_record = status["entries"]["some-other-entry"]["bases"]["ubuntu"]
        self.assertEqual("failed", stuck_record["state"])
        self.assertEqual(cc.build_status.INTERRUPTED_ERROR, stuck_record["error"])

    def test_ftp_without_allow_insecure_is_refused_by_the_cli(self) -> None:
        original = cc.fetch_catalog
        cc.fetch_catalog = lambda url, **kwargs: {"bundle_catalog": []}
        try:
            with tempfile.TemporaryDirectory() as tmp:
                config_path = Path(tmp) / "c.yml"
                config_path.write_text(
                    f"catalog_url: http://x\nworkdir: {tmp}/work\nsite_url: http://fake\n"
                    "upload:\n  protocol: ftp\n  host: h\n  remote_path: /p\n", encoding="utf-8")
                code = cc.main(["build", "--config", str(config_path)])
        finally:
            cc.fetch_catalog = original
        self.assertEqual(2, code)


class EntryCleanupWiringTests(unittest.TestCase):
    """Items 87-92: run_build's own use of tools/entry_cleanup.py - freeing
    a successful entry's heavy directories as it finishes, keeping a
    failed or skipped one untouched, the disabling flag, and refusing a
    dangerous workdir."""

    def test_a_successful_entrys_bundle_and_iso_are_gone_but_evidence_remains(self) -> None:
        entry_id = "web-server-nginx"
        catalog = real_catalog()
        session = FakeSession(default_archive=make_bundle_archive(entry_id=entry_id))
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "work"
            config = cc.Config(catalog_url="http://x", workdir=work, site_url="http://fake", smoke=False)
            report = cc.run_build(config, catalog=catalog, only=[entry_id], browser_factory=session,
                                  container_engine=lambda: None)
            entry_work_dir = work / "work" / entry_id
            result = report["targets"][entry_id]
            self.assertEqual("success", result["status"])
            self.assertTrue(result["cleanup"]["performed"])
            self.assertFalse((entry_work_dir / "bundle").exists(), "the unpacked bundle must be gone")
            self.assertTrue((entry_work_dir / "output").exists(), "the small output directory itself stays")
            # the evidence: build log present (possibly gzipped), and the
            # checksum/size already recorded in the result whether or not the
            # multi-GB ISO bytes themselves survive
            self.assertIsNotNone(result["iso"]["sha256"])
            self.assertIsNotNone(result["iso"]["size"])
            log_files = list((entry_work_dir / "output").glob("build.log*"))
            self.assertTrue(log_files, "the build log (or its .gz) must survive cleanup")

    def test_a_failed_entrys_directory_is_left_completely_untouched(self) -> None:
        entry_id = "web-server-nginx"
        catalog = real_catalog()
        raw = make_bundle_archive(entry_id=entry_id, build_sh=FAKE_BUILD_SH_ENGINE_FAIL)
        session = FakeSession(default_archive=raw)
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "work"
            config = cc.Config(catalog_url="http://x", workdir=work, site_url="http://fake", smoke=False)
            report = cc.run_build(config, catalog=catalog, only=[entry_id], browser_factory=session,
                                  container_engine=lambda: None)
            entry_work_dir = work / "work" / entry_id
            result = report["targets"][entry_id]
            self.assertNotEqual("success", result["status"])
            self.assertFalse(result["cleanup"]["performed"])
            self.assertTrue((entry_work_dir / "bundle").exists(), "a failed entry keeps its whole bundle directory")
            self.assertTrue((entry_work_dir / "bundle" / "dist" / "build.log").exists())

    def test_the_cleanup_config_key_set_false_keeps_everything(self) -> None:
        entry_id = "web-server-nginx"
        catalog = real_catalog()
        session = FakeSession(default_archive=make_bundle_archive(entry_id=entry_id))
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "work"
            config = cc.Config(catalog_url="http://x", workdir=work, site_url="http://fake", smoke=False, cleanup=False)
            report = cc.run_build(config, catalog=catalog, only=[entry_id], browser_factory=session,
                                  container_engine=lambda: None)
            entry_work_dir = work / "work" / entry_id
            result = report["targets"][entry_id]
            self.assertEqual("success", result["status"])
            self.assertFalse(result["cleanup"]["performed"])
            self.assertEqual("cleanup is disabled", result["cleanup"]["reason"])
            self.assertTrue((entry_work_dir / "bundle").exists())
            self.assertTrue((entry_work_dir / "bundle" / "dist" / "fake.iso").exists())

    def test_the_no_cleanup_cli_flag_keeps_everything_even_though_the_config_defaults_to_cleaning_up(self) -> None:
        entry_id = "web-server-nginx"
        catalog = real_catalog()
        entry = entry_by_id(catalog, entry_id)
        fake_catalog = {"bundle_catalog": [entry]}
        session = FakeSession(default_archive=make_bundle_archive(entry_id=entry_id))

        original_fetch = cc.fetch_catalog
        cc.fetch_catalog = lambda url, **kwargs: fake_catalog
        original_container_store = cc.host_resources.container_store
        cc.host_resources.container_store = lambda engine, container_root=None: None
        original_studio_session = cc.devtools_browser.StudioSession
        cc.devtools_browser.StudioSession = lambda *a, **k: session
        try:
            with tempfile.TemporaryDirectory() as tmp:
                work = Path(tmp) / "work"
                config_path = Path(tmp) / "c.yml"
                config_path.write_text(
                    f"catalog_url: http://x\nworkdir: {work}\nsite_url: http://fake\nsmoke: false\n",
                    encoding="utf-8")
                code = cc.main(["build", "--config", str(config_path), "--only", entry_id, "--no-cleanup"])
                entry_work_dir = work / "work" / entry_id
                self.assertEqual(0, code)
                self.assertTrue((entry_work_dir / "bundle").exists())
        finally:
            cc.fetch_catalog = original_fetch
            cc.host_resources.container_store = original_container_store
            cc.devtools_browser.StudioSession = original_studio_session

    def test_run_build_refuses_a_home_directory_workdir(self) -> None:
        catalog = {"bundle_catalog": []}
        config = cc.Config(catalog_url="http://x", workdir=Path.home(), site_url="http://fake")
        with self.assertRaises(cc.ConformanceError):
            cc.run_build(config, catalog=catalog, browser_factory=FakeSession())

    def test_run_build_refuses_a_workdir_that_looks_like_a_checkout(self) -> None:
        catalog = {"bundle_catalog": []}
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp) / "some-checkout"
            (checkout / ".git").mkdir(parents=True)
            config = cc.Config(catalog_url="http://x", workdir=checkout, site_url="http://fake")
            with self.assertRaises(cc.ConformanceError):
                cc.run_build(config, catalog=catalog, browser_factory=FakeSession())

    def test_peak_working_directory_usage_across_several_entries_stays_near_one_entrys_worth(self) -> None:
        """Item 89: freed as it goes, not batched to the end - by the time
        entry N+1 starts downloading, entry N's own bundle directory (the
        heavy thing) must already be gone, serial run or not."""
        ids = ["web-server-nginx", "web-server-apache", "git-server"]
        catalog = real_catalog()

        class TrackingSession(FakeSession):
            def __init__(self, *a, work_root: Path, **kw):
                super().__init__(*a, **kw)
                self.work_root = work_root
                self.bundle_dirs_present_at_start: list[int] = []

            def download_bundle(self, entry_id, name=None):
                count = sum(1 for p in self.work_root.glob("*/bundle") if p.is_dir())
                self.bundle_dirs_present_at_start.append(count)
                return super().download_bundle(entry_id, name=name)

        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "work"
            session = TrackingSession(archives_by_id={i: make_bundle_archive(entry_id=i) for i in ids},
                                      work_root=work / "work")
            config = cc.Config(catalog_url="http://x", workdir=work, site_url="http://fake", smoke=False)
            report = cc.run_build(config, catalog=catalog, only=ids, jobs=1, browser_factory=session,
                                  container_engine=lambda: None)
        self.assertTrue(all(r["status"] == "success" for r in report["targets"].values()))
        self.assertEqual([0, 0, 0], session.bundle_dirs_present_at_start,
                         "no previous entry's bundle directory should still exist when the next one starts")


class StorageReclaimTests(unittest.TestCase):
    """Item 91: per-worker podman storage roots this run created, and the
    launcher's own cache volumes, reclaimed at the end of a run - using a
    fake container engine and a fake volume remover throughout; nothing
    here ever shells out to a real podman/docker."""

    def test_worker_storage_roots_this_run_created_are_removed(self) -> None:
        ids = ["web-server-nginx", "web-server-apache"]
        catalog = real_catalog()
        entries = [entry_by_id(catalog, i) for i in ids]
        sub_catalog = {"bundle_catalog": entries}
        session = FakeSession(archives_by_id={i: make_bundle_archive(entry_id=i) for i in ids})
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "work"
            config = cc.Config(catalog_url="http://x", workdir=work, site_url="http://fake", smoke=False)
            report = cc.run_build(config, catalog=sub_catalog, jobs=2, browser_factory=session,
                                  container_engine=lambda: None)
            roots = list((work / "podman-storage").glob("worker-*")) if (work / "podman-storage").exists() else []
        self.assertEqual([], roots, "every worker storage root this run created must be gone")
        self.assertGreaterEqual(len(report["storage_reclaimed"]["worker_roots_removed"]), 1)

    def test_an_explicit_container_root_is_never_touched_by_reclaim(self) -> None:
        entry_id = "web-server-nginx"
        catalog = real_catalog()
        session = FakeSession(default_archive=make_bundle_archive(entry_id=entry_id))
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "work"
            owned = Path(tmp) / "my-own-storage"
            owned.mkdir()
            config = cc.Config(catalog_url="http://x", workdir=work, site_url="http://fake", smoke=False,
                               container_root=str(owned))
            report = cc.run_build(config, catalog=catalog, only=[entry_id], browser_factory=session,
                                  container_engine=lambda: "fake-engine")
            self.assertTrue(owned.is_dir(), "a person's own configured storage is never removed")
        reclaimed = report["storage_reclaimed"]
        self.assertEqual([], reclaimed["worker_roots_removed"])
        self.assertEqual([], reclaimed["cache_volumes_pruned"])

    def test_cache_volumes_for_every_base_and_suite_built_are_pruned_via_the_fake_remover(self) -> None:
        entry_id = "web-server-nginx"
        catalog = real_catalog()
        session = FakeSession(default_archive=make_bundle_archive(entry_id=entry_id, base="ubuntu", suite="noble"))
        removed_calls: list[tuple[str, str]] = []

        def fake_remover(engine: str, volume: str) -> tuple[bool, str]:
            removed_calls.append((engine, volume))
            return True, ""

        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "work"
            config = cc.Config(catalog_url="http://x", workdir=work, site_url="http://fake", smoke=False)
            report = cc.run_build(config, catalog=catalog, only=[entry_id], browser_factory=session,
                                  container_engine=lambda: "fake-engine", volume_remover=fake_remover)
        self.assertEqual([("fake-engine", "synos-cache-ubuntu-noble")], removed_calls)
        self.assertEqual(["synos-cache-ubuntu-noble"], report["storage_reclaimed"]["cache_volumes_pruned"])

    def test_a_volume_the_remover_refuses_is_left_alone_and_reported(self) -> None:
        entry_id = "web-server-nginx"
        catalog = real_catalog()
        session = FakeSession(default_archive=make_bundle_archive(entry_id=entry_id, base="ubuntu", suite="noble"))

        def fake_remover(engine: str, volume: str) -> tuple[bool, str]:
            return False, "volume is in use"

        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "work"
            config = cc.Config(catalog_url="http://x", workdir=work, site_url="http://fake", smoke=False)
            report = cc.run_build(config, catalog=catalog, only=[entry_id], browser_factory=session,
                                  container_engine=lambda: "fake-engine", volume_remover=fake_remover)
        self.assertEqual([], report["storage_reclaimed"]["cache_volumes_pruned"])
        self.assertEqual(1, len(report["storage_reclaimed"]["cache_volumes_left_alone"]))
        self.assertIn("volume is in use", report["storage_reclaimed"]["cache_volumes_left_alone"][0])

    def test_reclaim_is_skipped_entirely_when_cleanup_is_disabled(self) -> None:
        entry_id = "web-server-nginx"
        catalog = real_catalog()
        session = FakeSession(default_archive=make_bundle_archive(entry_id=entry_id))
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "work"
            config = cc.Config(catalog_url="http://x", workdir=work, site_url="http://fake", smoke=False, cleanup=False)
            report = cc.run_build(config, catalog=catalog, only=[entry_id], browser_factory=session,
                                  container_engine=lambda: "fake-engine")
        self.assertNotIn("storage_reclaimed", report)


class SharedWorkdirTests(unittest.TestCase):
    """--shared-workdir (docs/BUILD_MATRIX.md, "One shared build folder per
    pass"): the owner's own proof requirement comes first — a second entry
    must never see a file the first one shipped — plus the
    failure-does-not-poison-the-next-entry and
    engine-source-reuse-is-reported-honestly claims that go with it."""

    def _config(self, tmp, **kwargs) -> "cc.Config":
        kwargs.setdefault("smoke", False)
        return cc.Config(catalog_url="http://x", workdir=Path(tmp) / "work", site_url="http://fake-studio", **kwargs)

    def test_a_second_entry_never_sees_a_file_from_the_first(self) -> None:
        session = FakeSession(archives_by_id={"entry-a": archive_with_files("entry-a"),
                                              "entry-b": archive_with_files("entry-b")})
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(tmp)
            shared_dir = Path(tmp) / "shared"
            result_a = cc.build_one({"id": "entry-a"}, config=config, work_dir=shared_dir, browser_factory=session,
                                    shared=True, target_dir=Path(tmp) / "results-a")
            self.assertEqual("success", result_a["status"])
            self.assertTrue((shared_dir / "bundle" / "profiles" / "entry-a.yml").is_file())
            self.assertTrue((shared_dir / "bundle" / "manifests" / "entry-a.yml").is_file())

            result_b = cc.build_one({"id": "entry-b"}, config=config, work_dir=shared_dir, browser_factory=session,
                                    shared=True, target_dir=Path(tmp) / "results-b")
            self.assertEqual("success", result_b["status"])
            self.assertFalse((shared_dir / "bundle" / "profiles" / "entry-a.yml").exists(),
                             "entry-b must never see entry-a's own profile")
            self.assertFalse((shared_dir / "bundle" / "manifests" / "entry-a.yml").exists(),
                             "entry-b must never see entry-a's own manifest")
            self.assertTrue((shared_dir / "bundle" / "profiles" / "entry-b.yml").is_file())
            self.assertTrue((shared_dir / "bundle" / "manifests" / "entry-b.yml").is_file())
            # each entry's own evidence still lands somewhere per-entry, never
            # inside the folder the next entry is about to clear.
            self.assertEqual(str(Path(tmp) / "results-a" / "build.log"), result_a["log_path"])
            self.assertEqual(str(Path(tmp) / "results-b" / "build.log"), result_b["log_path"])

    def test_a_failed_first_entry_does_not_poison_the_second(self) -> None:
        session = FakeSession(archives_by_id={
            "entry-a": archive_with_files("entry-a", build_sh=FAKE_BUILD_SH_ENGINE_FAIL),
            "entry-b": archive_with_files("entry-b"),
        })
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(tmp)
            shared_dir = Path(tmp) / "shared"
            result_a = cc.build_one({"id": "entry-a"}, config=config, work_dir=shared_dir, browser_factory=session,
                                    shared=True, target_dir=Path(tmp) / "results-a")
            self.assertEqual("build_failed", result_a["status"])
            self.assertTrue((shared_dir / "bundle" / "profiles" / "entry-a.yml").is_file(),
                           "a failed entry keeps its own files exactly where it left them")

            result_b = cc.build_one({"id": "entry-b"}, config=config, work_dir=shared_dir, browser_factory=session,
                                    shared=True, target_dir=Path(tmp) / "results-b")
            self.assertEqual("success", result_b["status"],
                             "the next entry must succeed regardless of why the previous one failed")
            self.assertFalse((shared_dir / "bundle" / "profiles" / "entry-a.yml").exists())
            self.assertTrue((shared_dir / "bundle" / "profiles" / "entry-b.yml").is_file())

    def test_dot_build_survives_across_entries_and_reuse_is_reported_honestly(self) -> None:
        session = FakeSession(archives_by_id={"entry-a": archive_with_files("entry-a"),
                                              "entry-b": archive_with_files("entry-b")})
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(tmp)
            shared_dir = Path(tmp) / "shared"
            result_a = cc.build_one({"id": "entry-a"}, config=config, work_dir=shared_dir, browser_factory=session,
                                    shared=True, target_dir=Path(tmp) / "results-a")
            self.assertFalse(result_a["shared_workdir"]["engine_source_reused"],
                             "a fresh shared folder starts with nothing to reuse")

            # Stand in for what the real launcher's own content-addressed
            # cache would leave behind (tools/bundle_launcher.sh's
            # .build/engine-src/<id> plus its <id>.ok marker,
            # engine_src_is_reusable()) - these fakes stand in for build.sh
            # itself, never for the launcher this mode exists to let reuse
            # its own cache undisturbed.
            engine_src_root = shared_dir / "bundle" / ".build" / "engine-src"
            (engine_src_root / "deadbeef").mkdir(parents=True)
            (engine_src_root / "deadbeef" / "tools").mkdir()
            (engine_src_root / "deadbeef" / "tools" / "synos").write_text("stand-in for a real engine checkout")
            (engine_src_root / "deadbeef.ok").write_text("")

            result_b = cc.build_one({"id": "entry-b"}, config=config, work_dir=shared_dir, browser_factory=session,
                                    shared=True, target_dir=Path(tmp) / "results-b")
            self.assertEqual("success", result_b["status"])
            self.assertTrue(result_b["shared_workdir"]["engine_source_reused"],
                           "the second entry must see the engine source the first left behind")
            self.assertTrue((engine_src_root / "deadbeef" / "tools" / "synos").is_file(),
                           ".build/ must never be touched by the shared clear")
            self.assertTrue((engine_src_root / "deadbeef.ok").is_file())

    def test_clear_shared_folder_removes_declared_files_and_dist_never_dot_build(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle_dir = Path(tmp) / "bundle"
            (bundle_dir / "profiles").mkdir(parents=True)
            (bundle_dir / "manifests").mkdir()
            (bundle_dir / "dist").mkdir()
            (bundle_dir / ".build" / "engine-src").mkdir(parents=True)
            (bundle_dir / "profiles" / "old.yml").write_text("old profile")
            (bundle_dir / "manifests" / "old.yml").write_text("old manifest")
            (bundle_dir / "dist" / "old.iso").write_text("old iso bytes")
            (bundle_dir / ".build" / "engine-src" / "keep-me").write_text("must survive")
            (bundle_dir / "bundle.json").write_text(json.dumps(
                {"format": 1, "name": "old", "manifest": "manifests/old.yml",
                 "files": ["manifests/old.yml", "profiles/old.yml"]}))

            result = cc.clear_shared_folder(bundle_dir)

            self.assertFalse((bundle_dir / "profiles" / "old.yml").exists())
            self.assertFalse((bundle_dir / "manifests" / "old.yml").exists())
            self.assertFalse((bundle_dir / "bundle.json").exists())
            self.assertFalse((bundle_dir / "dist").exists())
            self.assertTrue((bundle_dir / ".build" / "engine-src" / "keep-me").is_file())
            self.assertIn("bundle.json", result["removed"])
            self.assertIn("dist/", result["removed"])
            self.assertIn("manifests/old.yml", result["removed"])
            self.assertIn("profiles/old.yml", result["removed"])

    def test_clear_shared_folder_on_a_fresh_or_missing_folder_is_a_safe_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle_dir = Path(tmp) / "bundle"  # does not exist yet at all
            result = cc.clear_shared_folder(bundle_dir)
        self.assertEqual([], result["removed"])

    def test_run_build_wires_shared_workdir_end_to_end(self) -> None:
        catalog = real_catalog()
        ids = ["web-server-nginx", "git-server"]
        session = FakeSession(archives_by_id={i: archive_with_files(i, base="ubuntu", suite="noble") for i in ids})
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "work"
            config = cc.Config(catalog_url="http://x", workdir=work, site_url="http://fake", smoke=False,
                               shared_workdir=True, cleanup=False)
            report = cc.run_build(config, catalog=catalog, only=ids, browser_factory=session,
                                  container_engine=lambda: None)
            self.assertEqual(set(ids), set(report["targets"]))
            for entry_id in ids:
                self.assertEqual("success", report["targets"][entry_id]["status"])
            self.assertIn("shared_workdir", report)
            self.assertEqual(set(ids), set(report["shared_workdir"]["order"]))
            shared_bundle_dir = work / "shared" / "worker-0" / "bundle"
            self.assertTrue(shared_bundle_dir.is_dir(), "jobs<=1 shared mode uses one folder for the whole pass")
            for entry_id in ids:
                self.assertTrue((work / "results" / entry_id / "build.log").is_file(),
                               f"{entry_id}'s own evidence must land in its own per-entry results folder")
            remaining_profiles = list((shared_bundle_dir / "profiles").glob("*.yml"))
            self.assertEqual(1, len(remaining_profiles),
                             "only the last entry to actually run left its own profile behind in the shared folder")

    def test_non_shared_mode_is_completely_unaffected(self) -> None:
        """The default path: nothing about --shared-workdir changes it. Same
        assertions test_a_second_entry_never_sees_a_file_from_the_first
        makes, but each entry keeps its own separate work_dir, the way it
        always has, and both entries' files coexist happily (they are not
        even in the same directory)."""
        session = FakeSession(archives_by_id={"entry-a": archive_with_files("entry-a"),
                                              "entry-b": archive_with_files("entry-b")})
        with tempfile.TemporaryDirectory() as tmp:
            # cleanup=False: this test is about extraction/isolation, not
            # about the (unchanged, pre-existing) post-success cleanup that
            # would otherwise remove bundle_dir wholesale in non-shared mode.
            config = self._config(tmp, cleanup=False)
            result_a = cc.build_one({"id": "entry-a"}, config=config, work_dir=Path(tmp) / "work-a",
                                    browser_factory=session)
            result_b = cc.build_one({"id": "entry-b"}, config=config, work_dir=Path(tmp) / "work-b",
                                    browser_factory=session)
            self.assertEqual("success", result_a["status"])
            self.assertEqual("success", result_b["status"])
            self.assertIsNone(result_a["shared_workdir"])
            self.assertTrue((Path(tmp) / "work-a" / "bundle" / "profiles" / "entry-a.yml").is_file())
            self.assertTrue((Path(tmp) / "work-b" / "bundle" / "profiles" / "entry-b.yml").is_file())


if __name__ == "__main__":
    unittest.main()
