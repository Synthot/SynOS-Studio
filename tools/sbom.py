#!/usr/bin/env python3
"""Write the package lock and a CycloneDX SBOM for a built image.

Inputs:  the dpkg manifest build.sh writes (image/LiveOS/filesystem.manifest,
         "package version" per line) and .build/resolved.json.
Outputs: <stem>.packages.lock   name=version, sorted, one per line
         <stem>.sbom.cdx.json   CycloneDX 1.5 with one component per package

    python3 tools/sbom.py --dpkg-manifest image/LiveOS/filesystem.manifest \\
        --resolved .build/resolved.json --stem dist/SynOS-1.0.0-amd64
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import sys
import uuid
from pathlib import Path


def read_dpkg_manifest(path: Path) -> list[tuple[str, str]]:
    packages: list[tuple[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) >= 2:
            packages.append((parts[0], parts[1]))
    if not packages:
        raise SystemExit(f"{path} lists no packages")
    return sorted(set(packages))


def purl(base: str, name: str, version: str, arch: str) -> str:
    pkg = name
    pkg_arch = arch
    if ":" in name:
        pkg, pkg_arch = name.split(":", 1)
    from urllib.parse import quote
    return f"pkg:deb/{base}/{quote(pkg)}@{quote(version, safe='')}?arch={pkg_arch}"


def build_sbom(packages: list[tuple[str, str]], resolved: dict, iso: Path | None) -> dict:
    manifest = resolved["manifest"]
    brand = resolved["brand"]
    base = resolved["base"]["BASE_ID"]
    arch = manifest["arch"]
    now = _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    components = [
        {
            "type": "library",
            "name": name.split(":", 1)[0],
            "version": version,
            "purl": purl(base, name, version, arch),
            "bom-ref": purl(base, name, version, arch),
            "supplier": {"name": f"{base} archive"},
        }
        for name, version in packages
    ]
    main = {
        "type": "operating-system",
        "name": brand["display_name"],
        "version": manifest["version"],
        "bom-ref": f"{brand['id']}-{manifest['version']}-{arch}",
        "supplier": {"name": brand.get("vendor", brand["display_name"])},
        "properties": [
            {"name": "synos:base", "value": f"{base} {manifest['suite']}"},
            {"name": "synos:profile", "value": " > ".join(resolved["profile_chain"])},
            {"name": "synos:regions", "value": " ".join(r["id"] for r in resolved["regions"])},
            {"name": "synos:snapshot", "value": (manifest.get("mirrors") or {}).get("snapshot", "") or "live"},
        ],
    }
    if iso and iso.is_file():
        digest = hashlib.sha256()
        with iso.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        main["hashes"] = [{"alg": "SHA-256", "content": digest.hexdigest()}]
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{uuid.uuid4()}",
        "version": 1,
        "metadata": {
            "timestamp": now,
            "tools": [{"vendor": "SynOS", "name": "tools/sbom.py", "version": "1"}],
            "component": main,
        },
        "components": components,
        "dependencies": [{"ref": main["bom-ref"], "dependsOn": [c["bom-ref"] for c in components]}],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dpkg-manifest", required=True)
    parser.add_argument("--resolved", required=True)
    parser.add_argument("--stem", required=True, help="output path without extension")
    parser.add_argument("--iso", default=None, help="ISO to hash into the SBOM")
    args = parser.parse_args(argv)
    packages = read_dpkg_manifest(Path(args.dpkg_manifest))
    resolved = json.loads(Path(args.resolved).read_text(encoding="utf-8"))
    stem = Path(args.stem)
    stem.parent.mkdir(parents=True, exist_ok=True)
    lock = stem.with_name(stem.name + ".packages.lock")
    lock.write_text("".join(f"{name}={version}\n" for name, version in packages), encoding="utf-8")
    sbom = stem.with_name(stem.name + ".sbom.cdx.json")
    sbom.write_text(json.dumps(build_sbom(packages, resolved, Path(args.iso) if args.iso else None), indent=2) + "\n", encoding="utf-8")
    print(f"wrote {lock.name} ({len(packages)} packages) and {sbom.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
