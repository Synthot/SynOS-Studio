#!/usr/bin/env python3
"""Read and write the phase-timing ledger a build writes as it runs.

Every phase of a build (the package-building step, the chroot bootstrap, the
mods, the Ansible run, the squashfs, the ISO assembly) appends one JSON line
to a ledger file as that phase ends -- never held in memory and flushed at
the end, so a build that dies partway still leaves every completed phase's
cost on disk. build.sh and mods/install_all_mods.sh write lines themselves
(pure shell, no python inside the chroot); tools/build_packages.py calls the
functions here directly since it is already python. This script's own job is
the summary: turn the ledger into the evidence file next to the ISO.

    python3 tools/build_timings.py reset --file .build/timings.jsonl
    python3 tools/build_timings.py record --file .build/timings.jsonl --phase "Build packages" --seconds 42
    python3 tools/build_timings.py summarize --file .build/timings.jsonl --stem dist/SynOS-1.0.0-amd64

summarize writes <stem>.timings.json (one entry per phase, longest first,
each with its share of the total -- the same shape as <stem>.resolved.json
and <stem>.sbom.cdx.json beside it) and prints the same ranking as plain
lines, so it reads like the rest of dist/build.log.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def reset_timings(path: Path) -> None:
    """Start a fresh ledger. Called once, at the start of the package-building
    step -- the first phase of every real build -- so stale entries from an
    earlier run never mix with this one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")


def append_phase(path: Path, phase: str, seconds: float) -> None:
    """Append one phase's timing as its own line, immediately. Never buffers
    more than one entry, so a crash right after this call still loses nothing
    but the phase that has not finished yet."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"phase": phase, "seconds": round(seconds)}) + "\n")


def read_phases(path: Path) -> list[tuple[str, int]]:
    """The ledger as (phase, seconds) pairs, in the order they were written.
    Malformed or partial lines (a crash mid-write) are skipped, not fatal --
    the whole point of this file is to survive a build that did not finish."""
    if not path.is_file():
        return []
    phases = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and "phase" in entry and "seconds" in entry:
            try:
                phases.append((str(entry["phase"]), round(float(entry["seconds"]))))
            except (TypeError, ValueError):
                continue
    return phases


def ranked_summary(phases: list[tuple[str, int]]) -> tuple[list[dict], int]:
    """Longest first, each with its share of the total (the sum of the
    phases actually recorded -- gaps between instrumented phases are not
    counted, since there is nothing to compare them against yet)."""
    total = sum(seconds for _, seconds in phases)
    ranked = sorted(phases, key=lambda item: item[1], reverse=True)
    share_of = total or 1
    return [
        {"phase": name, "seconds": seconds, "share": round(seconds / share_of, 4)}
        for name, seconds in ranked
    ], total


def format_summary_lines(ranked: list[dict], total: int) -> list[str]:
    lines = [f"phase timing summary ({len(ranked)} phase(s), {total}s total):"]
    width = max((len(entry["phase"]) for entry in ranked), default=0)
    for index, entry in enumerate(ranked, start=1):
        lines.append(
            f"  {index}. {entry['phase']:<{width}}  {entry['seconds']:>6d}s  {entry['share'] * 100:5.1f}%"
        )
    return lines


def cmd_reset(args: argparse.Namespace) -> int:
    reset_timings(Path(args.file))
    return 0


def cmd_record(args: argparse.Namespace) -> int:
    append_phase(Path(args.file), args.phase, args.seconds)
    return 0


def cmd_summarize(args: argparse.Namespace) -> int:
    phases = read_phases(Path(args.file))
    ranked, total = ranked_summary(phases)
    stem = Path(args.stem)
    stem.parent.mkdir(parents=True, exist_ok=True)
    out = stem.with_name(stem.name + ".timings.json")
    out.write_text(json.dumps({"total_seconds": total, "phases": ranked}, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out.name} ({len(ranked)} phase(s), {total}s total)")
    for line in format_summary_lines(ranked, total)[1:]:
        print(line)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    reset_p = sub.add_parser("reset", help="start a fresh ledger")
    reset_p.add_argument("--file", required=True)

    record_p = sub.add_parser("record", help="append one phase's timing")
    record_p.add_argument("--file", required=True)
    record_p.add_argument("--phase", required=True)
    record_p.add_argument("--seconds", required=True, type=float)

    summarize_p = sub.add_parser("summarize", help="write the evidence file and print the ranking")
    summarize_p.add_argument("--file", required=True)
    summarize_p.add_argument("--stem", required=True, help="output path without extension")

    args = parser.parse_args(argv)
    if args.command == "reset":
        return cmd_reset(args)
    if args.command == "record":
        return cmd_record(args)
    return cmd_summarize(args)


if __name__ == "__main__":
    sys.exit(main())
