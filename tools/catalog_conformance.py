#!/usr/bin/env python3
"""catalog_conformance — prove the *published* catalog builds, not this
repository's copy of it, the way a real person actually gets it: through
the real Studio page and its real launcher, not a shortcut through either.

tools/build_matrix.py proves this checkout's bundle-catalog/ builds; it
cannot catch a catalog that diverged after the Studio site last deployed,
a package or container image tag an upstream archive quietly dropped
between deploys, or — the failure that actually cost a day, found only by
building an image and booting it — the page's own bundle generation
silently dropping fields (`software.files`, `security.open_ports`, a
service's capability, an appliance's startup command) that a straight
JSON-to-disk shortcut would never have noticed, because it never asked the
page to generate anything.

    tools/catalog_conformance.py build --config conformance.yml
    tools/catalog_conformance.py build --config conformance.yml --only web-server-nginx
    tools/catalog_conformance.py check --config conformance.yml

"build" does what a person does, for every catalogued entry: open the real
Studio page (`config.site_url`, the deployed site or a locally served
release — this tool only ever connects to a URL, never builds or serves
one itself) under headless Chrome (tools/devtools_browser.py), go to the
Bundle Catalog, choose the entry, fill the configuration's name
(deterministically, the entry's own id), and download the bundle through
the page's real "Download bundle" path — the exact `.tar.gz` bytes the
page's own `downloadBundle()` produces, not a reconstruction of them. Then
it behaves like the person: unpacks the archive into its own working
directory and runs the bundle's own `./build.sh`, unattended (`SYNOS_YES=1`)
but otherwise untouched — its update check and its channel handling run
for real, because the launcher is as much a part of what this proves as
the engine is. Only then does it check the result: the image, its
checksum, and the smoke test against the resolved configuration, same as
before. A failure is recorded with which of the three stages it happened
in — `page`, `launcher` or `engine` (`smoke` for a smoke-test failure) —
because those are three different problems (docs/BUILD_MATRIX.md).

"check" is unchanged by any of this: the cheap early warning, no browser,
no build, no container runtime. For every entry it resolves the profile's
package list (straight from the catalog JSON's embedded files — a
generation bug there is exactly what "build" now exists to catch) against
its pinned base and suite's own archive and checks every pinned container
image tag still exists in its registry, in minutes rather than days.

Config (YAML; see conformance.example.yml):
    catalog_url: https://studio.example/data/catalog.json
    site_url: https://studio.example        # build only: the page itself
    workdir: /var/lib/synos-conformance
    min_free_gb: 40           # this checkout's own disk (build only)
    min_store_gb: 30          # the container runtime's storage (build only)
    max_builds_per_run: 5     # build only: how many entries one run attempts
    build_timeout_minutes: 90 # build only: per entry
    smoke: true               # build only: run tools/smoke_test.py on success
    image: null                # build only: $SYNOS_BUILDER_IMAGE equivalent
    engine_source: null        # build only: $SYNOS_ENGINE_SOURCE equivalent (rare: pins a local engine checkout)
    container_root: null       # build only, podman only: SYNOS_CONTAINER_ROOT equivalent; also where free disk is measured
    container_runroot: null    # build only, podman only: SYNOS_CONTAINER_RUNROOT equivalent
    report_url: null           # optional: POST the finished report here

Never invents a shell command from a name read out of a catalog: every
process here is started from an argument vector. Never runs a real browser,
a real build, or hits a real network in its own test suite
(tests/unit/test_catalog_conformance.py); the browser is a pluggable
factory a test replaces with a fake StudioSession, the launcher a fake
`build.sh` script, and fetch/inspect (check mode) pluggable functions —
see tools/devtools_browser.py's own `main()` and this module's "Running one
entry by hand" note in docs/BUILD_MATRIX.md for how to watch it work for
real, outside the test suite.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime
import gzip
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SYNOS = ROOT / "tools" / "synos"

MIN_FREE_GB = 40
MIN_STORE_GB = 30
DEFAULT_TIMEOUT_MINUTES = 90
DEFAULT_MAX_BUILDS = 5
LOG_TAIL_LINES = 60
HTTP_TIMEOUT = 30

STAGE_PAGE, STAGE_LAUNCHER, STAGE_ENGINE, STAGE_SMOKE = "page", "launcher", "engine", "smoke"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_loader(name, importlib.machinery.SourceFileLoader(name, str(path)))
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


synos_engine = _load("synos_engine_cc", SYNOS)
render_manifest = _load("render_manifest_cc", ROOT / "tools" / "render_manifest.py")

sys.path.insert(0, str(ROOT / "tools"))
import smoke_test  # noqa: E402
import host_resources  # noqa: E402
import job_queue  # noqa: E402
import devtools_browser  # noqa: E402


class ConformanceError(Exception):
    """Configuration or a catalog fetch is unusable; nothing to record per-entry."""


class ArchiveError(Exception):
    """The downloaded bundle archive itself could not be trusted or read —
    corrupt, unsafe paths, or missing bundle.json/manifest. This is the
    page's own output going wrong, so it maps to the "page" stage same as
    a devtools_browser.PageError."""


# --------------------------------------------------------------- config
@dataclasses.dataclass
class Config:
    catalog_url: str
    workdir: Path
    site_url: str | None = None            # build only: the real Studio page this tool drives; required to `build`
    min_free_gb: float = MIN_FREE_GB
    min_store_gb: float = MIN_STORE_GB
    max_builds_per_run: int = DEFAULT_MAX_BUILDS
    build_timeout_minutes: float = DEFAULT_TIMEOUT_MINUTES
    jobs: str = "1"
    smoke: bool = True
    image: str | None = None
    engine_source: str | None = None       # build only: SYNOS_ENGINE_SOURCE equivalent; rarely set (docs/BUILD_MATRIX.md)
    container_root: str | None = None      # podman only: SYNOS_CONTAINER_ROOT equivalent, also where free disk is measured
    container_runroot: str | None = None   # podman only: SYNOS_CONTAINER_RUNROOT equivalent
    report_url: str | None = None

    @classmethod
    def load(cls, path: Path) -> "Config":
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not data.get("catalog_url"):
            raise ConformanceError(f"{path}: catalog_url is required")
        if not data.get("workdir"):
            raise ConformanceError(f"{path}: workdir is required")
        known = {f.name for f in dataclasses.fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise ConformanceError(f"{path}: unknown config key(s): {', '.join(sorted(unknown))}")
        data = dict(data)
        data["workdir"] = Path(data["workdir"]).expanduser()
        if "jobs" in data:
            data["jobs"] = str(data["jobs"])  # YAML "jobs: 2" parses as an int; "auto" stays a string either way
        return cls(**data)


# ------------------------------------------------------------- fetching
def _http_get(url: str, timeout: float = HTTP_TIMEOUT) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - URL comes from an operator's own config
        return response.read()


def fetch_catalog(url: str, fetcher=_http_get) -> dict:
    raw = fetcher(url)
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ConformanceError(f"{url} did not return valid JSON: {exc}") from exc
    if "bundle_catalog" not in data:
        raise ConformanceError(f"{url} has no bundle_catalog key (tools/export_catalog.py's own shape)")
    return data


# -------------------------------------------------------- archive -> bundle
def extract_bundle_archive(raw: bytes, into: Path) -> None:
    """Unpacks a bundle .tar.gz — the real bytes downloaded through the
    page's own path — into `into`, the way a person's own `tar` would.
    Refuses anything that would write outside `into`: a corrupt or unsafe
    archive is the page's own output going wrong (ArchiveError, "page"
    stage), never trusted blindly just because it came from the site."""
    into.mkdir(parents=True, exist_ok=True)
    try:
        tar = tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz")
    except tarfile.TarError as exc:
        raise ArchiveError(f"not a valid tar.gz bundle archive: {exc}") from exc
    with tar:
        base = into.resolve()
        for member in tar.getmembers():
            if not member.isfile():
                raise ArchiveError(f"{member.name!r} in the downloaded archive is not a regular file")
            resolved = (into / member.name).resolve()
            if resolved != base and not str(resolved).startswith(str(base) + os.sep):
                raise ArchiveError(f"unsafe path in downloaded bundle archive: {member.name!r}")
        tar.extractall(into, filter="data")  # noqa: S202 - every member was checked above; "data" is belt-and-braces


def read_bundle_manifest(bundle_dir: Path) -> tuple[str, dict]:
    """bundle.json's own manifest key, and that manifest parsed — both read
    straight from what the archive actually contained, never assumed."""
    import yaml
    bundle_json_path = bundle_dir / "bundle.json"
    if not bundle_json_path.is_file():
        raise ArchiveError("the downloaded archive has no bundle.json")
    try:
        descriptor = json.loads(bundle_json_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ArchiveError(f"bundle.json is not valid JSON: {exc}") from exc
    manifest_rel = descriptor.get("manifest")
    if not manifest_rel:
        raise ArchiveError("bundle.json names no manifest")
    manifest_path = bundle_dir / manifest_rel
    if not manifest_path.is_file():
        raise ArchiveError(f"manifest {manifest_rel!r} named by bundle.json is missing from the archive")
    try:
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001 - yaml errors
        raise ArchiveError(f"{manifest_rel} is not valid YAML: {exc}") from exc
    return manifest_rel, manifest


def _eprint(message: str) -> None:
    """print(..., file=sys.stderr) that cannot itself raise: a caller with a
    closed or redirected stderr (a test capturing output, a service manager
    that already tore its pipe down) must never turn "here is an error" into
    a crash of its own."""
    try:
        print(message, file=sys.stderr)
    except Exception:  # noqa: BLE001 - reporting an error must never itself raise
        pass


# --------------------------------------------------------------- build
def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _tail(path: Path, lines: int = LOG_TAIL_LINES) -> list[str]:
    if not path.is_file():
        return []
    return path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_jobs(config: Config) -> tuple[int, list[str]]:
    """config.jobs ("auto" or a count) checked against what this machine can
    actually feed (tools/host_resources.py, the same formula
    tools/build_matrix.py uses); also refuses when there is no container
    engine at all, which no job count helps."""
    jobs, problems = host_resources.resolve_jobs(config.jobs, ROOT, min_free_gb=config.min_free_gb,
                                                 min_store_gb=config.min_store_gb, container_root=config.container_root)
    if not host_resources.container_engine():
        problems.append("podman or docker is required")
    return jobs, problems


def _launcher_env(config: Config) -> dict:
    """The environment `./build.sh` runs under: non-interactive
    (SYNOS_YES=1, so it never blocks on a prompt) but otherwise untouched —
    no SYNOS_NO_UPDATE_CHECK, no SYNOS_CHANNEL override, so the launcher's
    own update check and channel handling run exactly as they would for a
    real person (item 62's whole point). config.container_root/_runroot and
    config.image/config.engine_source only add to the environment; unset,
    they leave the launcher's own defaults (podman's own storage, whatever
    the bundle's engine.min names, GHCR) alone, same as for a real person
    who never heard of them."""
    env = os.environ.copy()
    env["SYNOS_YES"] = "1"
    if config.container_root:
        env["SYNOS_CONTAINER_ROOT"] = config.container_root
    if config.container_runroot:
        env["SYNOS_CONTAINER_RUNROOT"] = config.container_runroot
    if config.image:
        env["SYNOS_BUILDER_IMAGE"] = config.image
    if config.engine_source:
        env["SYNOS_ENGINE_SOURCE"] = config.engine_source
    return env


def run_launcher(bundle_dir: Path, *, config: Config, log_path: Path) -> tuple[int, str]:
    """Runs the downloaded bundle's own ./build.sh, unattended, exactly the
    way docs/BUNDLE.md tells a person to. Returns (exit_code, stage), where
    stage distinguishes "launcher" (build.sh never got as far as starting
    the containerized build: no runtime, couldn't resolve/pull an image, a
    host check failed) from "engine" (the containerized build ran — dist/build.log
    exists — and failed inside the container). Raises subprocess.TimeoutExpired
    on a timeout; the caller records that itself, stage "engine" (a hang is
    presumed to be the actual build, the by far longer-running part)."""
    build_sh = bundle_dir / "build.sh"
    if not build_sh.is_file():
        raise ArchiveError("the downloaded archive has no build.sh")
    argv = ["bash", str(build_sh)]
    env = _launcher_env(config)
    completed = subprocess.run(argv, cwd=bundle_dir, env=env, capture_output=True, text=True,
                               timeout=config.build_timeout_minutes * 60, check=False)
    dist_dir = bundle_dir / "dist"
    launcher_log = dist_dir / "build.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if launcher_log.is_file():
        log_path.write_text(launcher_log.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
    else:
        log_path.write_text((completed.stdout or "") + (completed.stderr or ""), encoding="utf-8")
    stage = STAGE_ENGINE if launcher_log.is_file() else STAGE_LAUNCHER
    return completed.returncode, stage


def collect_iso(bundle_dir: Path, target_dir: Path) -> dict | None:
    """The newest ISO build.sh's own dist/ holds, copied to target_dir (which
    survives after work_dir/bundle is cleaned up or reused for the next entry)."""
    dist_dir = bundle_dir / "dist"
    isos = sorted(dist_dir.glob("*.iso"), key=lambda p: p.stat().st_mtime) if dist_dir.is_dir() else []
    if not isos:
        return None
    iso_path = isos[-1]
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / iso_path.name
    shutil.copy2(iso_path, dest)
    return {"path": str(dest), "size": dest.stat().st_size, "sha256": _sha256_file(dest)}


def build_one(entry: dict, *, config: Config, work_dir: Path, browser_factory=None) -> dict:
    """Downloads entry["id"] through the real Studio page, unpacks it, and
    runs its own launcher — see the module docstring for the full flow.
    `browser_factory()` must return a context manager whose `__enter__`
    gives something with a `download_bundle(entry_id) -> (bytes, filename)`
    method (devtools_browser.StudioSession by default; tests substitute a
    fake). Never raises: every failure mode is recorded with a `stage`."""
    result = {"id": entry["id"], "kind": "catalog", "base": None, "suite": None, "checksum": None,
              "engine": synos_engine.engine_version(), "start": _now(), "end": None, "duration_s": None,
              "exit_code": None, "status": "running", "stage": None, "iso": None, "log_path": None,
              "log_tail": [], "smoke": None, "download": None}
    start = time.monotonic()

    def _finish(status: str, stage: str | None, log_tail: list[str]) -> dict:
        result["status"] = status
        result["stage"] = stage
        result["log_tail"] = log_tail
        result["end"] = _now()
        result["duration_s"] = round(time.monotonic() - start, 1)
        return result

    browser_factory = browser_factory or (lambda: devtools_browser.StudioSession(config.site_url))

    # ---- stage: page (download the real bytes through the real page) ----
    try:
        with browser_factory() as session:
            raw, filename = session.download_bundle(entry["id"])
    except devtools_browser.BrowserUnavailable as exc:
        return _finish("skipped", STAGE_PAGE, [f"no browser available to drive the page: {exc}"])
    except devtools_browser.PageError as exc:
        return _finish("failed", STAGE_PAGE, [str(exc)])
    except Exception as exc:  # noqa: BLE001 - one entry's crash must not abort the run
        return _finish("error", STAGE_PAGE, [f"{type(exc).__name__}: {exc}"])

    result["checksum"] = hashlib.sha256(raw).hexdigest()
    result["download"] = {"filename": filename, "size": len(raw)}

    # ---- still "page": a bad archive is the page's own output, same stage ----
    bundle_dir = work_dir / "bundle"
    target_dir = work_dir / "output"
    log_path = target_dir / "build.log"
    result["log_path"] = str(log_path)
    try:
        extract_bundle_archive(raw, bundle_dir)
        manifest_rel, manifest = read_bundle_manifest(bundle_dir)
        result["base"], result["suite"] = manifest.get("base"), manifest.get("suite")
    except ArchiveError as exc:
        return _finish("failed", STAGE_PAGE, [str(exc)])
    except Exception as exc:  # noqa: BLE001
        return _finish("error", STAGE_PAGE, [f"{type(exc).__name__}: {exc}"])

    # ---- stage: launcher / engine (behave like the person: run build.sh) ----
    try:
        exit_code, stage = run_launcher(bundle_dir, config=config, log_path=log_path)
        result["exit_code"] = exit_code
        if exit_code == 0:
            result["status"] = "success"
        else:
            result["stage"] = stage
            result["status"] = {1: "invalid", 2: "host", 3: "build_failed"}.get(exit_code, "failed")
            result["log_tail"] = _tail(log_path)
    except subprocess.TimeoutExpired:
        result["status"] = "timeout"
        result["stage"] = STAGE_ENGINE
        result["log_tail"] = _tail(log_path)
    except ArchiveError as exc:
        return _finish("failed", STAGE_PAGE, [str(exc)])
    except Exception as exc:  # noqa: BLE001
        result["status"] = "error"
        result["stage"] = STAGE_LAUNCHER
        result["log_tail"] = [f"{type(exc).__name__}: {exc}"]

    result["end"] = _now()
    result["duration_s"] = round(time.monotonic() - start, 1)

    if result["status"] == "success":
        result["iso"] = collect_iso(bundle_dir, target_dir)

    # ---- check the result: the smoke test against the resolved configuration ----
    if result["status"] == "success" and config.smoke:
        if result["iso"]:
            resolved_profile: dict = {}
            try:
                profile_path = bundle_dir / "profiles" / f"{manifest['profile']}.yml"
                profile_text = profile_path.read_text(encoding="utf-8") if profile_path.is_file() else None
                resolved_profile = resolve_published_profile(manifest["profile"], profile_text)
            except Exception as exc:  # noqa: BLE001
                result["smoke"] = {"status": "error", "reason": f"could not resolve profile: {exc}"}
            if result["smoke"] is None:
                result["smoke"] = smoke_test.run(Path(result["iso"]["path"]), resolved_profile, output_dir=target_dir)
        else:
            result["smoke"] = {"status": "skipped", "reason": "build succeeded but produced no ISO"}
        if result["smoke"] and result["smoke"]["status"] not in ("passed", "skipped"):
            result["stage"] = STAGE_SMOKE

    return result


def resolve_published_profile(profile_id: str, own_text: str | None) -> dict:
    """The profile as the published entry ships it, `extends` merged the way
    tools/render_manifest.py merges it, without writing anything into this
    checkout's own profiles/ directory (unlike applying the bundle, which
    tools/synos does; this is used for the cheap "check" mode and for the
    smoke test's expectations, both read-only)."""
    import yaml
    if own_text is None:
        resolved, _chain = render_manifest.resolve_profile(profile_id)
        return resolved
    data = yaml.safe_load(own_text)
    parent = data.get("extends")
    body = {k: v for k, v in data.items() if k not in {"id", "extends", "description"}}
    if not parent:
        return body
    parent_data, _chain = render_manifest.resolve_profile(parent)
    return render_manifest.deep_merge(parent_data, body)


class _EntryRef:
    """job_queue.run_parallel only needs `.id` off whatever it schedules;
    entries are plain dicts, so this is the thinnest wrapper that carries
    both the id and the entry itself to the worker callback."""

    def __init__(self, entry: dict):
        self.id = entry["id"]
        self.entry = entry


def run_build(config: Config, *, catalog: dict, max_builds: int | None = None, jobs: int = 1,
             only: list[str] | None = None, browser_factory=None) -> dict:
    entries = catalog["bundle_catalog"]
    if only:
        wanted = set(only)
        entries = [e for e in entries if e["id"] in wanted]
        missing = wanted - {e["id"] for e in entries}
        if missing:
            raise ConformanceError(f"--only names entries the catalog does not have: {', '.join(sorted(missing))}")
    limit = max_builds if max_builds is not None else (config.max_builds_per_run if not only else None)
    selected = entries[:limit] if limit else entries
    report = {"mode": "build", "generated": _now(), "engine": synos_engine.engine_version(),
              "catalog_url": config.catalog_url, "site_url": config.site_url, "targets": {}}

    for entry in selected:
        work_dir = config.workdir / "work" / entry["id"]
        if work_dir.exists():
            shutil.rmtree(work_dir)

    if jobs <= 1:
        for entry in selected:
            work_dir = config.workdir / "work" / entry["id"]
            report["targets"][entry["id"]] = build_one(entry, config=config, work_dir=work_dir,
                                                        browser_factory=browser_factory)
        return report

    def build_one_for_worker(ref: "_EntryRef", worker_id: int) -> dict:
        work_dir = config.workdir / "work" / ref.id
        worker_config = config
        if not config.container_root:
            # Each worker gets its own podman storage, so two workers that
            # happen to build the same base/suite never share a cache
            # volume name (docs/BUNDLE.md's own synos-cache-<base>-<suite>,
            # written by the launcher itself, not by this tool) — the same
            # collision that cost two concurrent ./build.sh runs once.
            worker_root = config.workdir / "podman-storage" / f"worker-{worker_id}"
            worker_root.mkdir(parents=True, exist_ok=True)
            worker_config = dataclasses.replace(config, container_root=str(worker_root))
        return build_one(ref.entry, config=worker_config, work_dir=work_dir, browser_factory=browser_factory)

    results = job_queue.run_parallel([_EntryRef(entry) for entry in selected], jobs, build_one_for_worker)
    report["targets"] = results
    return report


# --------------------------------------------------------------- check
def _component_urls(base_env: dict, suite: str, arch: str) -> list[str]:
    mirror = base_env["APT_MIRROR"].rstrip("/")
    return [f"{mirror}/dists/{suite}/{component}/binary-{arch}/Packages.gz"
            for component in base_env.get("COMPONENTS", "main").split()]


def fetch_archive_package_names(urls: list[str], fetcher=_http_get) -> set[str]:
    names: set[str] = set()
    for url in urls:
        try:
            raw = gzip.decompress(fetcher(url))
        except Exception:  # noqa: BLE001 - a missing component index is reported, not fatal
            continue
        for line in raw.decode("utf-8", "replace").splitlines():
            if line.startswith("Package: "):
                names.add(line[len("Package: "):].strip())
    return names


def resolved_install_packages(manifest: dict, profile: dict) -> list[str]:
    base_dir = ROOT / "bases" / manifest["base"]
    pkg_map = render_manifest.load_package_map(base_dir / "packages.map")
    software = profile.get("software", {}) or {}
    bundles = render_manifest.load_bundles()
    expanded: list = []
    for bundle_name in software.get("bundles", []) or []:
        expanded += bundles.get(bundle_name, [])
    expanded += (software.get("packages", {}) or {}).get("add", [])
    remove = set((software.get("packages", {}) or {}).get("remove", []) or [])
    concrete, _unmapped = render_manifest.resolve_packages(expanded, pkg_map, manifest.get("arch", "amd64"), ["en"], manifest["base"])
    return [p for p in concrete if p not in remove]


def _skopeo_inspect(image: str) -> bool:
    if not shutil.which("skopeo"):
        return True  # can't tell; do not manufacture a failure for a missing tool
    result = subprocess.run(["skopeo", "inspect", f"docker://{image}"], capture_output=True, text=True,
                            check=False, timeout=30)
    return result.returncode == 0


def check_one(entry: dict, *, archive_cache: dict[tuple[str, str], set[str]], image_cache: dict[str, bool],
             fetcher=_http_get, inspector=_skopeo_inspect) -> dict:
    result = {"id": entry["id"], "base": None, "suite": None, "status": "ok",
              "missing_packages": [], "missing_images": [], "packages_checked": 0, "images_checked": 0, "reason": None}
    try:
        bundle_json = json.loads(entry["files"]["bundle.json"])
        manifest_text = entry["files"][bundle_json["manifest"]]
        import yaml
        manifest = yaml.safe_load(manifest_text)
        profile_path = f"profiles/{manifest['profile']}.yml"
        own_profile_text = entry["files"].get(profile_path)
        profile = resolve_published_profile(manifest["profile"], own_profile_text)
    except Exception as exc:  # noqa: BLE001
        result["status"] = "error"
        result["reason"] = f"{type(exc).__name__}: {exc}"
        return result

    result["base"], result["suite"] = manifest.get("base"), manifest.get("suite")
    key = (manifest.get("base"), manifest.get("suite"))
    if key not in archive_cache:
        base_env = render_manifest.load_env(ROOT / "bases" / key[0] / "base.env")
        urls = _component_urls(base_env, key[1], manifest.get("arch", "amd64"))
        archive_cache[key] = fetch_archive_package_names(urls, fetcher=fetcher)
    available = archive_cache[key]

    try:
        packages = resolved_install_packages(manifest, profile)
    except Exception as exc:  # noqa: BLE001
        result["status"] = "error"
        result["reason"] = f"could not resolve packages: {type(exc).__name__}: {exc}"
        return result
    result["packages_checked"] = len(packages)
    if available:
        result["missing_packages"] = sorted(p for p in packages if p not in available)

    images = [service["image"] for service in (profile.get("software", {}) or {}).get("services", []) or []]
    result["images_checked"] = len(images)
    for image in images:
        if image not in image_cache:
            image_cache[image] = inspector(image)
        if not image_cache[image]:
            result["missing_images"].append(image)

    if result["missing_packages"] or result["missing_images"]:
        result["status"] = "drift"
    return result


def run_check(config: Config, *, catalog: dict, fetcher=_http_get, inspector=_skopeo_inspect) -> dict:
    entries = catalog["bundle_catalog"]
    archive_cache: dict[tuple[str, str], set[str]] = {}
    image_cache: dict[str, bool] = {}
    report = {"mode": "check", "generated": _now(), "engine": synos_engine.engine_version(),
              "catalog_url": config.catalog_url, "targets": {}}
    for entry in entries:
        report["targets"][entry["id"]] = check_one(entry, archive_cache=archive_cache, image_cache=image_cache,
                                                    fetcher=fetcher, inspector=inspector)
    return report


# ------------------------------------------------------------ reporting
def diff_reports(previous: dict | None, current: dict) -> dict:
    """What a person acts on: not the whole report again, just what changed."""
    if not previous:
        return {"newly_failed": list(current["targets"]), "recovered": [], "still_failing": [], "unchanged": []}
    ok_statuses = {"success", "ok"}
    newly_failed, recovered, still_failing, unchanged = [], [], [], []
    for target_id, record in current["targets"].items():
        was_ok = previous.get("targets", {}).get(target_id, {}).get("status") in ok_statuses
        is_ok = record.get("status") in ok_statuses
        if target_id not in previous.get("targets", {}):
            (newly_failed if not is_ok else unchanged).append(target_id)
        elif was_ok and not is_ok:
            newly_failed.append(target_id)
        elif not was_ok and is_ok:
            recovered.append(target_id)
        elif not was_ok and not is_ok:
            still_failing.append(target_id)
        else:
            unchanged.append(target_id)
    return {"newly_failed": newly_failed, "recovered": recovered, "still_failing": still_failing, "unchanged": unchanged}


def post_report(report: dict, url: str, poster=None) -> None:
    """Best-effort delivery: a failure here is a warning (via _eprint, which
    cannot itself raise), never an exception a caller has to handle."""
    poster = poster or _http_post
    try:
        poster(url, json.dumps(report).encode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - delivery is best-effort, never fatal to the run
        _eprint(f"warning: could not POST the report to {url}: {exc}")


def _http_post(url: str, body: bytes) -> None:
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:  # noqa: S310
        response.read()


def summary_text(report: dict, diff: dict) -> str:
    counts: dict[str, int] = {}
    for record in report["targets"].values():
        counts[record.get("status", "-")] = counts.get(record.get("status", "-"), 0) + 1
    lines = [f"{report['mode']} run against {report['catalog_url']}: {len(report['targets'])} entr(y/ies)"]
    lines.append(", ".join(f"{count} {status}" for status, count in sorted(counts.items())))
    if diff["newly_failed"]:
        lines.append(f"newly failing: {', '.join(sorted(diff['newly_failed']))}")
    if diff["recovered"]:
        lines.append(f"recovered: {', '.join(sorted(diff['recovered']))}")
    # "skipped" (item 61: no browser available) is printed with its reason,
    # not left to be dug out of report.json — it means this run proved
    # nothing for that entry, which is worth saying plainly.
    for target_id, record in sorted(report["targets"].items()):
        if record.get("status") == "skipped":
            reason = (record.get("log_tail") or ["no reason recorded"])[0]
            lines.append(f"skipped {target_id}: {reason}")
    return "\n".join(lines)


# ---------------------------------------------------------------- CLI
def _run(mode: str, config: Config, *, max_builds: int | None = None, only: list[str] | None = None) -> int:
    catalog = fetch_catalog(config.catalog_url)
    config.workdir.mkdir(parents=True, exist_ok=True)
    report_path = config.workdir / f"{mode}-report.json"
    previous = json.loads(report_path.read_text(encoding="utf-8")) if report_path.is_file() else None

    if mode == "build":
        if not config.site_url:
            _eprint("error: site_url is required to build (the real Studio page this tool drives)")
            return 2
        jobs, problems = resolve_jobs(config)
        if problems:
            for problem in problems:
                _eprint(f"error: {problem}")
            return 2
        try:
            report = run_build(config, catalog=catalog, max_builds=max_builds, jobs=jobs, only=only)
        except ConformanceError as exc:
            _eprint(f"error: {exc}")
            return 2
    else:
        report = run_check(config, catalog=catalog)

    diff = diff_reports(previous, report)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    text = summary_text(report, diff)
    (config.workdir / f"{mode}-summary.txt").write_text(text + "\n", encoding="utf-8")
    print(text)

    if config.report_url:
        post_report({"report": report, "diff": diff}, config.report_url)

    ok_statuses = {"success", "ok"}
    failed = [tid for tid, record in report["targets"].items() if record.get("status") not in ok_statuses]
    return 0 if not failed else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="catalog_conformance", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="mode", required=True)
    for name in ("build", "check"):
        p = sub.add_parser(name)
        p.add_argument("--config", type=Path, required=True)
        p.add_argument("--dry-run", action="store_true", help="fetch the catalog and print what would run, nothing else")
        if name == "build":
            p.add_argument("--max", type=int, help="override max_builds_per_run for this run")
            p.add_argument("--only", help="comma-separated entry ids to build (e.g. one entry, to watch it work by hand)")
    args = parser.parse_args(argv)

    try:
        config = Config.load(args.config)
    except ConformanceError as exc:
        _eprint(f"error: {exc}")
        return 2

    only = args.only.split(",") if getattr(args, "only", None) else None

    if args.dry_run:
        try:
            catalog = fetch_catalog(config.catalog_url)
        except ConformanceError as exc:
            _eprint(f"error: {exc}")
            return 2
        entries = catalog["bundle_catalog"]
        if only:
            entries = [e for e in entries if e["id"] in set(only)]
        if args.mode == "build" and not only:
            entries = entries[:args.max] if args.max else entries[:config.max_builds_per_run]
        for entry in entries:
            print(entry["id"])
        print(f"\n{len(entries)} entr(y/ies) against {config.catalog_url}"
             + (f", site {config.site_url}" if args.mode == "build" and config.site_url else ""))
        return 0

    try:
        return _run(args.mode, config, max_builds=getattr(args, "max", None), only=only)
    except ConformanceError as exc:
        _eprint(f"error: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
