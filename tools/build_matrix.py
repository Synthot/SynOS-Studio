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
import concurrent.futures
import datetime
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SYNOS = ROOT / "tools" / "synos"
CORE_MANIFEST_DIR = ROOT / ".build" / "matrix" / "manifests"

MIN_FREE_GB = 40          # tools/synos MIN_FREE_GB: the working checkout
MIN_STORE_GB = 30         # bundle_launcher.sh: the container runtime's own storage
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
def _container_engine() -> str | None:
    return shutil.which("podman") or shutil.which("docker")


def _container_store(engine: str) -> str | None:
    for fmt in ("{{.DockerRootDir}}", "{{.Store.GraphRoot}}"):
        result = subprocess.run([engine, "info", "--format", fmt], capture_output=True, text=True, check=False, timeout=20)
        value = result.stdout.strip()
        if result.returncode == 0 and value and "{{" not in value and value != "<no value>":
            return value
    return None


def guard_host(jobs: int) -> list[str]:
    """Refuses a run this machine cannot feed: not enough disk here, not
    enough in the container runtime's own storage, no engine at all, or a
    --jobs count this checkout has no evidence it can sustain (tools/synos
    MIN_FREE_GB, bundle_launcher.sh's storage check, both scaled by jobs)."""
    problems = []
    if jobs < 1:
        problems.append("--jobs must be at least 1")
    cpu = os.cpu_count() or 1
    if jobs > cpu:
        problems.append(f"--jobs {jobs} exceeds {cpu} CPUs available; each build already uses several")
    free_root = shutil.disk_usage(ROOT).free / 1024**3
    need_root = MIN_FREE_GB * max(jobs, 1)
    if free_root < need_root:
        problems.append(f"at least {need_root:.0f} GB free is needed under {ROOT} for {jobs} concurrent build(s); {free_root:.0f} GB available")
    engine = _container_engine()
    if not engine:
        problems.append("podman or docker is required")
    else:
        store = _container_store(engine)
        if store and Path(store).is_dir():
            free_store = shutil.disk_usage(store).free / 1024**3
            need_store = MIN_STORE_GB * max(jobs, 1)
            if free_store < need_store:
                problems.append(f"the container runtime keeps its storage under {store}, "
                                f"which has {free_store:.0f} GB free; {need_store:.0f} GB are needed for {jobs} concurrent build(s)")
    return problems


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


def _resolved_profile(profile_id: str) -> dict:
    resolved, _chain = render_manifest.resolve_profile(profile_id)
    return resolved


def run_one(target: Target, *, output_dir: Path, timeout_seconds: int, image: str | None,
            pull: bool, run_smoke: bool) -> dict:
    target_dir = output_dir / "targets" / target.id
    target_dir.mkdir(parents=True, exist_ok=True)
    log_path = target_dir / "build.log"
    start = time.monotonic()
    result = {**target.as_dict(), "engine": synos_engine.engine_version(), "start": _now(), "end": None,
              "duration_s": None, "exit_code": None, "status": "running", "iso": None, "log_path": str(log_path),
              "log_tail": [], "smoke": None}

    argv = [sys.executable, str(SYNOS), "--json", "build", str(target.source), "--output", str(target_dir), "--log", str(log_path)]
    if image:
        argv += ["--image", image]
    if pull:
        argv.append("--pull")

    payload: dict = {}
    try:
        completed = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, timeout=timeout_seconds, check=False)
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
    except Exception as exc:  # noqa: BLE001 - one target's crash must not abort the run
        result["status"] = "error"
        result["log_tail"] = [f"{type(exc).__name__}: {exc}"]

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
                profile = _resolved_profile(target.profile_id)
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
    parser.add_argument("--jobs", type=int, default=1, help="builds to run at once (default 1)")
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
            print(f"jobs={args.jobs} timeout={args.timeout:.0f}m image={args.image or '(engine default)'} "
                  f"pull={args.pull} smoke={not args.no_smoke} output={args.output}")
            for target in targets:
                argv_preview = [str(SYNOS), "--json", "build", str(target.source), "--output",
                                str(args.output / 'targets' / target.id), "--log",
                                str(args.output / 'targets' / target.id / 'build.log')]
                if args.image:
                    argv_preview += ["--image", args.image]
                if args.pull:
                    argv_preview.append("--pull")
                print("  " + " ".join(argv_preview))
        return EXIT_OK

    problems = guard_host(args.jobs)
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

    def _run_and_record(target: Target) -> None:
        record = run_one(target, output_dir=output_dir, timeout_seconds=timeout_seconds, image=args.image,
                          pull=args.pull, run_smoke=not args.no_smoke)
        with _STATE_LOCK:
            report["targets"][target.id] = record
            _write_report(report_path, report)
            update_build_status(ROOT, targets_by_id, report)
        print(f"{target.id}: {record['status']} ({record['duration_s']:.0f}s)", file=sys.stderr)

    if to_run:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(args.jobs, 1)) as pool:
            list(pool.map(_run_and_record, to_run))

    _write_report(report_path, report)
    summary_text = human_summary(report)
    (output_dir / "summary.txt").write_text(summary_text + "\n", encoding="utf-8")
    print(summary_text)

    failed = [tid for tid, record in report["targets"].items()
             if record.get("status") not in ("success", "pending") and tid in {t.id for t in to_run}]
    return EXIT_OK if not failed else EXIT_FAILURES


if __name__ == "__main__":
    sys.exit(main())
