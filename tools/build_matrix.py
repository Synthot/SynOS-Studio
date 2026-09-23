#!/usr/bin/env python3
"""build_matrix — prove every catalogued bundle and every base/suite core
builds, end to end, through the engine's own path.

    python3 tools/build_matrix.py --list
    python3 tools/build_matrix.py --dry-run
    python3 tools/build_matrix.py [--only ID[,ID...]] [--base debian,ubuntu]
                                   [--suite trixie,noble] [--kind {catalog,core}]
                                   [--jobs N] [--timeout MINUTES] [--pull]
                                   [--no-smoke] [--output DIR] [--resume]

Two kinds of target:

  catalog   every bundle-catalog/index.yml entry, built with its own pinned
            base and suite (bundle-catalog/<folder>, docs/BUNDLE.md)
  core      every (base, suite) combination this engine supports, built once
            each on the engine's own default profile (manifest.yml's
            `profile:`) — "the distro cores"

Each target is built exactly the way a person would build it:
`tools/synos build <bundle dir or manifest.yml>`, one target at a time by
default. A smoke test (tools/smoke_test.py) then boots the ISO when the
build succeeded and qemu-system-x86_64/xorriso are on PATH; its result is
recorded alongside the build's, never fed back into the build's own
exit code.

`tools/synos build` applies a bundle's files into whatever checkout its own
`tools/synos` script lives in before building; invoked as *this* checkout's
`tools/synos` that would leave a `profiles/<id>.yml` behind per catalog
entry. Every build here instead runs inside a disposable `git worktree` of
this checkout (tools/scratch_checkout.py), so this checkout is left exactly
as this run found it — verified by `tests/unit/test_build_matrix.py`'s own
`git status --porcelain` check — no matter how many targets run or how the
run ends, killed or not.

The plan, the guard and the report never need the network; a build already
does, and nothing here asks for more than that.

State: --output/report.json (default dist/matrix/report.json) is written
after every target finishes, so killing this run loses at most the target
that was in flight, never the ones already recorded. --resume re-reads that
file and skips a target whose last recorded result was "success" for the
exact same checksum; everything else (never run, failed, timed out, or
whose catalog entry/base changed since) is retried.

Never builds a shell command from a name read out of a file: every process
here is started from an argument vector.

Exit code: 0 when every target that was attempted (built, not skipped)
succeeded; 1 when at least one failed, timed out, or errored; 2 when the
host cannot even start (disk, --jobs, missing engine).
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SYNOS = ROOT / "tools" / "synos"
CORE_MANIFEST_DIR = ROOT / ".build" / "matrix" / "manifests"

DEFAULT_TIMEOUT_MINUTES = 90
DEFAULT_OUTPUT = ROOT / "dist" / "matrix"
LOG_TAIL_LINES = 60

EXIT_OK, EXIT_FAILURES, EXIT_HOST = 0, 1, 2


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_loader(name, importlib.machinery.SourceFileLoader(name, str(path)))
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


synos_engine = _load("synos_engine", SYNOS)
render_manifest = _load("render_manifest_bm", ROOT / "tools" / "render_manifest.py")

sys.path.insert(0, str(ROOT / "tools"))
import smoke_test  # noqa: E402
import scratch_checkout  # noqa: E402
import host_resources  # noqa: E402
import job_queue  # noqa: E402


# --------------------------------------------------------------- planning
class Target:
    __slots__ = ("id", "kind", "base", "suite", "source", "checksum", "profile_id")

    def __init__(self, id: str, kind: str, base: str, suite: str, source: Path, checksum: str, profile_id: str):
        self.id = id
        self.kind = kind
        self.base = base
        self.suite = suite
        self.source = source
        self.checksum = checksum
        self.profile_id = profile_id

    def as_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "base": self.base, "suite": self.suite,
                "source": str(self.source), "checksum": self.checksum, "profile": self.profile_id}


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_tree(folder: Path) -> str:
    """One checksum for a bundle folder: sorted (path, bytes) of every file in
    it, so a catalog edit (a summary, a pinned image tag) changes it and
    --resume rebuilds; unrelated repository changes do not."""
    digest = hashlib.sha256()
    for path in sorted(p for p in folder.rglob("*") if p.is_file()):
        digest.update(path.relative_to(folder).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _bases(root: Path) -> list[tuple[str, list[str]]]:
    """(base_id, [suite, ...]) for every real base adapter (bases/_template excluded)."""
    out = []
    for base_dir in sorted(p for p in (root / "bases").iterdir() if p.is_dir() and not p.name.startswith("_")):
        env = render_manifest.load_env(base_dir / "base.env")
        suites = env.get("SUPPORTED_SUITES", env.get("DEFAULT_SUITE", "")).split() or [env.get("DEFAULT_SUITE", "")]
        out.append((env["BASE_ID"], suites))
    return out


def plan_catalog_targets(root: Path) -> list[Target]:
    index = render_manifest.load_yaml(root / "bundle-catalog" / "index.yml")
    targets = []
    for entry in index.get("bundle_catalog", []):
        folder = root / "bundle-catalog" / entry["folder"]
        descriptor = json.loads((folder / "bundle.json").read_text(encoding="utf-8"))
        manifest = render_manifest.load_yaml(folder / descriptor["manifest"])
        targets.append(Target(
            id=entry["id"], kind="catalog", base=manifest["base"], suite=manifest["suite"],
            source=folder, checksum=_sha256_tree(folder), profile_id=manifest.get("profile", entry["id"]),
        ))
    return targets


def _default_profile(root: Path) -> str:
    return render_manifest.load_yaml(root / "manifest.yml")["profile"]


def _write_core_manifest(root: Path, base: str, suite: str, profile: str) -> tuple[Path, str]:
    """A lean manifest for one base/suite core build: the engine's default
    profile, one region, no third-party repository — the same shape a
    catalog entry's own manifest keeps. Written under .build/ (git-ignored,
    same as args.sh and the render cache) because tools/synos build requires
    the manifest to live inside the checkout."""
    manifest = {
        "schema_version": 1, "name": f"synos-core-{base}-{suite}", "version": "1.0.0",
        "base": base, "suite": suite, "arch": "amd64",
        "profile": profile, "regions": ["us"], "brand": "synos",
        "mirrors": {}, "packages": {"repository": "", "takeover": "all"}, "overrides": {},
    }
    checksum = _sha256_bytes(json.dumps(manifest, sort_keys=True).encode("utf-8"))
    CORE_MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    path = CORE_MANIFEST_DIR / f"core-{base}-{suite}.yml"
    import yaml
    path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    return path, checksum


def plan_core_targets(root: Path) -> list[Target]:
    profile = _default_profile(root)
    targets = []
    for base, suites in _bases(root):
        for suite in suites:
            path, checksum = _write_core_manifest(root, base, suite, profile)
            targets.append(Target(id=f"core-{base}-{suite}", kind="core", base=base, suite=suite,
                                  source=path, checksum=checksum, profile_id=profile))
    return targets


def plan_targets(root: Path) -> list[Target]:
    return plan_catalog_targets(root) + plan_core_targets(root)


def select_targets(targets: list[Target], *, only: list[str] | None, base: list[str] | None,
                    suite: list[str] | None, kind: str | None) -> list[Target]:
    selected = targets
    if kind:
        selected = [t for t in selected if t.kind == kind]
    if base:
        selected = [t for t in selected if t.base in base]
    if suite:
        selected = [t for t in selected if t.suite in suite]
    if only:
        wanted = set(only)
        selected = [t for t in selected if t.id in wanted]
        missing = wanted - {t.id for t in selected}
        if missing:
            raise SystemExit(f"--only names targets that do not exist: {', '.join(sorted(missing))}")
    return selected


# ------------------------------------------------------------- host guard
def resolve_jobs(requested: str) -> tuple[int, list[str]]:
    """--jobs "auto" derives the safe count from this machine's own disk,
    memory and CPUs (tools/host_resources.py, the same formula
    tools/catalog_conformance.py uses); an explicit count above that is
    refused with the reason, not silently capped. Also refuses when there
    is no container engine at all, which no job count helps."""
    jobs, problems = host_resources.resolve_jobs(requested, ROOT)
    if not host_resources.container_engine():
        problems.append("podman or docker is required")
    return jobs, problems


# -------------------------------------------------------------- execution
def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _tail(path: Path, lines: int = LOG_TAIL_LINES) -> list[str]:
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return text.splitlines()[-lines:]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_bundle_profile(profile_id: str, own_text: str | None) -> dict:
    """A profile as one bundle-catalog folder ships it, `extends` merged the
    way tools/render_manifest.py merges it, read straight from that folder's
    own profiles/<id>.yml (own_text) rather than from this checkout's
    profiles/ — nothing is ever applied into this checkout any more, so its
    own profiles/ never carries a catalog entry's profile to read back."""
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


def _resolved_profile(target: Target) -> dict:
    if target.kind != "catalog":
        resolved, _chain = render_manifest.resolve_profile(target.profile_id)
        return resolved
    profile_path = target.source / "profiles" / f"{target.profile_id}.yml"
    own_text = profile_path.read_text(encoding="utf-8") if profile_path.is_file() else None
    return _resolve_bundle_profile(target.profile_id, own_text)


def _synos_for(scratch_path: Path) -> Path:
    """The scratch checkout's own tools/synos — its ROOT resolves to the
    scratch checkout, not this one, which is the entire point (tests
    monkeypatch this name to stand in a fake build without needing a real
    container runtime)."""
    return scratch_path / "tools" / "synos"


def _materialize_source(target: Target, root: Path, scratch_path: Path) -> Path:
    """Where to point `synos build` inside the scratch checkout. A catalog
    entry's bundle-catalog/<folder> is an ordinary tracked directory, so the
    worktree already has it checked out; a core target's manifest lives
    under this checkout's own .build/ (git-ignored, written at plan time),
    which a worktree of a commit never carries, so its exact text is copied
    into the scratch checkout's own .build/ before building."""
    relative = target.source.relative_to(root)
    if target.kind == "core":
        dest = scratch_path / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(target.source.read_text(encoding="utf-8"), encoding="utf-8")
        return dest
    return scratch_path / relative


def run_one(target: Target, *, output_dir: Path, timeout_seconds: int, image: str | None,
            pull: bool, run_smoke: bool, root: Path = ROOT, scratch_path: Path | None = None,
            scratch_base: Path | None = None) -> dict:
    """Builds one target inside `scratch_path`. When `scratch_path` is not
    given, a scratch checkout is created and removed just for this one call
    (used directly by tests and by anything building a single target); a
    parallel run instead passes in a scratch checkout one worker owns for
    its whole run (tools/job_queue.py's run_parallel), so several targets
    from the same worker share and reuse its cache without ever sharing a
    checkout across workers."""
    target_dir = output_dir / "targets" / target.id
    target_dir.mkdir(parents=True, exist_ok=True)
    log_path = target_dir / "build.log"
    start = time.monotonic()
    result = {**target.as_dict(), "engine": synos_engine.engine_version(), "start": _now(), "end": None,
              "duration_s": None, "exit_code": None, "status": "running", "iso": None, "log_path": str(log_path),
              "log_tail": [], "smoke": None}

    owns_scratch = scratch_path is None
    payload: dict = {}
    try:
        if owns_scratch:
            scratch_base = scratch_base or (output_dir / "scratch")
            scratch_path = scratch_checkout.create(root, scratch_base, label=target.id)
        build_source = _materialize_source(target, root, scratch_path)
        argv = [sys.executable, str(_synos_for(scratch_path)), "--json", "build", str(build_source),
                "--output", str(target_dir), "--log", str(log_path)]
        if image:
            argv += ["--image", image]
        if pull:
            argv.append("--pull")
        completed = subprocess.run(argv, cwd=scratch_path, capture_output=True, text=True,
                                   timeout=timeout_seconds, check=False)
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
        result["exit_code"] = None
        result["log_tail"] = _tail(log_path)
    except scratch_checkout.ScratchCheckoutError as exc:
        result["status"] = "error"
        result["log_tail"] = [f"could not create a scratch checkout: {exc}"]
    except Exception as exc:  # noqa: BLE001 - one target's crash must not abort the run
        result["status"] = "error"
        result["log_tail"] = [f"{type(exc).__name__}: {exc}"]
    finally:
        if owns_scratch and scratch_path is not None:
            scratch_checkout.remove(scratch_path)

    end = time.monotonic()
    result["end"] = _now()
    result["duration_s"] = round(end - start, 1)

    iso_str = payload.get("iso")
    if iso_str:
        iso_path = Path(iso_str)
        if iso_path.is_file():
            result["iso"] = {"path": str(iso_path), "size": iso_path.stat().st_size, "sha256": _sha256_file(iso_path)}

    if result["status"] == "success" and run_smoke:
        if result["iso"]:
            try:
                profile = _resolved_profile(target)
            except Exception as exc:  # noqa: BLE001
                profile = {}
                result["smoke"] = {"status": "error", "reason": f"could not resolve profile {target.profile_id!r}: {exc}"}
            if result["smoke"] is None:
                result["smoke"] = smoke_test.run(Path(result["iso"]["path"]), profile, output_dir=target_dir)
        else:
            result["smoke"] = {"status": "skipped", "reason": "build succeeded but produced no ISO"}

    return result


# ------------------------------------------------------------------ state
_STATE_LOCK = threading.Lock()


def _load_report(path: Path) -> dict:
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            pass
    return {"generated": None, "engine": None, "targets": {}}


def _write_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    report["generated"] = _now()
    report["engine"] = synos_engine.engine_version()
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".report-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def update_build_status(root: Path, targets_by_id: dict, report: dict) -> None:
    """bundle-catalog/build-status.yml: the honest "last built" record
    tools/export_catalog.py merges into bundle_catalog[]. Catalog targets
    only — a core build proves the base, not a catalogued bundle. An entry
    that has never been built (or whose last run only failed to start, e.g.
    a host problem) is never written here in a way that looks like success."""
    status_path = root / "bundle-catalog" / "build-status.yml"
    import yaml
    data = {}
    if status_path.is_file():
        loaded = yaml.safe_load(status_path.read_text(encoding="utf-8")) or {}
        data = loaded.get("build_status", {}) or {}
    for target_id, target in targets_by_id.items():
        if target.kind != "catalog":
            continue
        record = report["targets"].get(target_id)
        if not record or record["status"] == "running":
            continue
        entry = {"engine": record["engine"], "date": record["end"] or record["start"], "result": record["status"],
                  "duration_s": record["duration_s"]}
        if record.get("iso"):
            entry["iso_sha256"] = record["iso"]["sha256"]
        if record.get("smoke"):
            entry["smoke"] = record["smoke"]["status"]
        data[target_id] = entry
    header = ("# Honest \"last built\" status per catalogued bundle, written by\n"
              "# tools/build_matrix.py after a matrix run (never by hand). An id absent\n"
              "# from this file has never been built: absent, not false. Merged into\n"
              "# bundle_catalog[].build_status by tools/export_catalog.py.\n")
    status_path.write_text(header + yaml.safe_dump({"build_status": data}, sort_keys=True), encoding="utf-8")


def targets_to_run(targets: list["Target"], report: dict, *, resume: bool) -> list["Target"]:
    """Which planned targets this run should actually build: every target when
    not resuming; with --resume, every target except one whose last recorded
    result was success at the exact checksum being planned now (docstring:
    "skips what already succeeded and retries what failed or never ran")."""
    if not resume:
        return list(targets)
    to_run = []
    for target in targets:
        previous = report["targets"].get(target.id)
        if previous and previous.get("status") == "success" and previous.get("checksum") == target.checksum:
            continue
        to_run.append(target)
    return to_run


def run_targets_in_parallel(to_run: list["Target"], *, output_dir: Path, timeout_seconds: int, image: str | None,
                            pull: bool, run_smoke: bool, jobs: int, root: Path = ROOT,
                            scratch_root_dir: Path | None = None, on_result=None) -> dict:
    """Runs `to_run` with `jobs` workers (tools/job_queue.py). Each worker
    owns exactly one scratch checkout (tools/scratch_checkout.py) for this
    call's whole lifetime, created the first time that worker is handed a
    target and removed only once every target is done — reused across every
    target that worker builds, never shared with another worker. That reuse
    is this project's per-worker build cache (apt lists, downloaded
    packages: whatever of .build/ survives between builds), the isolation
    that keeps two concurrent builds from colliding the way two `./build.sh`
    runs on one bundle once did (docs/BUILD_MATRIX.md)."""
    scratch_root_dir = scratch_root_dir or (output_dir / "scratch")
    worker_scratch: dict[int, Path] = {}

    def build_one(target: "Target", worker_id: int) -> dict:
        scratch_path = worker_scratch.get(worker_id)
        if scratch_path is None:
            scratch_path = scratch_checkout.create(root, scratch_root_dir, label=f"worker-{worker_id}")
            worker_scratch[worker_id] = scratch_path
        return run_one(target, output_dir=output_dir, timeout_seconds=timeout_seconds, image=image,
                       pull=pull, run_smoke=run_smoke, root=root, scratch_path=scratch_path)

    try:
        return job_queue.run_parallel(to_run, jobs, build_one, on_result=on_result)
    finally:
        for scratch_path in worker_scratch.values():
            scratch_checkout.remove(scratch_path)


# --------------------------------------------------------------- summary
def human_summary(report: dict) -> str:
    lines = [f"{'target':<28} {'kind':<8} {'result':<12} {'duration':>9} {'iso size':>10}"]
    lines.append("-" * len(lines[0]))
    for target_id in sorted(report["targets"]):
        record = report["targets"][target_id]
        duration = f"{record['duration_s']:.0f}s" if record.get("duration_s") is not None else "-"
        size = record.get("iso", {}).get("size") if record.get("iso") else None
        size_str = f"{size / 1024**3:.1f} GB" if size else "-"
        lines.append(f"{target_id:<28} {record.get('kind', '-'):<8} {record.get('status', '-'):<12} {duration:>9} {size_str:>10}")
    counts: dict[str, int] = {}
    for record in report["targets"].values():
        counts[record.get("status", "-")] = counts.get(record.get("status", "-"), 0) + 1
    lines.append("")
    lines.append(", ".join(f"{count} {status}" for status, count in sorted(counts.items())))
    return "\n".join(lines)


# ---------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="build_matrix", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", action="store_true", help="print the planned targets and exit")
    parser.add_argument("--dry-run", action="store_true", help="print the plan (and the command each target would run) without building")
    parser.add_argument("--only", help="comma-separated target ids to build")
    parser.add_argument("--base", help="comma-separated base ids to restrict to")
    parser.add_argument("--suite", help="comma-separated suites to restrict to")
    parser.add_argument("--kind", choices=["catalog", "core"], help="restrict to one kind of target")
    parser.add_argument("--jobs", default="1",
                        help="builds to run at once: a number, or \"auto\" to derive the safe count from this "
                             "machine's disk, memory and CPUs (tools/host_resources.py); default 1")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_MINUTES, help="per-build timeout in minutes")
    parser.add_argument("--image", default=os.environ.get("SYNOS_BUILDER_IMAGE"),
                        help="builder image for every target (default: $SYNOS_BUILDER_IMAGE, else the engine's own selection)")
    parser.add_argument("--pull", action="store_true", help="pull the published builder image instead of building it locally")
    parser.add_argument("--no-smoke", action="store_true", help="skip tools/smoke_test.py even when a build succeeds")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="where report.json, logs and ISOs go (default dist/matrix/)")
    parser.add_argument("--resume", action="store_true", help="skip targets whose last recorded result was success at this checksum")
    args = parser.parse_args(argv)

    only = args.only.split(",") if args.only else None
    base = args.base.split(",") if args.base else None
    suite = args.suite.split(",") if args.suite else None

    targets = select_targets(plan_targets(ROOT), only=only, base=base, suite=suite, kind=args.kind)
    targets.sort(key=lambda t: t.id)

    if args.list or args.dry_run:
        for target in targets:
            print(f"{target.id:<28} {target.kind:<8} {target.base:<8} {target.suite:<10} {target.source}")
        print(f"\n{len(targets)} target(s)")
        if args.dry_run:
            timeout_seconds = int(args.timeout * 60)
            safe_jobs, factors = host_resources.derive_safe_jobs(ROOT)
            print(f"jobs={args.jobs} (auto would use {safe_jobs}: {host_resources.explain(factors)}) "
                  f"timeout={args.timeout:.0f}m image={args.image or '(engine default)'} "
                  f"pull={args.pull} smoke={not args.no_smoke} output={args.output}")
            for target in targets:
                # The real synos path and build source are only known once a
                # disposable scratch checkout exists (run_one, tools/scratch_checkout.py);
                # this previews the shape of that command, not a literal one.
                relative_source = target.source.relative_to(ROOT)
                argv_preview = ["<scratch-checkout>/tools/synos", "--json", "build", f"<scratch-checkout>/{relative_source}",
                                "--output", str(args.output / 'targets' / target.id), "--log",
                                str(args.output / 'targets' / target.id / 'build.log')]
                if args.image:
                    argv_preview += ["--image", args.image]
                if args.pull:
                    argv_preview.append("--pull")
                print("  " + " ".join(argv_preview))
        return EXIT_OK

    jobs, problems = resolve_jobs(args.jobs)
    if problems:
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr)
        return EXIT_HOST

    output_dir = args.output
    report_path = output_dir / "report.json"
    report = _load_report(report_path) if args.resume else {"generated": None, "engine": None, "targets": {}}

    targets_by_id = {t.id: t for t in targets}
    to_run = targets_to_run(targets, report, resume=args.resume)
    for target in to_run:
        report["targets"][target.id] = {**target.as_dict(), "status": "pending", "start": None, "end": None,
                                        "duration_s": None, "exit_code": None, "iso": None, "log_path": None,
                                        "log_tail": [], "smoke": None, "engine": synos_engine.engine_version()}
    _write_report(report_path, report)

    timeout_seconds = int(args.timeout * 60)

    def _on_result(target: Target, record: dict) -> None:
        with _STATE_LOCK:
            report["targets"][target.id] = record
            _write_report(report_path, report)
            update_build_status(ROOT, targets_by_id, report)
        duration = record.get("duration_s")
        suffix = f" ({duration:.0f}s)" if duration is not None else ""
        print(f"{target.id}: {record['status']}{suffix}", file=sys.stderr)

    if to_run:
        print(f"building {len(to_run)} target(s) with {jobs} worker(s)", file=sys.stderr)
        run_targets_in_parallel(to_run, output_dir=output_dir, timeout_seconds=timeout_seconds, image=args.image,
                                pull=args.pull, run_smoke=not args.no_smoke, jobs=jobs, root=ROOT, on_result=_on_result)

    _write_report(report_path, report)
    summary_text = human_summary(report)
    (output_dir / "summary.txt").write_text(summary_text + "\n", encoding="utf-8")
    print(summary_text)

    failed = [tid for tid, record in report["targets"].items()
             if record.get("status") not in ("success", "pending") and tid in {t.id for t in to_run}]
    return EXIT_OK if not failed else EXIT_FAILURES


if __name__ == "__main__":
    sys.exit(main())
