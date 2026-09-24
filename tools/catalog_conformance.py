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
    upload: null                # build only, optional: send workdir/build-status.json to the deployed site
                                 # (tools/status_uploader.py; docs/BUILD_MATRIX.md's "Publishing the build-status badge")
    cleanup: true                # build only: free a successful entry's heavy directories as it finishes; set
                                 # false (or --no-cleanup) to keep everything for a debugging run

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
import socket
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
import build_status  # noqa: E402
import status_uploader  # noqa: E402
import entry_cleanup  # noqa: E402


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
    upload: dict | None = None             # build only, optional: see tools/status_uploader.py; never committed
    cleanup: bool = True                   # build only: free a successful entry's heavy directories as it finishes
                                             # (tools/entry_cleanup.py); set false (or --no-cleanup) to keep everything

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


def cleanup_one(result: dict, *, work_dir: Path) -> dict:
    """Items 87-89: called only for a `result["status"] == "success"` entry
    (a failed or skipped one keeps everything — that is exactly what
    somebody will want to read — so this is never even called for one),
    immediately after that entry finishes, never batched to the end of a
    run (so a long run's peak disk usage stays near one build, not fifty).

    Removes the heavy things: the unpacked bundle (its own build tree and
    `dist/`, including the ISO `build.sh` left there before this tool
    copied it out) and the copied ISO itself under `work_dir/output` —
    "the image's name, size and checksum" already live in `result`/
    build-report.json/build-status.json, which is what "record what a
    person would need later" (item 87) actually means for the multi-GB
    image: the bytes are disposable, the proof they existed and what they
    checksummed to is not.

    Keeps the small things: the build log (`tools/entry_cleanup.py`'s
    `maybe_gzip`, compressed only if it is actually large), and the smoke
    test's own evidence — this project's own smoke test
    (tools/smoke_test.py) is serial-console-only by design and has no
    screenshot capability (it says so in its own module docstring: "the
    same recipe ... applied here without that suite's GRUB-menu and
    screenshot machinery, which this headless smoke test does not need");
    its transcript (the serial log) is the equivalent evidence, and is
    kept in its place. `work_dir/output` itself (the small directory that
    held both) is left in place, never removed.

    Returns a summary — {"performed": True, "removed": [...], "kept":
    [...]} — folded into the entry's own result so build-report.json and
    the run's own summary can say exactly what happened (item 88)."""
    bundle_dir = work_dir / "bundle"
    target_dir = work_dir / "output"
    removed: list[str] = []
    kept: list[str] = []

    if bundle_dir.exists():
        entry_cleanup.safe_remove(work_dir, bundle_dir)
        removed.append(str(bundle_dir))

    iso = result.get("iso") or {}
    if iso.get("path"):
        iso_path = Path(iso["path"])
        if iso_path.exists():
            entry_cleanup.safe_remove(work_dir, iso_path)
            removed.append(str(iso_path))

    if result.get("log_path"):
        log_path = Path(result["log_path"])
        if log_path.exists():
            final_log = entry_cleanup.maybe_gzip(log_path)
            kept.append(str(final_log))

    smoke = result.get("smoke") or {}
    for key in ("screenshot", "transcript"):
        evidence = smoke.get(key)
        if evidence and Path(evidence).exists():
            kept.append(str(evidence))

    if target_dir.exists():
        kept.append(str(target_dir))

    return {"performed": True, "removed": removed, "kept": kept}


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

    # ---- items 87-89: free the heavy things this one entry created, as
    # soon as it is done, keeping the evidence a person would need later ----
    if result["status"] == "success" and config.cleanup:
        try:
            result["cleanup"] = cleanup_one(result, work_dir=work_dir)
        except entry_cleanup.CleanupError as exc:
            result["cleanup"] = {"performed": False, "removed": [], "kept": [], "error": str(exc)}
    else:
        reason = "cleanup is disabled" if not config.cleanup else "kept: not a successful entry"
        result["cleanup"] = {"performed": False, "removed": [], "kept": [], "reason": reason}

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


def _remove_cache_volume(engine: str, volume: str) -> tuple[bool, str]:
    """The one real side effect in this module's storage reclaim: asks the
    runtime itself to remove one named volume, no force flag, so its own
    refusal when something still has it mounted is the safety check.
    Returns (removed, reason) — reason is empty on success, otherwise the
    runtime's own last output line."""
    proc = subprocess.run([engine, "volume", "rm", volume], capture_output=True, text=True,
                          check=False, timeout=30)
    if proc.returncode == 0:
        return True, ""
    tail = (proc.stderr or proc.stdout or "").strip().splitlines()
    return False, tail[-1] if tail else "unknown reason"


def reclaim_container_storage(config: Config, *, jobs: int, targets: dict,
                              container_engine=None, volume_remover=None) -> dict:
    """Item 91: when this run owns the storage it used — `config.
    container_root` was not set, meaning the person did not point this at
    a location of their own ("never touch storage the person configured
    themselves" then means leaving it alone entirely, skipped below) —
    removes every per-worker podman storage root this run itself created
    (`config.workdir/podman-storage/worker-N`) and asks the container
    runtime itself to prune the launcher's own `synos-cache-<base>-<suite>`
    volume (docs/BUNDLE.md) for every base/suite this run touched. Never
    with a force flag: the runtime's own refusal to remove a volume still
    mounted by a container *is* the "another build may be using it" check,
    so this only ever removes a volume the runtime itself judges idle.
    Returns exactly what was pruned and what was left alone and why —
    never silent about either."""
    result = {"worker_roots_removed": [], "worker_roots_left_alone": [],
             "cache_volumes_pruned": [], "cache_volumes_left_alone": []}
    if config.container_root:
        return result

    for worker_id in range(max(jobs, 1)):
        worker_root = config.workdir / "podman-storage" / f"worker-{worker_id}"
        if not worker_root.exists():
            continue
        try:
            entry_cleanup.safe_remove(config.workdir, worker_root)
            result["worker_roots_removed"].append(str(worker_root))
        except entry_cleanup.CleanupError as exc:
            result["worker_roots_left_alone"].append(f"{worker_root}: {exc}")

    container_engine = container_engine or host_resources.container_engine
    volume_remover = volume_remover or _remove_cache_volume
    engine = container_engine()
    if not engine:
        return result
    pairs = sorted({(r.get("base"), r.get("suite")) for r in targets.values() if r.get("base") and r.get("suite")})
    for base, suite in pairs:
        volume = f"synos-cache-{base}-{suite}"
        removed, reason = volume_remover(engine, volume)
        if removed:
            result["cache_volumes_pruned"].append(volume)
        else:
            result["cache_volumes_left_alone"].append(f"{volume}: {reason}")
    return result


def run_build(config: Config, *, catalog: dict, max_builds: int | None = None, jobs: int = 1,
             only: list[str] | None = None, browser_factory=None, upload_config=None,
             dry_run_upload: bool = False, upload_transport=None, upload_sleeper=None,
             container_engine=None, volume_remover=None) -> dict:
    # Item 90: refuse the whole run, not only cleanup, when workdir looks
    # like a filesystem root, a home directory, or a source checkout —
    # a configuration mistake worth catching outright.
    try:
        entry_cleanup.guard_workdir(config.workdir)
    except entry_cleanup.CleanupError as exc:
        raise ConformanceError(str(exc)) from exc

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

    # Item 71-73, 93: a small build-status.json, updated live as each
    # entry moves queued -> testing -> a real outcome (not only at the
    # end — a run interrupted partway through fifty-odd entries must
    # still leave a valid file behind, and a file fetched mid-run must
    # say "testing", never yesterday's result) and uploaded, best-effort,
    # whenever an entry's live state changes plus once more,
    # unconditionally, when the whole run finishes. The uploader is
    # entirely optional: no `upload:` section in the config means
    # `changer.enabled` is False and `maybe_upload` is a no-op.
    status_path = config.workdir / "build-status.json"
    changer = status_uploader.ChangeUploader(upload_config, dry_run=dry_run_upload,
                                             transport=upload_transport, sleeper=upload_sleeper)
    status_updater = build_status.StatusUpdater(
        status_path, on_change=(changer.maybe_upload if changer.enabled else None),
        own_paths=[str(config.workdir), str(Path.home())], hostname=socket.gethostname())

    # Item 93: an entry a previous run left "testing" (killed before it
    # finished) is resolved to "failed" before anything new is queued —
    # never guessed as "success". Said plainly in the report and the
    # run's own summary, never left silent.
    interrupted = status_updater.resolve_interrupted()
    if interrupted:
        report["resolved_interrupted"] = interrupted
    for entry in selected:
        status_updater.queue(entry["id"])

    if jobs <= 1:
        for entry in selected:
            work_dir = config.workdir / "work" / entry["id"]
            status_updater.start(entry["id"])
            result = build_one(entry, config=config, work_dir=work_dir, browser_factory=browser_factory)
            report["targets"][entry["id"]] = result
            status_updater.record(entry["id"], result)
    else:
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
            status_updater.start(ref.id)
            return build_one(ref.entry, config=worker_config, work_dir=work_dir, browser_factory=browser_factory)

        results = job_queue.run_parallel(
            [_EntryRef(entry) for entry in selected], jobs, build_one_for_worker,
            on_result=lambda ref, result: status_updater.record(ref.id, result))
        report["targets"] = results

    # Item 91: reclaim the container storage this run itself owns, once,
    # at the end — never mid-run, since a worker's own storage root is
    # still in use by that worker until the whole run is done.
    if config.cleanup:
        report["storage_reclaimed"] = reclaim_container_storage(
            config, jobs=jobs, targets=report["targets"],
            container_engine=container_engine, volume_remover=volume_remover)

    # The end-of-run upload item 73 asks for regardless of whether the
    # last entry changed state — the same file every per-entry change
    # already sent, so this is a low-cost final sync, not a second file.
    if changer.enabled:
        changer.maybe_upload(status_path)
    if changer.warnings:
        report["upload_warnings"] = list(changer.warnings)

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
    if report.get("resolved_interrupted"):
        lines.append("resolved as failed (a previous run was interrupted mid-test): "
                     + ", ".join(sorted(report["resolved_interrupted"])))
    # "skipped" (item 61: no browser available) is printed with its reason,
    # not left to be dug out of report.json — it means this run proved
    # nothing for that entry, which is worth saying plainly.
    for target_id, record in sorted(report["targets"].items()):
        if record.get("status") == "skipped":
            reason = (record.get("log_tail") or ["no reason recorded"])[0]
            lines.append(f"skipped {target_id}: {reason}")
    # Items 87-89: say exactly which directories were freed and which were
    # kept, and why — never leave that only in build-report.json.
    cleanups = {tid: r["cleanup"] for tid, r in report["targets"].items() if r.get("cleanup")}
    if cleanups:
        cleaned = sorted(tid for tid, c in cleanups.items() if c.get("performed"))
        kept = sorted(tid for tid, c in cleanups.items() if not c.get("performed"))
        if cleaned:
            lines.append(f"cleaned up {len(cleaned)} successful entr(y/ies)' working director(y/ies): "
                         + ", ".join(cleaned))
        for tid in kept:
            reason = cleanups[tid].get("reason") or cleanups[tid].get("error") or "not cleaned"
            lines.append(f"kept {tid}'s working directory ({reason})")
    reclaimed = report.get("storage_reclaimed")
    if reclaimed and (reclaimed["worker_roots_removed"] or reclaimed["cache_volumes_pruned"]
                      or reclaimed["worker_roots_left_alone"] or reclaimed["cache_volumes_left_alone"]):
        if reclaimed["worker_roots_removed"]:
            lines.append(f"removed {len(reclaimed['worker_roots_removed'])} worker podman storage root(s)")
        if reclaimed["cache_volumes_pruned"]:
            lines.append("pruned cache volume(s): " + ", ".join(sorted(reclaimed["cache_volumes_pruned"])))
        if reclaimed["cache_volumes_left_alone"]:
            lines.append("left cache volume(s) alone: " + "; ".join(sorted(reclaimed["cache_volumes_left_alone"])))
    return "\n".join(lines)


# ---------------------------------------------------------------- CLI
def _run(mode: str, config: Config, *, max_builds: int | None = None, only: list[str] | None = None,
        dry_run_upload: bool = False, no_cleanup: bool = False) -> int:
    catalog = fetch_catalog(config.catalog_url)
    config.workdir.mkdir(parents=True, exist_ok=True)
    report_path = config.workdir / f"{mode}-report.json"
    previous = json.loads(report_path.read_text(encoding="utf-8")) if report_path.is_file() else None

    if mode == "build":
        if not config.site_url:
            _eprint("error: site_url is required to build (the real Studio page this tool drives)")
            return 2
        # Item 90: --no-cleanup only ever moves the effective setting
        # towards keeping more, never towards deleting more than the
        # config file itself already says.
        if no_cleanup:
            config = dataclasses.replace(config, cleanup=False)
        jobs, problems = resolve_jobs(config)
        if problems:
            for problem in problems:
                _eprint(f"error: {problem}")
            return 2
        try:
            upload_config = status_uploader.parse_upload_config(config.upload)
        except status_uploader.UploadConfigError as exc:
            _eprint(f"error: {exc}")
            return 2
        try:
            report = run_build(config, catalog=catalog, max_builds=max_builds, jobs=jobs, only=only,
                               upload_config=upload_config, dry_run_upload=dry_run_upload)
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

    # A failed upload is a warning, never a build failure (item 73) — said
    # plainly here, same as a skipped entry's reason, never left to be dug
    # out of report.json alone.
    for warning in report.get("upload_warnings", []):
        _eprint(f"warning: {warning}")

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
            p.add_argument("--dry-run-upload", action="store_true",
                          help="run for real, but only print where build-status.json would be sent, never send it "
                               "(tools/status_uploader.py has a standalone way to test the upload config on its own)")
            p.add_argument("--no-cleanup", action="store_true",
                          help="keep every entry's full working directory (the unpacked bundle, dist/, the ISO) "
                               "even on success, instead of the default of freeing it as soon as that entry "
                               "finishes - for a debugging run; only ever keeps more than conformance.yml's own "
                               "cleanup: setting, never less")
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
        return _run(args.mode, config, max_builds=getattr(args, "max", None), only=only,
                   dry_run_upload=getattr(args, "dry_run_upload", False),
                   no_cleanup=getattr(args, "no_cleanup", False))
    except ConformanceError as exc:
        _eprint(f"error: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
