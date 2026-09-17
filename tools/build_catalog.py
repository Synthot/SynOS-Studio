#!/usr/bin/env python3
"""Generate the application catalog from the base archives' AppStream metadata.

    python3 tools/build_catalog.py [--suites ubuntu:resolute debian:trixie ...] [--output profiles/catalog.apps.yml]

Every base archive publishes AppStream (DEP-11) metadata for each suite and
component: the desktop applications the distribution offers, with the name,
summary and categories their developers declared. That is what the app stores
show, and it is what the Studio application screen should offer instead of a
hand-typed list. This tool downloads those files (cached under .build/catalog),
keeps the desktop applications, maps their freedesktop categories to a short
list a person can browse, records on which base and suite each one exists,
and writes profiles/catalog.apps.yml. The file is committed, so the export and
the tests never need the network; regenerate it when a suite changes.

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
SPEC = importlib.util.spec_from_file_location("render_manifest", ROOT / "tools" / "render_manifest.py")
render_manifest = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(render_manifest)

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


def collect(suites: list[tuple[str, str]], components: dict[str, tuple[str, ...]] | None = None, arch: str = "amd64") -> dict:
    components = components or DEFAULT_COMPONENTS
    entries: dict[str, dict] = {}
    for base_id, suite in suites:
        env = render_manifest.load_env(ROOT / "bases" / base_id / "base.env")
        mirror = env["APT_MIRROR"].rstrip("/")
        for component in components.get(base_id, ("main",)):
            url = f"{mirror}/dists/{suite}/{component}/dep11/Components-{arch}.yml.gz"
            print(f"reading {url}", file=sys.stderr)
            for app in read_components(_fetch(url)):
                entry = entries.setdefault(app["package"], {"name": app["name"], "summary": app["summary"], "category": app["category"],
                                                            "id": app["id"], "available": {}})
                entry["available"].setdefault(base_id, [])
                if suite not in entry["available"][base_id]:
                    entry["available"][base_id].append(suite)
                if not entry["summary"] and app["summary"]:
                    entry["summary"] = app["summary"]
    packages = [{"package": pkg, "title": e["name"], "summary": e["summary"], "category": e["category"], "available": e["available"],
                 "id": e["id"]} for pkg, e in sorted(entries.items())]
    return {"generated": True, "suites": [f"{b}:{s}" for b, s in suites], "packages": packages}


def default_suites() -> list[tuple[str, str]]:
    """Every suite listed as supported by a base, with the base's default first."""
    result = []
    for base_dir in sorted(p for p in (ROOT / "bases").iterdir() if p.is_dir() and not p.name.startswith("_")):
        env = render_manifest.load_env(base_dir / "base.env")
        suites = env.get("SUPPORTED_SUITES", env.get("DEFAULT_SUITE", "")).split()
        for suite in suites:
            result.append((base_dir.name, suite))
    return result


def main(argv: list[str] | None = None) -> int:
    import yaml
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--suites", nargs="*", help="base:suite pairs (default: every supported suite of every base)")
    parser.add_argument("--output", type=Path, default=ROOT / "profiles" / "catalog.apps.yml")
    args = parser.parse_args(argv)
    suites = [tuple(s.split(":", 1)) for s in args.suites] if args.suites else default_suites()
    data = collect(suites)  # type: ignore[arg-type]
    header = ("# GENERATED by tools/build_catalog.py from the base archives' AppStream metadata;\n"
              "# do not edit. Regenerate when a suite changes. Hand-written entries live in catalog.yml.\n")
    args.output.write_text(header + yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=200), encoding="utf-8")
    by_cat: dict[str, int] = {}
    for p in data["packages"]:
        by_cat[p["category"]] = by_cat.get(p["category"], 0) + 1
    print(f"wrote {args.output.relative_to(ROOT)}: {len(data['packages'])} applications from {len(suites)} suite(s); "
          + ", ".join(f"{c} {n}" for c, n in sorted(by_cat.items(), key=lambda x: -x[1])), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
