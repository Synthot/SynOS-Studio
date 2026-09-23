#!/usr/bin/env python3
"""catalog_conformance — prove the *published* catalog builds, not this
repository's copy of it.

tools/build_matrix.py proves this checkout's bundle-catalog/ builds; it
cannot catch a catalog that diverged after the Studio site last deployed,
or a package or container image tag an upstream archive quietly dropped
between deploys. This tool downloads the catalog a real person would get
(the Studio site's exported JSON, `tools/export_catalog.py`'s
`bundle_catalog` key), builds every entry the same way `tools/synos` does,
and reports what would break before a person hits it. Kept as a separate
tool from tools/build_matrix.py (a different report, a different trigger, a
different machine to run it on), sharing only tools/smoke_test.py and the
per-target report shape.

    tools/catalog_conformance.py build --config conformance.yml
    tools/catalog_conformance.py check --config conformance.yml

"build" applies and builds every catalogued entry end to end (network,
disk and time cost like tools/build_matrix.py's catalog targets: 40+ GB,
tens of minutes, per entry) and smoke-tests what succeeds.

"check" is the cheap early warning: no build, no container runtime. For
every entry it resolves the profile's package list against its pinned
base and suite's own archive (the real Packages index, not a guess) and
checks every pinned container image tag still exists in its registry, in
minutes rather than days. This is the mode meant to run often and page
someone the moment a component a build depends on disappears upstream.

Config (YAML; see conformance.example.yml):
    catalog_url: https://studio.example/data/catalog.json
    workdir: /var/lib/synos-conformance
    min_free_gb: 40           # this checkout's own disk (build only)
    min_store_gb: 30          # the container runtime's storage (build only)
    max_builds_per_run: 5     # build only: how many entries one run attempts
    build_timeout_minutes: 90 # build only: per entry
    smoke: true               # build only: run tools/smoke_test.py on success
    image: null                # build only: $SYNOS_BUILDER_IMAGE equivalent
    pull: false                # build only
    container_root: null       # build only, podman only: SYNOS_CONTAINER_ROOT equivalent; also where free disk is measured
    container_runroot: null    # build only, podman only: SYNOS_CONTAINER_RUNROOT equivalent
    report_url: null           # optional: POST the finished report here

Never invents a shell command from a name read out of a catalog: every
process here is started from an argument vector. Never runs a real build
or hits a real network in its own test suite (tests/unit/test_catalog_conformance.py);
both are behind pluggable fetch/inspect functions a test replaces with a fake.

`tools/synos build` applies a bundle's files into whatever checkout its own
`tools/synos` script lives in; `build` here runs every entry inside a
disposable `git worktree` of this checkout (tools/scratch_checkout.py), the
same fix tools/build_matrix.py uses, so a conformance run never applies a
downloaded catalog entry's files into this checkout either.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime
import gzip
import hashlib
import importlib.machinery
import importlib.util
import json
import shutil
import subprocess
import sys
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
import scratch_checkout  # noqa: E402
import host_resources  # noqa: E402
import job_queue  # noqa: E402


class ConformanceError(Exception):
    """Configuration or a catalog fetch is unusable; nothing to record per-entry."""


class EntryUnbuildable(Exception):
    """The published entry is missing something the engine truly needs and
    nothing here can fill in (docstring: "records that honestly rather
    than skipping silently")."""


# --------------------------------------------------------------- config
@dataclasses.dataclass
class Config:
    catalog_url: str
    workdir: Path
    min_free_gb: float = MIN_FREE_GB
    min_store_gb: float = MIN_STORE_GB
    max_builds_per_run: int = DEFAULT_MAX_BUILDS
    build_timeout_minutes: float = DEFAULT_TIMEOUT_MINUTES
    jobs: str = "1"
    smoke: bool = True
    image: str | None = None
    pull: bool = False
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


# ------------------------------------------------------- entry -> bundle
def _sha256_entry(entry: dict) -> str:
    digest = hashlib.sha256()
    for path in sorted(entry.get("files", {})):
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(entry["files"][path].encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def ensure_required_fields(entry: dict, bundle_json: dict, manifest: dict) -> None:
    """Fill what a real person filling in the Studio form would have filled
    in, deterministically from the entry id; refuse (EntryUnbuildable) when
    something is missing that nothing here can invent."""
    if "manifest" not in bundle_json:
        raise EntryUnbuildable("bundle.json has no manifest key")
    if not bundle_json.get("name"):
        bundle_json["name"] = entry["id"]
    for required in ("base", "suite", "arch", "profile", "regions", "brand"):
        if not manifest.get(required):
            raise EntryUnbuildable(f"manifest is missing {required!r}, and there is no deterministic default for it")
    if not manifest.get("name"):
        manifest["name"] = entry["id"]
    if not manifest.get("version"):
        manifest["version"] = "1.0.0"
    manifest.setdefault("schema_version", 1)
    manifest.setdefault("mirrors", {})
    manifest.setdefault("packages", {"repository": "", "takeover": "all"})
    manifest.setdefault("overrides", {})


def prepare_bundle_dir(entry: dict, into: Path) -> Path:
    """Write one published catalog entry's files into `into`, filling the
    fields a person would have (docstring: "essentially the name"). Raises
    EntryUnbuildable, never silently drops the entry, when something the
    catalog was supposed to supply is missing."""
    import yaml
    files = entry.get("files") or {}
    if "bundle.json" not in files:
        raise EntryUnbuildable("no bundle.json in this entry's files")
    try:
        bundle_json = json.loads(files["bundle.json"])
    except ValueError as exc:
        raise EntryUnbuildable(f"bundle.json is not valid JSON: {exc}") from exc
    manifest_rel = bundle_json.get("manifest")
    if not manifest_rel or manifest_rel not in files:
        raise EntryUnbuildable(f"manifest {manifest_rel!r} named by bundle.json is not among this entry's files")
    try:
        manifest = yaml.safe_load(files[manifest_rel]) or {}
    except Exception as exc:  # noqa: BLE001 - yaml errors
        raise EntryUnbuildable(f"{manifest_rel} is not valid YAML: {exc}") from exc

    ensure_required_fields(entry, bundle_json, manifest)

    into.mkdir(parents=True, exist_ok=True)
    for path, content in files.items():
        target = into / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if path == "bundle.json":
            target.write_text(json.dumps(bundle_json, indent=2), encoding="utf-8")
        elif path == manifest_rel:
            target.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
        else:
            target.write_text(content, encoding="utf-8")
    return into


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


def _synos_for(scratch_path: Path) -> Path:
    """The scratch checkout's own tools/synos — its ROOT resolves to the
    scratch checkout, not this one (tests monkeypatch this name)."""
    return scratch_path / "tools" / "synos"


def build_one(entry: dict, *, config: Config, work_dir: Path, root: Path = ROOT,
             scratch_path: Path | None = None, scratch_base: Path | None = None) -> dict:
    """Builds one entry inside `scratch_path`. Without one, a scratch
    checkout is created and removed just for this call (single-entry use,
    tests); a parallel run instead passes in a scratch checkout one worker
    owns for its whole run (tools/job_queue.py's run_parallel via run_build)."""
    result = {"id": entry["id"], "kind": "catalog", "base": None, "suite": None, "checksum": _sha256_entry(entry),
              "engine": synos_engine.engine_version(), "start": _now(), "end": None, "duration_s": None,
              "exit_code": None, "status": "running", "iso": None, "log_path": None, "log_tail": [], "smoke": None}
    start = time.monotonic()
    bundle_dir = work_dir / "bundle"
    target_dir = work_dir / "output"
    log_path = target_dir / "build.log"
    result["log_path"] = str(log_path)

    resolved_profile: dict = {}
    try:
        prepare_bundle_dir(entry, bundle_dir)
        manifest_rel = json.loads((bundle_dir / "bundle.json").read_text(encoding="utf-8"))["manifest"]
        manifest = render_manifest.load_yaml(bundle_dir / manifest_rel)
        result["base"], result["suite"] = manifest["base"], manifest["suite"]
    except EntryUnbuildable as exc:
        result["status"] = "unbuildable"
        result["log_tail"] = [str(exc)]
        result["end"] = _now()
        result["duration_s"] = round(time.monotonic() - start, 1)
        return result

    owns_scratch = scratch_path is None
    payload: dict = {}
    try:
        # bundle_dir already lives outside `root` (prepare_bundle_dir wrote it
        # under work_dir); what must not be `root` is the tools/synos that
        # applies it, since `apply_bundle` always writes into whatever
        # checkout *that script* lives in, not wherever bundle_dir is.
        if owns_scratch:
            scratch_base = scratch_base or (work_dir / "scratch")
            scratch_path = scratch_checkout.create(root, scratch_base, label=entry["id"])
        argv = [sys.executable, str(_synos_for(scratch_path)), "--json", "build", str(bundle_dir),
                "--output", str(target_dir), "--log", str(log_path)]
        if config.image:
            argv += ["--image", config.image]
        if config.pull:
            argv.append("--pull")
        if config.container_root:
            argv += ["--container-root", config.container_root]
        if config.container_runroot:
            argv += ["--container-runroot", config.container_runroot]
        completed = subprocess.run(argv, cwd=scratch_path, capture_output=True, text=True,
                                   timeout=config.build_timeout_minutes * 60, check=False)
        result["exit_code"] = completed.returncode
        try:
            payload = json.loads(completed.stdout) if completed.stdout.strip() else {}
        except ValueError:
            payload = {}
        if completed.returncode == 0:
            result["status"] = "success"
        else:
            result["status"] = {1: "invalid", 2: "host", 3: "build_failed"}.get(completed.returncode, "failed")
            result["log_tail"] = _tail(log_path) or completed.stderr.strip().splitlines()[-LOG_TAIL_LINES:]
    except subprocess.TimeoutExpired:
        result["status"] = "timeout"
        result["log_tail"] = _tail(log_path)
    except scratch_checkout.ScratchCheckoutError as exc:
        result["status"] = "error"
        result["log_tail"] = [f"could not create a scratch checkout: {exc}"]
    except Exception as exc:  # noqa: BLE001 - one entry's crash must not abort the run
        result["status"] = "error"
        result["log_tail"] = [f"{type(exc).__name__}: {exc}"]
    finally:
        if owns_scratch and scratch_path is not None:
            scratch_checkout.remove(scratch_path)

    result["end"] = _now()
    result["duration_s"] = round(time.monotonic() - start, 1)

    iso_str = payload.get("iso")
    if iso_str and Path(iso_str).is_file():
        iso_path = Path(iso_str)
        result["iso"] = {"path": str(iso_path), "size": iso_path.stat().st_size, "sha256": _sha256_file(iso_path)}

    if result["status"] == "success" and config.smoke:
        if result["iso"]:
            try:
                base_dir = ROOT / "bases" / manifest["base"]
                base = render_manifest.load_env(base_dir / "base.env")
                profile_text = (bundle_dir / f"profiles/{manifest['profile']}.yml").read_text(encoding="utf-8") \
                    if (bundle_dir / f"profiles/{manifest['profile']}.yml").is_file() else None
                resolved_profile = resolve_published_profile(manifest["profile"], profile_text)
            except Exception as exc:  # noqa: BLE001
                result["smoke"] = {"status": "error", "reason": f"could not resolve profile: {exc}"}
            if result["smoke"] is None:
                result["smoke"] = smoke_test.run(Path(result["iso"]["path"]), resolved_profile, output_dir=target_dir)
        else:
            result["smoke"] = {"status": "skipped", "reason": "build succeeded but produced no ISO"}

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


def run_build(config: Config, *, catalog: dict, max_builds: int | None = None, jobs: int = 1) -> dict:
    entries = catalog["bundle_catalog"]
    limit = max_builds if max_builds is not None else config.max_builds_per_run
    selected = entries[:limit] if limit else entries
    report = {"mode": "build", "generated": _now(), "engine": synos_engine.engine_version(),
              "catalog_url": config.catalog_url, "targets": {}}

    for entry in selected:
        work_dir = config.workdir / "work" / entry["id"]
        if work_dir.exists():
            shutil.rmtree(work_dir)

    # Always goes through job_queue.run_parallel, even at jobs=1: one worker
    # still owns one scratch checkout across every entry it builds, the same
    # reuse tools/build_matrix.py's run_targets_in_parallel gives a sequential
    # run. Each worker owns one scratch checkout for the whole run (tools/job_queue.py,
    # tools/scratch_checkout.py) — the same treatment tools/build_matrix.py gives
    # a parallel matrix run, and for the same reason: apply_bundle always writes
    # into whatever checkout its own tools/synos lives in, so two concurrent
    # builds must never share one, and reusing it across a worker's own entries
    # is that worker's build cache (docs/BUILD_MATRIX.md).
    scratch_root_dir = config.workdir / "scratch"
    worker_scratch: dict[int, Path] = {}

    def build_one_for_worker(ref: _EntryRef, worker_id: int) -> dict:
        scratch_path = worker_scratch.get(worker_id)
        if scratch_path is None:
            scratch_path = scratch_checkout.create(ROOT, scratch_root_dir, label=f"worker-{worker_id}")
            worker_scratch[worker_id] = scratch_path
        work_dir = config.workdir / "work" / ref.id
        return build_one(ref.entry, config=config, work_dir=work_dir, scratch_path=scratch_path)

    try:
        results = job_queue.run_parallel([_EntryRef(entry) for entry in selected], jobs, build_one_for_worker)
    finally:
        for scratch_path in worker_scratch.values():
            scratch_checkout.remove(scratch_path)
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
    """Best-effort delivery: a failure here is a warning, never an exception
    a caller has to handle. The warning print itself is wrapped too, so a
    closed or redirected stderr (as a caller might do while capturing
    output) still cannot turn "could not deliver a report" into a crash."""
    poster = poster or _http_post
    try:
        poster(url, json.dumps(report).encode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - delivery is best-effort, never fatal to the run
        try:
            print(f"warning: could not POST the report to {url}: {exc}", file=sys.stderr)
        except Exception:  # noqa: BLE001 - even the warning must never raise
            pass


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
    return "\n".join(lines)


# ---------------------------------------------------------------- CLI
def _run(mode: str, config: Config, *, max_builds: int | None = None) -> int:
    catalog = fetch_catalog(config.catalog_url)
    config.workdir.mkdir(parents=True, exist_ok=True)
    report_path = config.workdir / f"{mode}-report.json"
    previous = json.loads(report_path.read_text(encoding="utf-8")) if report_path.is_file() else None

    if mode == "build":
        jobs, problems = resolve_jobs(config)
        if problems:
            for problem in problems:
                print(f"error: {problem}", file=sys.stderr)
            return 2
        report = run_build(config, catalog=catalog, max_builds=max_builds, jobs=jobs)
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
    args = parser.parse_args(argv)

    try:
        config = Config.load(args.config)
    except ConformanceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        try:
            catalog = fetch_catalog(config.catalog_url)
        except ConformanceError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        entries = catalog["bundle_catalog"]
        if args.mode == "build":
            entries = entries[:args.max] if args.max else entries[:config.max_builds_per_run]
        for entry in entries:
            print(entry["id"])
        print(f"\n{len(entries)} entr(y/ies) against {config.catalog_url}")
        return 0

    try:
        return _run(args.mode, config, max_builds=getattr(args, "max", None))
    except ConformanceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
