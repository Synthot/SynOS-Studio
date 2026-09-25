#!/usr/bin/env python3
"""run_checks — the pre-push routine in one sequential pass, instead of four
or five commands run by hand across two checkouts.

    python3 tools/run_checks.py [--level {fast,unit,all}] [--only a,b]
                                 [--list] [--fail-fast] [--json]
                                 [--log-dir DIR] [--studio-repo PATH]

Stages run one at a time, in a fixed, deliberate order: cheapest and most
likely to fail first (a manifest that does not validate makes the six-minute
unit suite pointless), the long unit suite last among what --level selects.

Levels:
    fast   seconds-long checks only: manifest validation, a shell syntax
           pass, python3 -m compileall, and an offline bundle-catalog
           validation loop.
    unit   fast, plus the full unit suite under tests/unit/ (default).
    all    unit, plus anything else that is safe to run unattended on any
           machine (right now: the separate front end's own unit suite,
           when --studio-repo/SYNOS_STUDIO_REPO is given).

Nothing that builds an image, boots QEMU, pulls a container image or needs
the network ever runs at any --level. The one stage here that drives a real
browser (studio-e2e) is never selected by --level at all; it exists only
for --only, with its cost stated in its own description. It does report a
real verdict, so --only studio-e2e can be trusted as a gate; it is simply
not something to pay for on every run.

A stage whose tool is genuinely absent (no node, no google-chrome, the
separate front end not configured) is reported "skipped" with the reason
and does not fail the run. A stage that is absent because *this* checkout
is broken (e.g. a --studio-repo given that does not look like one) fails
instead — a runner that silently skips what it cannot do is worse than no
runner.

Every stage's full stdout and stderr is written to its own file under the
log directory (default test-results/checks/, already git-ignored); the
summary on stdout stays one line per stage, with the last few lines of a
failure and the path to its full log.

Exit codes, this repository's own (tools/synos, tools/build_matrix.py): 0
every attempted stage passed (or was skipped), 1 at least one stage failed,
2 the run itself could not start (log directory not writable).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOG_DIR = ROOT / "test-results" / "checks"

LEVELS = ("fast", "unit", "all")
TAIL_LINES = 12

EXIT_OK, EXIT_FAILURES, EXIT_HOST = 0, 1, 2


# --------------------------------------------------------------- context
@dataclass
class Context:
    root: Path
    studio_repo: Optional[Path]
    log_dir: Path


# --------------------------------------------------------------- stages
@dataclass
class Stage:
    name: str
    description: str
    level: Optional[str]  # "fast" | "unit" | "all" | None (None: --only only)
    run: Callable[[Context], "Outcome"]
    precheck: Optional[Callable[[Context], Optional["Outcome"]]] = None


@dataclass
class Outcome:
    status: str  # "passed" | "failed" | "skipped"
    stdout: str = ""
    stderr: str = ""
    note: Optional[str] = None
    command: str = ""
    cwd: Optional[Path] = None


def _run_argv(argv: list[str], *, cwd: Path, env: Optional[dict] = None, timeout: int = 120) -> Outcome:
    try:
        completed = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, env=env, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        return Outcome(status="failed", stdout=exc.stdout or "", stderr=exc.stderr or "",
                       note=f"timed out after {timeout}s", command=" ".join(argv), cwd=cwd)
    except OSError as exc:
        return Outcome(status="failed", note=f"{type(exc).__name__}: {exc}", command=" ".join(argv), cwd=cwd)
    status = "passed" if completed.returncode == 0 else "failed"
    return Outcome(status=status, stdout=completed.stdout, stderr=completed.stderr,
                   command=" ".join(argv), cwd=cwd)


def _tail(text: str, lines: int = TAIL_LINES) -> list[str]:
    return [l for l in text.splitlines() if l.strip()][-lines:]


# ---- manifest ------------------------------------------------------------
def _run_manifest(ctx: Context) -> Outcome:
    return _run_argv([sys.executable, str(ctx.root / "tools" / "render_manifest.py"), "--check"],
                     cwd=ctx.root, timeout=60)


# ---- shell syntax ----------------------------------------------------------
def _shell_syntax_precheck(ctx: Context) -> Optional[Outcome]:
    if shutil.which("bash") is None:
        return Outcome(status="skipped", note="bash not found on PATH")
    return None


def _run_shell_syntax(ctx: Context) -> Outcome:
    files = sorted(ctx.root.glob("tools/*.sh")) + sorted(p for p in ctx.root.glob("mods/**/*.sh"))
    out, err, rc = [], [], 0
    for path in files:
        rel = path.relative_to(ctx.root).as_posix()
        completed = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True, timeout=10)
        if completed.returncode != 0:
            rc = 1
            out.append(f"{rel}: FAILED")
            err.append(f"--- {rel} ---\n{completed.stderr}")
        else:
            out.append(f"{rel}: ok")
    out.append(f"\n{len(files)} script(s) checked")
    return Outcome(status="passed" if rc == 0 else "failed", stdout="\n".join(out), stderr="\n".join(err),
                   command="bash -n <each of tools/*.sh, mods/**/*.sh>", cwd=ctx.root)


# ---- compileall ------------------------------------------------------------
def _run_compileall(ctx: Context) -> Outcome:
    return _run_argv([sys.executable, "-m", "compileall", "-q", "tools", "tests", "mods"], cwd=ctx.root, timeout=120)


# ---- catalog validate --------------------------------------------------
def _run_catalog_validate(ctx: Context) -> Outcome:
    catalog_dir = ctx.root / "bundle-catalog"
    folders = sorted(p for p in catalog_dir.iterdir() if p.is_dir() and not p.name.startswith("_")) if catalog_dir.is_dir() else []
    out, err, rc = [], [], 0
    for folder in folders:
        completed = subprocess.run([sys.executable, str(ctx.root / "tools" / "synos"), "--json", "bundle", "validate", str(folder)],
                                   cwd=ctx.root, capture_output=True, text=True, timeout=30)
        ok = False
        try:
            ok = bool(json.loads(completed.stdout).get("ok")) if completed.stdout.strip() else False
        except ValueError:
            ok = False
        if not ok:
            rc = 1
            out.append(f"{folder.name}: FAILED")
            err.append(f"--- {folder.name} ---\n{completed.stdout}\n{completed.stderr}")
        else:
            out.append(f"{folder.name}: ok")
    out.append(f"\n{len(folders)} catalog entr{'y' if len(folders) == 1 else 'ies'} validated")
    return Outcome(status="passed" if rc == 0 else "failed", stdout="\n".join(out), stderr="\n".join(err),
                   command="tools/synos --json bundle validate <each of bundle-catalog/*/>", cwd=ctx.root)


# ---- host check ------------------------------------------------------------
def _run_host_check(ctx: Context) -> Outcome:
    """tools/synos check is about *this machine*, not the code: a missing
    container engine is normal on a machine that only ever runs these
    checks, so it is reported here, never as a run failure. Any other
    problem (disk, a missing python module) means the checkout itself is
    unusable and does fail."""
    outcome = _run_argv([sys.executable, str(ctx.root / "tools" / "synos"), "--json", "check"], cwd=ctx.root, timeout=30)
    if outcome.status == "passed":
        return outcome
    try:
        problems = json.loads(outcome.stdout).get("host", {}).get("problems", [])
    except ValueError:
        problems = []
    engine_only = bool(problems) and all("podman or docker" in p for p in problems)
    if engine_only:
        outcome.status = "skipped"
        outcome.note = "; ".join(problems)
    elif problems:
        outcome.note = "; ".join(problems)
    return outcome


# ---- unit suite --------------------------------------------------------
def _run_unit_suite(ctx: Context) -> Outcome:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ctx.root / "tests")
    return _run_argv([sys.executable, "-m", "unittest", "discover", "-s", "tests/unit", "-p", "test_*.py"],
                     cwd=ctx.root, env=env, timeout=1800)


# ---- the separate front end (Studio) ------------------------------------
def _studio_base_precheck(ctx: Context) -> Optional[Outcome]:
    if ctx.studio_repo is None:
        return Outcome(status="skipped", note="the separate front end's checkout was not given "
                                              "(--studio-repo or SYNOS_STUDIO_REPO)")
    if not (ctx.studio_repo / "tests" / "test_configurator.py").is_file():
        return Outcome(status="failed",
                       note=f"--studio-repo {ctx.studio_repo} does not look like a Studio checkout "
                            "(missing tests/test_configurator.py)")
    if shutil.which("node") is None:
        return Outcome(status="skipped", note="node not found on PATH (required by the Studio suites)")
    return None


def _run_studio_unit(ctx: Context) -> Outcome:
    env = dict(os.environ)
    env["SYNOS_REPO"] = str(ctx.root)
    return _run_argv([sys.executable, "-m", "unittest", "tests.test_configurator"],
                     cwd=ctx.studio_repo, env=env, timeout=120)


def _studio_e2e_precheck(ctx: Context) -> Optional[Outcome]:
    base = _studio_base_precheck(ctx)
    if base is not None:
        return base
    if shutil.which("google-chrome") is None:
        return Outcome(status="skipped", note="google-chrome not found on PATH (required by the e2e suite)")
    return None


def _run_studio_e2e(ctx: Context) -> Outcome:
    env = dict(os.environ)
    env["SYNOS_REPO"] = str(ctx.root)
    return _run_argv([sys.executable, "tests/e2e_open_bundle.py"], cwd=ctx.studio_repo, env=env, timeout=300)


STAGES: list[Stage] = [
    Stage("manifest",
         "manifest.yml validates and its base/profile/brand chain resolves, without writing args.sh "
         "(tools/render_manifest.py --check)",
         "fast", _run_manifest),
    Stage("shell-syntax",
         "every tools/*.sh and mods/**/*.sh script parses under bash -n",
         "fast", _run_shell_syntax, _shell_syntax_precheck),
    Stage("compileall",
         "every tools/, tests/ and mods/ python file compiles (python3 -m compileall)",
         "fast", _run_compileall),
    Stage("catalog-validate",
         "every bundle-catalog/ entry validates against the bundle schema, offline "
         "(tools/synos bundle validate, looped; ~10s for the current catalog)",
         "fast", _run_catalog_validate),
    Stage("host-check",
         "this machine's own build readiness: container engine, disk, python modules "
         "(tools/synos check) — informational; a missing container engine here never fails this run",
         "fast", _run_host_check),
    Stage("unit-suite",
         "the full unit test suite, about 6 minutes; the count is whatever the suite reports, "
         "not a number kept here to drift "
         "(PYTHONPATH=tests python3 -m unittest discover -s tests/unit -p 'test_*.py')",
         "unit", _run_unit_suite),
    Stage("studio-unit",
         "the separate front end's own unit suite, about 10s; needs node and "
         "--studio-repo/SYNOS_STUDIO_REPO (python3 -m unittest tests.test_configurator)",
         "all", _run_studio_unit, _studio_base_precheck),
    Stage("studio-e2e",
         "the separate front end's real-browser end-to-end script, about 90s, drives headless "
         "google-chrome and generates real bundles at the page's own upload limits; needs "
         "--studio-repo/SYNOS_STUDIO_REPO, node and google-chrome. It exits non-zero when any of "
         "its own checks fails, so this stage's status can be trusted; kept out of --level "
         "because of the browser and the minute and a half. Reach it with --only studio-e2e.",
         None, _run_studio_e2e, _studio_e2e_precheck),
]

STAGES_BY_NAME = {s.name: s for s in STAGES}


_LEVEL_RANK = {"fast": 0, "unit": 1, "all": 2}


def stages_for_level(level: str) -> list[Stage]:
    ceiling = _LEVEL_RANK[level]
    return [s for s in STAGES if s.level is not None and _LEVEL_RANK[s.level] <= ceiling]


def select_stages(*, level: str, only: Optional[list[str]]) -> list[Stage]:
    if only:
        wanted = set(only)
        missing = wanted - set(STAGES_BY_NAME)
        if missing:
            raise SystemExit(f"--only names stages that do not exist: {', '.join(sorted(missing))}")
        return [s for s in STAGES if s.name in wanted]
    return stages_for_level(level)


# --------------------------------------------------------------- execution
def _write_log(ctx: Context, stage: Stage, outcome: Outcome) -> Path:
    ctx.log_dir.mkdir(parents=True, exist_ok=True)
    log_path = ctx.log_dir / f"{stage.name}.log"
    parts = []
    if outcome.command:
        parts.append(f"$ {outcome.command}")
    if outcome.cwd:
        parts.append(f"(cwd: {outcome.cwd})")
    parts.append(f"status: {outcome.status}")
    if outcome.note:
        parts.append(f"note: {outcome.note}")
    parts.append("")
    if outcome.stdout:
        parts.append(outcome.stdout)
    if outcome.stderr and outcome.stderr.strip():
        parts.append("--- stderr ---")
        parts.append(outcome.stderr)
    log_path.write_text("\n".join(parts) + "\n", encoding="utf-8")
    return log_path


def run_stage(ctx: Context, stage: Stage) -> dict:
    start = time.monotonic()
    outcome = stage.precheck(ctx) if stage.precheck else None
    if outcome is None:
        outcome = stage.run(ctx)
    duration = round(time.monotonic() - start, 1)
    log_path = _write_log(ctx, stage, outcome)
    record = {"name": stage.name, "level": stage.level, "status": outcome.status, "duration_s": duration,
             "log_path": str(log_path), "note": outcome.note, "tail": []}
    if outcome.status == "failed":
        tail = _tail(outcome.stderr) or _tail(outcome.stdout)
        if not tail and outcome.note:
            tail = [outcome.note]
        record["tail"] = tail
    return record


def run_stages(ctx: Context, stages: list[Stage], *, fail_fast: bool, on_result=None) -> list[dict]:
    records = []
    for stage in stages:
        record = run_stage(ctx, stage)
        records.append(record)
        if on_result:
            on_result(record)
        if fail_fast and record["status"] == "failed":
            break
    return records


# --------------------------------------------------------------- summary
def human_line(record: dict) -> str:
    line = f"{record['name']:<18} {record['status']:<8} {record['duration_s']:>6.1f}s"
    if record["status"] == "skipped" and record["note"]:
        line += f"  ({record['note']})"
    return line


def print_record(record: dict) -> None:
    print(human_line(record))
    if record["status"] == "failed":
        for l in record["tail"]:
            print(f"    {l}")
        print(f"    full log: {record['log_path']}")


def counts_line(records: list[dict]) -> str:
    counts: dict[str, int] = {}
    for record in records:
        counts[record["status"]] = counts.get(record["status"], 0) + 1
    return ", ".join(f"{count} {status}" for status, count in sorted(counts.items())) or "0 stages ran"


def overall_ok(records: list[dict]) -> bool:
    """A run is ok when nothing failed; "skipped" (tool genuinely absent, or
    a stage never reached under --fail-fast) never counts against it."""
    return not any(r["status"] == "failed" for r in records)


# --------------------------------------------------------------------- CLI
def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="run_checks", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--level", choices=LEVELS, default="unit", help="how much to run (default: unit)")
    parser.add_argument("--only", help="comma-separated stage names to run, ignoring --level")
    parser.add_argument("--list", action="store_true", help="print every declared stage and exit")
    parser.add_argument("--fail-fast", action="store_true", help="stop at the first failed stage (default: run all, report all failures)")
    parser.add_argument("--json", action="store_true", help="one JSON object on stdout, nothing else")
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR,
                        help=f"where each stage's full stdout/stderr goes (default {DEFAULT_LOG_DIR.relative_to(ROOT)}/)")
    parser.add_argument("--studio-repo", default=os.environ.get("SYNOS_STUDIO_REPO"),
                        help="the separate front end's checkout, if you have one (default: $SYNOS_STUDIO_REPO, else not run)")
    args = parser.parse_args(argv)

    if args.list:
        for stage in STAGES:
            level = stage.level or "only"
            print(f"{stage.name:<18} {level:<6} {stage.description}")
        return EXIT_OK

    stages = select_stages(level=args.level, only=args.only.split(",") if args.only else None)

    studio_repo = Path(args.studio_repo).expanduser().resolve() if args.studio_repo else None
    try:
        args.log_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"error: cannot create log directory {args.log_dir}: {exc}", file=sys.stderr)
        return EXIT_HOST

    ctx = Context(root=ROOT, studio_repo=studio_repo, log_dir=args.log_dir)

    if not args.json:
        records = run_stages(ctx, stages, fail_fast=args.fail_fast, on_result=print_record)
        print()
        print(counts_line(records))
    else:
        records = run_stages(ctx, stages, fail_fast=args.fail_fast)
        payload = {"ok": overall_ok(records), "level": args.level, "stages": records}
        print(json.dumps(payload, indent=2, sort_keys=True))

    return EXIT_OK if overall_ok(records) else EXIT_FAILURES


if __name__ == "__main__":
    sys.exit(main())
