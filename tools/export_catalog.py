#!/usr/bin/env python3
"""Export the repository's configuration data as one JSON document.

    python3 tools/export_catalog.py [--output catalog.json]

Bases, suites, profiles (with their resolved inheritance chain), bundles,
regions, package maps, stack roles, brand kits, the software catalog
(profiles/catalog.yml), the JSON schemas, the defaults of manifest.yml, the
engine version (VERSION) and the bundle format this engine accepts.

This is the contract between the build engine (this repository) and any
front end that generates configuration bundles for it, such as the web
configurator, which lives in its own repository and embeds this document at
build time. Nothing here depends on that front end.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("render_manifest", ROOT / "tools" / "render_manifest.py")
render_manifest = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(render_manifest)

BUNDLE_FORMAT = 1
LAUNCHERS = {"build.sh": "bundle_launcher.sh", "build.ps1": "bundle_launcher.ps1", "build.cmd": "bundle_launcher.cmd",
             "build.command": "bundle_launcher.command"}


def engine_version() -> str:
    return (ROOT / "VERSION").read_text(encoding="utf-8").strip()


def merged_packages(catalog: dict) -> list[dict]:
    """The application list Studio shows: the curated entries of catalog.yml first
    (recommended, any package), then the applications generated from the base
    archives' AppStream metadata (tools/build_catalog.py, catalog.apps.yml) with
    the bases and suites each one exists on."""
    curated = []
    for entry in catalog.get("packages", []):
        curated.append({"name": entry["name"], "title": entry.get("title", entry["name"]), "summary": entry.get("summary", ""),
                        "category": entry.get("category", "Other"), "available": entry.get("available", {}), "recommended": True})
    seen = {c["name"] for c in curated}
    generated_path = ROOT / "profiles" / "catalog.apps.yml"
    generated = render_manifest.load_yaml(generated_path).get("packages", []) if generated_path.is_file() else []
    for entry in generated:
        if entry["package"] in seen:
            for c in curated:                       # a curated entry inherits the real availability
                if c["name"] == entry["package"] and not c["available"]:
                    c["available"] = entry["available"]
            continue
        curated.append({"name": entry["package"], "title": entry["title"], "summary": entry["summary"], "category": entry["category"],
                        "available": entry["available"], "recommended": False})
    return curated


def launchers() -> dict[str, str]:
    """The scripts every bundle ships so it builds without a checkout: the
    engine runs inside the published builder image (docs/BUNDLE.md). Read as
    bytes and decoded, not read_text: build.cmd is CRLF, and text mode would
    translate it to LF, making a downloaded bundle.cmd look stale forever."""
    return {name: (ROOT / "tools" / source).read_bytes().decode("utf-8") for name, source in LAUNCHERS.items()}


def bundle_catalog() -> list[dict]:
    """The bundle catalog (bundle-catalog/, docs/BUNDLE.md): ready-made, ordinary
    bundles a front end can offer as starting points, alongside "start from
    scratch". Each entry's files are read as bytes and decoded, not read_text,
    for the same reason as launchers() above, and must come back byte-identical
    to what bundle-catalog/<folder> holds so a front end can write them out
    unchanged. summary is one sentence for the catalogue card; first_boot is
    the steps to show after a bundle is chosen, [] when there is none."""
    load_yaml = render_manifest.load_yaml
    index_path = ROOT / "bundle-catalog" / "index.yml"
    if not index_path.is_file():
        return []
    entries = load_yaml(index_path).get("bundle_catalog", [])
    catalog = []
    for entry in entries:
        folder = ROOT / "bundle-catalog" / entry["folder"]
        bundle = json.loads((folder / "bundle.json").read_text(encoding="utf-8"))
        manifest = load_yaml(folder / bundle["manifest"])
        files = {
            path.relative_to(folder).as_posix(): path.read_bytes().decode("utf-8")
            for path in sorted(folder.rglob("*")) if path.is_file()
        }
        item = {
            "id": entry["id"],
            "name": entry["name"],
            "summary": entry["summary"],
            "first_boot": list(entry.get("first_boot", [])),
            "tags": list(entry.get("tags", [])),
            "kind": manifest["profile"],
            "files": files,
            "services": list(entry.get("services", [])),
            "ports": list(entry.get("ports", [])),
            "verified": bool(entry.get("verified", False)),
        }
        if entry.get("contributed_by"):
            item["contributed_by"] = entry["contributed_by"]
        catalog.append(item)
    return catalog


def collect() -> dict:
    load_yaml = render_manifest.load_yaml
    catalog = load_yaml(ROOT / "profiles" / "catalog.yml")

    bases = []
    for base_dir in sorted(p for p in (ROOT / "bases").iterdir() if p.is_dir() and not p.name.startswith("_")):
        env = render_manifest.load_env(base_dir / "base.env")
        pkg_map = render_manifest.load_package_map(base_dir / "packages.map")
        bases.append({
            "id": env["BASE_ID"],
            "description": env.get("DESCRIPTION", ""),
            "suites": env.get("SUPPORTED_SUITES", env.get("DEFAULT_SUITE", "")).split(),
            "default_suite": env.get("DEFAULT_SUITE", ""),
            "mirror": env.get("APT_MIRROR", ""),
            "components": env.get("COMPONENTS", ""),
            "snapshot_mirror": env.get("SNAPSHOT_MIRROR", ""),
            "package_map": {k: " ".join(v) for k, v in pkg_map.items()},
        })

    # A profile derived from a brand kit (<brand>-<profile>, what the Studio and
    # `synos bundle apply` write) is a company's own profile, not a machine kind.
    brand_ids = sorted((p.parent.name for p in (ROOT / "branding").glob("*/brand.yml")), key=len, reverse=True)
    profiles = []
    for path in sorted((ROOT / "profiles").glob("*.yml")):
        if path.name == "bundles.yml":
            continue
        data = load_yaml(path)
        if "id" not in data:
            continue
        resolved, chain = render_manifest.resolve_profile(data["id"])
        profiles.append({
            "id": data["id"],
            "extends": data.get("extends"),
            "description": data.get("description", ""),
            "example": data["id"].startswith("example-"),
            "derived": any(data["id"].startswith(brand + "-") for brand in brand_ids) or data["id"].startswith("example-"),
            "chain": chain,
            "resolved": resolved,
        })

    bundles = load_yaml(ROOT / "profiles" / "bundles.yml")
    bundle_list = [{"id": name, "items": items, "description": catalog.get("bundles", {}).get(name, "")} for name, items in bundles.items()]

    regions = []
    for path in sorted((ROOT / "regions").glob("*.yml")):
        data = load_yaml(path)
        regions.append({
            "id": data["id"], "name": data["name"],
            "locales": [loc.replace(".UTF-8", "") for loc in data.get("locales", [])],
            "keyboard": str(data.get("keyboard", "")), "timezone": data.get("timezone", ""),
            "compliance": data.get("compliance", "none"), "eid": data.get("eid", []) or [],
            "paper": data.get("paper", "a4"), "group": data.get("group", "other"),
        })

    stack = load_yaml(ROOT / "packages" / "stack.yml")
    roles = []
    for group_id, group in (stack.get("groups") or {}).items():
        for entry in group.get("packages") or []:
            package = str(entry["package"])
            roles.append({"role": entry["role"], "group": group_id, "package": package,
                          "origin": "synos" if (ROOT / "packages" / package / "control").is_file() else "archive",
                          "available": True})

    brands = []
    for path in sorted((ROOT / "branding").glob("*/brand.yml")):
        data = load_yaml(path)
        brands.append({"id": data["id"], "display_name": data.get("display_name", data["id"]), "example": data["id"].startswith("example-")})

    schemas = {
        name: json.loads((ROOT / "schema" / f"{name}.schema.json").read_text(encoding="utf-8"))
        for name in ("manifest", "profile", "bundle")
    }
    default_manifest = load_yaml(ROOT / "manifest.yml")
    return {
        "generated_from": str(ROOT.name),
        "engine": engine_version(),
        "bundle_format": BUNDLE_FORMAT,
        "launchers": launchers(),
        "bundle_catalog": bundle_catalog(),
        "bases": bases,
        "profiles": profiles,
        "bundles": bundle_list,
        "regions": regions,
        "roles": roles,
        "brands": brands,
        "catalog": {"compliance": catalog.get("compliance", []), "packages": merged_packages(catalog),
                    "services": catalog.get("services", []), "gpus": catalog.get("gpus", [])},
        "schemas": schemas,
        "defaults": {
            "base": default_manifest.get("base"), "suite": default_manifest.get("suite"), "arch": default_manifest.get("arch"),
            "profile": default_manifest.get("profile"), "regions": default_manifest.get("regions", []),
            "snapshot": str(default_manifest.get("mirrors", {}).get("snapshot", "")),
            "takeover": (default_manifest.get("packages") or {}).get("takeover", []),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--output", type=Path, help="write the JSON here instead of stdout")
    args = parser.parse_args(argv)
    data = collect()
    text = json.dumps(data, ensure_ascii=False, indent=None if args.output else 2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
        print(f"wrote {args.output} ({len(data['profiles'])} profiles, {len(data['regions'])} regions, "
              f"{len(data['bases'])} bases, engine {data['engine']}, bundle format {data['bundle_format']})", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
