#!/usr/bin/env python3
"""Generate the application catalog from the base archives' AppStream metadata.

    python3 tools/build_catalog.py [--suites ubuntu:resolute debian:trixie ...] [--output profiles/catalog.apps.yml]

Every base archive publishes AppStream (DEP-11) metadata for each suite and
component: the desktop applications the distribution offers, with the name,
summary and categories their developers declared. That is what the app stores
show, and it is what the Studio application screen should offer instead of a
hand-typed list. This tool downloads those files (cached under .build/catalog),
keeps the desktop applications, maps their freedesktop categories to a short
list a person can browse, and writes profiles/catalog.apps.yml. The file is
committed, so the export and the tests never need the network; regenerate it
when a suite changes.

AppStream metadata records "this suite published a desktop-application
component for this name" — nothing about whether the *binary* package it
names can still be installed. Metadata outlives a dropped binary all the
time (an orphaned package, a build failure on one suite that a later one
fixes, a name AppStream never updates once removed): the two drift apart on
their own, silently, between one regeneration and the next. So a candidate
is only ever recorded as available on a (base, suite) once its package name
is also checked against that suite's real Packages.gz, across every
component bases/<base>/base.env actually enables for a build (not merely
the narrower `main`/`universe` AppStream was scraped from) — the exact
index tools/catalog_apps_audit.py itself reads, via its own fetch_index()
(which itself reuses tools/catalog_conformance.py's own
fetch_archive_package_names()/_component_urls()), reused here rather than
writing a third copy of any of it. A (base, suite) whose real index cannot
be fetched at all contributes no candidate that suite would otherwise have
claimed, and is named plainly in the run's own warnings and exit code —
never silently trusted on AppStream's word alone.

Only ever emits a suite render_manifest.live_capable_suites() says the
engine can actually build a Live image for (bases/*/live.map) — the same
function tools/export_catalog.py already uses to decide what the Studio
page offers, so what this catalogue claims and what the engine will accept
can never disagree; an explicit --suites naming one it refuses is an error,
not a quietly-honored request.

profiles/catalog.yml stays hand-written: bundle descriptions, compliance
profiles and the curated packages shown first (servers, command-line tools
and other packages that have no desktop entry).
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / ".build" / "catalog"


def _load_module(name: str, relative: str):
    """tests/unit/test_catalog_conformance.py's own pattern for loading a
    sibling tool as a module: register it in sys.modules *before* exec, so a
    dataclass defined inside it (catalog_conformance.Config) can resolve its
    own module for annotations."""
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


render_manifest = _load_module("render_manifest_for_build_catalog", "tools/render_manifest.py")
catalog_apps_audit = _load_module("catalog_apps_audit_for_build_catalog", "tools/catalog_apps_audit.py")

# freedesktop main categories -> what the page shows; first match wins
CATEGORY_MAP = [
    ("Security", "Security"),
    ("Office", "Office"),
    ("Graphics", "Graphics"),
    ("AudioVideo", "Audio & video"), ("Audio", "Audio & video"), ("Video", "Audio & video"),
    ("Development", "Development"),
    ("Network", "Internet"),
    ("Game", "Games"),
    ("Education", "Education"),
    ("Science", "Science"),
    ("System", "System"),
    ("Settings", "System"),
    ("Utility", "Utilities"),
]
DEFAULT_COMPONENTS = {"ubuntu": ("main", "universe"), "debian": ("main",)}
SKIP_PACKAGE_PREFIXES = ("lib", "fonts-", "gnome-shell-extension-", "kde-config-", "plasma-", "kwin-")
SKIP_IDS = ("org.kde.plasma", "org.gnome.Shell.Extensions")


def _fetch(url: str) -> bytes:
    import urllib.request
    CACHE.mkdir(parents=True, exist_ok=True)
    cached = CACHE / (hashlib.sha256(url.encode()).hexdigest()[:16] + ".yml.gz")
    if cached.is_file():
        return cached.read_bytes()
    request = urllib.request.Request(url, headers={"User-Agent": "SynOS catalog builder"})
    with urllib.request.urlopen(request, timeout=180) as response:
        data = response.read()
    cached.write_bytes(data)
    return data


def _text(value) -> str:
    """AppStream localised fields: {C: ..., fr: ...}; keep the untranslated one."""
    if isinstance(value, dict):
        value = value.get("C") or value.get("en") or next(iter(value.values()), "")
    # a few upstream summaries carry U+FFFD where a character was lost upstream
    return " ".join(str(value or "").replace("\ufffd", " ").split())


def category_for(categories: list[str] | None) -> str:
    cats = set(categories or [])
    for key, label in CATEGORY_MAP:
        if key in cats:
            return label
    return "Other"


def read_components(data: bytes) -> list[dict]:
    """Desktop applications from one Components-*.yml.gz."""
    import yaml
    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    apps = []
    for doc in yaml.load_all(gzip.decompress(data), Loader=loader):
        if not isinstance(doc, dict) or doc.get("Type") != "desktop-application":
            continue
        package = doc.get("Package")
        if not package or package.startswith(SKIP_PACKAGE_PREFIXES) or str(doc.get("ID", "")).startswith(SKIP_IDS):
            continue
        name, summary = _text(doc.get("Name")), _text(doc.get("Summary"))
        if not name:
            continue
        apps.append({"package": package, "name": name, "summary": summary[:140], "category": category_for(doc.get("Categories")),
                     "id": doc.get("ID", "")})
    return apps


def collect(suites: list[tuple[str, str]], components: dict[str, tuple[str, ...]] | None = None, arch: str = "amd64",
           *, fetcher=None) -> tuple[dict, list[str]]:
    """Returns (data, warnings). `fetcher` (url -> bytes) feeds both the
    AppStream scrape (as before) and, per (base, suite), the real binary
    index check via tools/catalog_apps_audit.fetch_index() — the same
    fetcher, so a test's in-memory fake stands in for both without a second
    plumbing path; defaults to `_fetch` (this file's own on-disk-cached
    network fetch), same as always.

    A candidate an AppStream component names is only ever recorded as
    available on (base_id, suite) once fetch_index() says the real archive
    (every component base.env enables) actually has that package name — not
    merely the narrower main/universe `components` AppStream itself was
    scraped from. A (base, suite) whose real index cannot be fetched at all
    (fetch_index().ok is False) claims nothing for any candidate that suite
    would otherwise have contributed, and is named in `warnings` rather than
    silently trusted on AppStream's word alone."""
    components = components or DEFAULT_COMPONENTS
    fetcher = fetcher or _fetch
    entries: dict[str, dict] = {}
    base_envs: dict[str, dict] = {}
    archive_cache: dict = {}
    warnings: list[str] = []

    for base_id, suite in suites:
        env = base_envs.setdefault(base_id, render_manifest.load_env(ROOT / "bases" / base_id / "base.env"))
        mirror = env["APT_MIRROR"].rstrip("/")
        candidates: dict[str, dict] = {}
        for component in components.get(base_id, ("main",)):
            url = f"{mirror}/dists/{suite}/{component}/dep11/Components-{arch}.yml.gz"
            print(f"reading {url}", file=sys.stderr)
            for app in read_components(fetcher(url)):
                candidates.setdefault(app["package"], app)
        if not candidates:
            continue

        archive = catalog_apps_audit.fetch_index(base_id, suite, arch, env, fetcher=fetcher, cache=archive_cache)
        if not archive.ok:
            warning = (f"{base_id}:{suite}: could not verify against its real package index ({archive.error}); "
                       f"claiming none of its {len(candidates)} AppStream candidate(s) for this suite")
            print(f"warning: {warning}", file=sys.stderr)
            warnings.append(warning)
            continue

        for pkg, app in candidates.items():
            if pkg not in archive.names:
                continue  # AppStream had metadata; the real binary package does not actually exist here
            entry = entries.setdefault(pkg, {"name": app["name"], "summary": app["summary"], "category": app["category"],
                                             "id": app["id"], "available": {}})
            entry["available"].setdefault(base_id, [])
            if suite not in entry["available"][base_id]:
                entry["available"][base_id].append(suite)
            if not entry["summary"] and app["summary"]:
                entry["summary"] = app["summary"]

    # Sorted regardless of the order `suites` was actually processed in: the
    # committed file's diff, from one regeneration to the next, should show
    # only what genuinely changed on the archive, never a base or suite
    # reordered because this run's --suites (or the base directory listing)
    # happened to be given in a different order than last time's.
    packages = [{"package": pkg, "title": e["name"], "summary": e["summary"], "category": e["category"],
                 "available": {base: sorted(s) for base, s in sorted(e["available"].items())}, "id": e["id"]}
                for pkg, e in sorted(entries.items())]
    return {"generated": True, "suites": [f"{b}:{s}" for b, s in suites], "packages": packages}, warnings


def default_suites() -> list[tuple[str, str]]:
    """Every suite a base can actually build a Live image for, with the
    base's default first — render_manifest.live_capable_suites(), the same
    "SUPPORTED_SUITES minus whatever live.map marks unavailable" function
    tools/export_catalog.py already uses to decide what Studio offers, so a
    suite (jammy, today) the engine refuses before a chroot ever runs is
    never even considered here, regardless of what SUPPORTED_SUITES itself
    still lists."""
    result = []
    for base_dir in sorted(p for p in (ROOT / "bases").iterdir() if p.is_dir() and not p.name.startswith("_")):
        env = render_manifest.load_env(base_dir / "base.env")
        for suite in render_manifest.live_capable_suites(base_dir, env):
            result.append((base_dir.name, suite))
    return result


def _validate_suites_are_live_capable(suites: list[tuple[str, str]]) -> list[str]:
    """Item 183: whether `suites` came from default_suites() or an explicit
    --suites, every one of them must be render_manifest.live_capable_suites()
    for its own base — what this catalogue claims and what the engine will
    accept can never disagree. Returns the "base:suite" strings that fail,
    empty when every one passes."""
    live_by_base: dict[str, list[str]] = {}
    for base_dir in sorted(p for p in (ROOT / "bases").iterdir() if p.is_dir() and not p.name.startswith("_")):
        env = render_manifest.load_env(base_dir / "base.env")
        live_by_base[base_dir.name] = render_manifest.live_capable_suites(base_dir, env)
    return [f"{b}:{s}" for b, s in suites if s not in live_by_base.get(b, [])]


def main(argv: list[str] | None = None) -> int:
    import yaml
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--suites", nargs="*", help="base:suite pairs (default: every live-capable suite of every base)")
    parser.add_argument("--output", type=Path, default=ROOT / "profiles" / "catalog.apps.yml")
    args = parser.parse_args(argv)
    suites = [tuple(s.split(":", 1)) for s in args.suites] if args.suites else default_suites()

    # Item 183: an explicit --suites naming one the engine refuses outright
    # (bases/*/live.map) is a usage error, not a quietly-honored request —
    # default_suites() already excludes these, so this only ever fires for
    # --suites, but it is checked unconditionally rather than trusting the
    # caller to know which path they took.
    refused = _validate_suites_are_live_capable(suites)
    if refused:
        print(f"error: suite(s) the engine cannot build a Live image for at all (bases/*/live.map): "
              f"{', '.join(refused)}", file=sys.stderr)
        return 2

    data, warnings = collect(suites)  # type: ignore[arg-type]
    header = ("# GENERATED by tools/build_catalog.py from the base archives' AppStream metadata;\n"
              "# do not edit. Regenerate when a suite changes. Hand-written entries live in catalog.yml.\n")
    args.output.write_text(header + yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=200), encoding="utf-8")
    by_cat: dict[str, int] = {}
    for p in data["packages"]:
        by_cat[p["category"]] = by_cat.get(p["category"], 0) + 1
    # Item 184: --output outside this checkout used to crash *after* the
    # file was already written (relative_to() raising ValueError) — the
    # work had happened, only the summary line blew up. The summary must
    # survive any output path, real or relative.
    try:
        display_path = args.output.relative_to(ROOT)
    except ValueError:
        display_path = args.output
    print(f"wrote {display_path}: {len(data['packages'])} applications from {len(suites)} suite(s); "
          + ", ".join(f"{c} {n}" for c, n in sorted(by_cat.items(), key=lambda x: -x[1])), file=sys.stderr)
    if warnings:
        print(f"warning: {len(warnings)} suite(s) could not be verified against their real package index and "
              "contributed nothing (see the warnings above); the catalogue is incomplete, not wrong, for those",
              file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
