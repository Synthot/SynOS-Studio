#!/usr/bin/env python3
"""catalog_apps_audit — does every name in the searchable catalogue actually
exist in the archive the Studio page would build it from?

    python3 tools/catalog_apps_audit.py
    python3 tools/catalog_apps_audit.py --json

profiles/catalog.apps.yml (tools/build_catalog.py) lets a person add any of
its ~2200 entries by name to a build, on whichever base the entry's own
`available` mapping claims (`{ubuntu: [resolute, noble], debian: [trixie]}`,
say). That mapping was never checked against the thing that actually
matters: whether `apt-get install <entry["package"]>` resolves on that
base+suite. It was computed once, at generation time, from a *different*
archive index entirely — the AppStream (DEP-11) `Components-<arch>.yml.gz`
files, over `main`+`universe` (ubuntu) or `main` (debian) only
(tools/build_catalog.py's own DEFAULT_COMPONENTS) — which records "this
name had a desktop-application AppStream component published here", not
"this apt package name exists here". The two can and do disagree: a
metadata error, a package renamed or split upstream since the metadata was
scraped, or a component AppStream never looked at (restricted, multiverse,
contrib, non-free, non-free-firmware) that the engine itself enables
(bases/*/base.env's own COMPONENTS) all pass through untouched. The first
anyone finds out is `apt-get install` failing on a real build, forty
minutes in.

This audit closes exactly that gap, read-only: for every catalogue entry,
for every (base, suite) its own `available` mapping claims, it fetches that
base+suite's *real* Packages.gz — every component the engine enables, not
just the ones AppStream metadata was scraped from — and checks the entry's
package name against it. It never builds anything, never touches the
catalogue file, and reuses tools/catalog_conformance.py's own
fetch_archive_package_names()/_component_urls() and
tools/render_manifest.py's own load_env()/live_capable_suites() to read the
archive exactly the way that tool already does, rather than writing a
second downloader.

One archive index is fetched per (base, suite, component) actually named by
some entry's `available` mapping, cached for the run — 2200-odd entries are
then a set-membership check each, not 2200 requests. A component that
cannot be fetched at all (offline, or a mirror that will not answer) marks
every entry claimed on that (base, suite) "could not check", never
"missing" — a network failure must never read as a broken catalogue. An
entry claimed on one base's archive but missing there, while present and
claimed on the other, is reported per base (`missing_from` / `present_on`)
without being called an error on its own: the page offers applications per
base, and an app being one-base-only is normal (bases/ubuntu/packages.map's
own `browser-headless` marks the same asymmetry the other way, on the
Debian side).

Exit codes:
    0  every claimed (entry, base, suite) resolved against the real archive
    1  at least one entry claims availability the real archive does not
       have (this is the gate: non-zero means the catalogue would let
       somebody start a build that fails)
    2  nothing was found missing among what could be checked, but at least
       one (base, suite) index could not be fetched at all — the run is
       incomplete, not clean; never conflated with exit 1

Never invents an archive layout of its own: base+suite+component -> URL
comes from tools/catalog_conformance.py's own _component_urls(), which
reads it straight out of bases/<base>/base.env. Never hits the network in
its own test suite (tests/unit/test_catalog_apps_audit.py); the fetcher is
a plain function argument a test replaces with an in-memory fake.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime
import gzip
import importlib.util
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DEFAULT_ARCH = "amd64"


def _load_module(name: str, relative: str):
    """Same pattern tests/unit/test_catalog_conformance.py already uses to load
    a sibling tool as a module: register it in sys.modules before exec, so a
    dataclass defined inside (catalog_conformance.Config) can resolve its own
    module for annotations."""
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


catalog_conformance = _load_module("catalog_conformance_for_apps_audit", "tools/catalog_conformance.py")
render_manifest = _load_module("render_manifest_for_apps_audit", "tools/render_manifest.py")


class AuditError(Exception):
    """The catalogue or this checkout's own base adapters could not be read
    at all — nothing to check per-entry."""


# ------------------------------------------------------------- archives
@dataclasses.dataclass(frozen=True)
class ArchiveResult:
    """One (base, suite)'s real package names, or why it could not be read.
    `ok=False` means at least one component's index could not be fetched or
    decompressed — the whole (base, suite) is then untrustworthy for a
    "missing" verdict (a package that lives in the one component that
    failed would otherwise look absent), so every entry claimed there is
    reported "could not check", never "missing"."""
    ok: bool
    names: frozenset
    error: str | None
    urls: tuple


def fetch_index(base: str, suite: str, arch: str, base_env: dict, *, fetcher, cache: dict) -> ArchiveResult:
    """The real Packages.gz names for (base, suite), across every component
    base.env enables (catalog_conformance._component_urls) — fetched once
    per (base, suite) no matter how many catalogue entries ask for it,
    because `cache` is shared across the whole run. Reuses
    catalog_conformance.fetch_archive_package_names() to do the actual
    fetch-and-parse; the only thing added here is noticing *which* url (if
    any) failed, which that function's own per-url `except: continue`
    deliberately swallows (a missing component is not fatal to the check it
    was written for) — this audit needs to tell "found nothing" apart from
    "could not look"."""
    key = (base, suite)
    if key in cache:
        return cache[key]
    urls = catalog_conformance._component_urls(base_env, suite, arch)
    failures: list[str] = []

    def recording_fetcher(url: str) -> bytes:
        try:
            raw = fetcher(url)
            gzip.decompress(raw)  # validate now; fetch_archive_package_names decompresses its own copy again
        except Exception as exc:  # noqa: BLE001 - recorded, then re-raised for the caller's own handling
            failures.append(f"{url}: {type(exc).__name__}: {exc}")
            raise
        return raw

    names = catalog_conformance.fetch_archive_package_names(urls, fetcher=recording_fetcher)
    result = ArchiveResult(ok=not failures, names=frozenset(names),
                           error="; ".join(failures) if failures else None, urls=tuple(urls))
    cache[key] = result
    return result


def engine_matrix(root: Path = ROOT) -> tuple[dict[str, list[str]], dict[str, dict]]:
    """Every base this checkout has an adapter for, and the suites each one
    actually supports for a real build — render_manifest.live_capable_suites(),
    the same "suites base.env's SUPPORTED_SUITES accepts minus whatever
    live.map marks unavailable" export_catalog.py already uses to decide
    what Studio offers, so this audit checks exactly the matrix a person can
    actually reach, never a suite (jammy) the engine refuses before a chroot
    ever runs regardless of what any catalogue says."""
    # catalog_conformance._known_bases() also picks up bases/_template (it has
    # its own base.env, as a template to copy); export_catalog.py's own
    # collect() excludes anything starting with "_" for the same reason, and
    # this does too — a catalogue entry's `available` mapping is keyed by
    # real base ids only, never "_template".
    bases = sorted(b for b in catalog_conformance._known_bases() if not b.startswith("_"))
    if not bases:
        raise AuditError(f"no base adapter found under {root / 'bases'}")
    matrix: dict[str, list[str]] = {}
    envs: dict[str, dict] = {}
    for base in bases:
        base_dir = root / "bases" / base
        env = render_manifest.load_env(base_dir / "base.env")
        envs[base] = env
        matrix[base] = render_manifest.live_capable_suites(base_dir, env)
    return matrix, envs


# --------------------------------------------------------------- auditing
def load_catalog_entries(path: Path) -> list[dict]:
    if not path.is_file():
        raise AuditError(f"{path}: not found")
    data = render_manifest.load_yaml(path)
    entries = data.get("packages")
    if entries is None:
        raise AuditError(f"{path}: no top-level 'packages' key ({path.name}'s own generated shape)")
    return entries


def build_report(entries: list[dict], matrix: dict[str, list[str]], base_envs: dict[str, dict], *,
                 fetcher, arch: str = DEFAULT_ARCH, cache: dict | None = None) -> dict:
    """The audit itself: for every entry, for every (base, suite) its own
    `available` mapping claims, check the real archive. Pure with respect to
    the network — `fetcher` is the only thing that ever leaves this process,
    and it is called at most once per distinct archive index URL, via
    fetch_index()'s shared `cache`."""
    cache = {} if cache is None else cache
    checks_total = 0
    missing_by_id: dict[str, dict] = {}
    could_not_check: dict[tuple[str, str], dict] = {}
    stale_claims: list[dict] = []

    for entry in entries:
        pkg = entry["package"]
        eid = entry["id"]
        title = entry.get("title", pkg)
        available = entry.get("available") or {}
        missing_here: list[tuple[str, str]] = []
        present_here: list[tuple[str, str]] = []

        for base, suites in available.items():
            if base not in matrix:
                for suite in suites:
                    stale_claims.append({"id": eid, "package": pkg, "base": base, "suite": suite,
                                         "reason": f"base {base!r} is not one this checkout has an adapter for"})
                continue
            for suite in suites:
                if suite not in matrix[base]:
                    stale_claims.append({"id": eid, "package": pkg, "base": base, "suite": suite,
                                         "reason": f"{base} does not currently support {suite} as a "
                                                   "live-capable suite (base.env/live.map)"})
                    continue
                result = fetch_index(base, suite, arch, base_envs[base], fetcher=fetcher, cache=cache)
                if not result.ok:
                    bucket = could_not_check.setdefault((base, suite), {"error": result.error, "entries": set()})
                    bucket["entries"].add(eid)
                    continue
                checks_total += 1
                if pkg in result.names:
                    present_here.append((base, suite))
                else:
                    missing_here.append((base, suite))

        if missing_here:
            missing_by_id[eid] = {
                "id": eid, "package": pkg, "title": title,
                "missing_from": [{"base": b, "suite": s} for b, s in missing_here],
                "present_on": [{"base": b, "suite": s} for b, s in present_here],
                "everywhere": not present_here,
            }

    return {
        "entries_total": len(entries),
        "checks_total": checks_total,
        "missing": sorted(missing_by_id.values(), key=lambda r: r["id"]),
        "could_not_check": [
            {"base": b, "suite": s, "error": v["error"], "entries_affected": len(v["entries"])}
            for (b, s), v in sorted(could_not_check.items())
        ],
        "stale_suite_claims": sorted(stale_claims, key=lambda r: (r["id"], r["base"], r["suite"])),
    }


def exit_code(report: dict) -> int:
    if report["missing"]:
        return 1
    if report["could_not_check"]:
        return 2
    return 0


# ------------------------------------------------------------ reporting
def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def summary_text(report: dict) -> str:
    lines = [f"catalog_apps_audit: {report['entries_total']} catalogue entr{'y' if report['entries_total'] == 1 else 'ies'}, "
             f"{report['checks_total']} (entry, base, suite) claim(s) checked against the real archive"]

    missing = report["missing"]
    if missing:
        lines.append(f"MISSING: {len(missing)} entr{'y' if len(missing) == 1 else 'ies'} claim availability "
                     "the real archive does not have:")
        for record in missing:
            missing_from = ", ".join(f"{c['base']}:{c['suite']}" for c in record["missing_from"])
            if record["everywhere"]:
                where = f"missing everywhere it is claimed ({missing_from})"
            else:
                present_on = ", ".join(f"{c['base']}:{c['suite']}" for c in record["present_on"])
                where = f"missing from {missing_from}; present on {present_on}"
            lines.append(f"  {record['id']} (package: {record['package']}): {where}")
    else:
        lines.append("no catalogue entry is missing from an archive it claims to be available on")

    could_not_check = report["could_not_check"]
    if could_not_check:
        affected = sum(c["entries_affected"] for c in could_not_check)
        lines.append(f"COULD NOT CHECK: {len(could_not_check)} archive index/indices unreachable, "
                     f"leaving {affected} claimed entr{'y' if affected == 1 else 'ies'} unverified:")
        for c in could_not_check:
            lines.append(f"  {c['base']}:{c['suite']}: {c['error']} ({c['entries_affected']} entr"
                         f"{'y' if c['entries_affected'] == 1 else 'ies'} affected)")

    stale = report["stale_suite_claims"]
    if stale:
        lines.append(f"note: {len(stale)} claim(s) name a base/suite this checkout no longer supports "
                     "(not checked, not counted as missing or unverified):")
        for s in stale[:20]:
            lines.append(f"  {s['id']} ({s['package']}) claims {s['base']}:{s['suite']}: {s['reason']}")
        if len(stale) > 20:
            lines.append(f"  ... and {len(stale) - 20} more")

    stats = report.get("fetch_stats")
    if stats:
        lines.append(f"fetched {stats['requests']} archive index file(s), {stats['bytes'] / 1_000_000:.1f} MB, "
                     f"in {stats['duration_s']}s")
    return "\n".join(lines)


# ---------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="catalog_apps_audit", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--catalog", type=Path, default=ROOT / "profiles" / "catalog.apps.yml",
                       help="path to catalog.apps.yml (default: profiles/catalog.apps.yml)")
    parser.add_argument("--arch", default=DEFAULT_ARCH, help=f"architecture to check (default: {DEFAULT_ARCH})")
    parser.add_argument("--json", action="store_true", help="machine-readable report instead of the human summary")
    args = parser.parse_args(argv)

    try:
        entries = load_catalog_entries(args.catalog)
        matrix, envs = engine_matrix(ROOT)
    except AuditError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    stats = {"requests": 0, "bytes": 0}

    def counting_fetcher(url: str) -> bytes:
        raw = catalog_conformance._http_get(url)
        stats["requests"] += 1
        stats["bytes"] += len(raw)
        return raw

    start = time.monotonic()
    report = build_report(entries, matrix, envs, fetcher=counting_fetcher, arch=args.arch)
    report["fetch_stats"] = {"requests": stats["requests"], "bytes": stats["bytes"],
                             "duration_s": round(time.monotonic() - start, 1)}
    report["catalog_path"] = str(args.catalog)
    report["engine_matrix"] = matrix
    report["generated"] = _now()

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(summary_text(report))
    return exit_code(report)


if __name__ == "__main__":
    sys.exit(main())
