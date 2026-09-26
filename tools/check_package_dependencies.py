#!/usr/bin/env python3
"""check_package_dependencies — every SynOS-authored package that is actually
installed in a real image (a real build's dist/<name>.packages.lock) has its
own Depends/Pre-Depends actually satisfied by that same locked set.

    python3 tools/check_package_dependencies.py dist/*.packages.lock
    python3 tools/check_package_dependencies.py PACKAGES_LOCK --base debian --suite trixie

Each PACKAGES_LOCK is a `name=version` snapshot, one per line, that
tools/sbom.py writes next to a built image (dist/<name>.packages.lock,
build.sh's "Write the package lock and SBOM"). This script also looks for
that same build's own dist/<name>.resolved.json sitting next to it — the
same source tools/smoke_test.py's own --resolved auto-discovery uses — to
learn which base/suite/arch packages/*/control was actually rendered for
(manifest.base/suite/arch); a Depends field can carry a per-base override
(`Depends[debian]:`, resolved the same way tools/build_packages.py's own
apply_base_fields() resolves it for a real build) and this must be checked
against the exact rendering that build used, not guessed. Without a
resolved.json next to the lock, --base/--suite/--version/--arch must be
given explicitly instead.

What this catches, and what it deliberately does not:

This is exactly the class of defect that motivated it: an alternative
dependency (`pkgA | pkgB`, e.g. synos-desktop-core's own
`firmware-sof-synos | firmware-sof-signed`) where a build's own package
selection ends up with *neither* side actually installed — something
removed one alternative without the other ever landing in its place. A
package's Depends is walked clause by clause (comma-separated groups, each
group's `|`-separated alternatives with any `(>= 1.0)`-style version
constraint stripped before comparison); a clause passes if at least one
alternative's bare name is itself in the locked set, or is `Provides:`d by
some *other* package that is itself in the locked set (gathered from every
packages/*/control file's own Provides — e.g. firmware-sof-synos's
`Provides: firmware-sof-signed`), exactly the way dpkg's own dependency
resolution treats a virtual/alternative name.

This only checks the Depends/Pre-Depends this repo itself writes
(packages/*/control) — every other package a real image locks comes from
the base distribution's own archive, whose Depends fields are not available
here without the network (tools/packages_map_conformance.py and
tools/catalog_apt_solver.py already do variations of that, deliberately
kept off of --level fast/unit because they need it). It also never checks a
version constraint's actual number, only that some alternative exists at
all in the locked set — the lock is a flat name=version snapshot, not a
full apt version-comparison context, and every real case of this bug class
is an alternative with nothing on either side, never a too-old version.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DEPENDENCY_FIELDS = ("Depends", "Pre-Depends")
CONSTRAINT_RE = re.compile(r"\s*\(.*?\)")
PACKAGE_FIELD_RE = re.compile(r"^Package:\s*(.+)$", re.MULTILINE)
PROVIDES_FIELD_RE = re.compile(r"^Provides:\s*(.+)$", re.MULTILINE)


def _load(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


build_packages = _load("build_packages_under_check_deps", "tools/build_packages.py")
render_manifest = _load("render_manifest_under_check_deps", "tools/render_manifest.py")


class DependencyCheckError(Exception):
    """A lock file, or the build context needed to check it, is missing or unusable."""


def parse_lock(path: Path) -> dict[str, str]:
    """name=version, one per line, exactly as tools/sbom.py wrote it."""
    locked: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        name, _, version = line.partition("=")
        locked[name.strip()] = version.strip()
    return locked


def resolved_json_path(lock_path: Path) -> Path:
    """dist/<name>.resolved.json sits next to dist/<name>.packages.lock, both
    written by the same build (build.sh, "Write the package lock and SBOM")."""
    name = lock_path.name
    stem = name[: -len(".packages.lock")] if name.endswith(".packages.lock") else lock_path.stem
    return lock_path.with_name(stem + ".resolved.json")


def build_context(lock_path: Path, base_override: str | None, suite_override: str | None,
                   version_override: str | None, arch_override: str | None) -> tuple[dict, dict]:
    """(base_env_dict, manifest_dict) for rendering packages/*/control the same
    way tools/build_packages.py rendered it for this exact build — from the
    build's own resolved.json when one sits next to the lock, else entirely
    from the --base/--suite/--version/--arch overrides."""
    resolved_path = resolved_json_path(lock_path)
    if resolved_path.is_file():
        data = json.loads(resolved_path.read_text(encoding="utf-8"))
        manifest = data["manifest"]
        base_env = data["base"]
        return base_env, {"version": manifest["version"], "suite": manifest["suite"], "arch": manifest["arch"]}
    if not (base_override and suite_override):
        raise DependencyCheckError(
            f"no {resolved_path} next to {lock_path}, and no --base/--suite given; "
            "cannot tell which base/suite packages/*/control should be rendered for")
    bases_dir = ROOT / "bases" / base_override
    env_path = bases_dir / "base.env"
    if not env_path.is_file():
        raise DependencyCheckError(f"unknown base {base_override!r}: {env_path} does not exist")
    base_env = render_manifest.load_env(env_path)
    return base_env, {
        "version": version_override or "0.0.0",
        "suite": suite_override,
        "arch": arch_override or "amd64",
    }


def strip_constraint(alt: str) -> str:
    return CONSTRAINT_RE.sub("", alt).strip()


def dependency_clauses(rendered_control: str) -> list[tuple[str, list[str]]]:
    """(field, [alternative names]) for every Depends/Pre-Depends clause in an
    already base-applied, already-rendered control file's text."""
    clauses: list[tuple[str, list[str]]] = []
    for line in rendered_control.splitlines():
        match = re.match(r"^(" + "|".join(DEPENDENCY_FIELDS) + r"):\s*(.*)$", line)
        if not match:
            continue
        for group in match.group(2).split(","):
            alts = [strip_constraint(a) for a in group.split("|") if a.strip()]
            if alts:
                clauses.append((match.group(1), alts))
    return clauses


def collect_provides(rendered_controls: dict[str, str]) -> dict[str, set[str]]:
    """virtual/alternative name -> set of real package names that Provides it,
    across every control file this build actually rendered (package name ->
    its own rendered control text)."""
    provides: dict[str, set[str]] = {}
    for package_name, text in rendered_controls.items():
        for match in PROVIDES_FIELD_RE.finditer(text):
            for item in match.group(1).split(","):
                name = strip_constraint(item)
                if name:
                    provides.setdefault(name, set()).add(package_name)
    return provides


def check_lock(lock_path: Path, base_override: str | None = None, suite_override: str | None = None,
               version_override: str | None = None, arch_override: str | None = None) -> dict:
    """Check one dist/<name>.packages.lock. Returns {"status": "passed"|"failed"|
    "skipped", "reason": str, "failures": [...]}. Never raises for an ordinary
    missing-context problem — that comes back as "skipped", the same way a
    missing tool skips tools/smoke_test.py's checks rather than failing them."""
    if not lock_path.is_file():
        return {"lock": str(lock_path), "status": "skipped", "reason": f"{lock_path} does not exist", "failures": []}
    try:
        base_env, manifest = build_context(lock_path, base_override, suite_override, version_override, arch_override)
    except DependencyCheckError as exc:
        return {"lock": str(lock_path), "status": "skipped", "reason": str(exc), "failures": []}

    locked = parse_lock(lock_path)
    brand = render_manifest.resolve_brand("synos")
    subs = build_packages.substitutions(manifest, brand, base_env)
    base_id = base_env["BASE_ID"]

    rendered_controls: dict[str, str] = {}
    for control in sorted((ROOT / "packages").glob("*/control")):
        bases_file = control.parent / "bases.txt"
        if bases_file.is_file() and base_id not in bases_file.read_text(encoding="utf-8").split():
            continue
        text = control.read_text(encoding="utf-8")
        rendered = build_packages.apply_base_fields(build_packages.render_text(text, subs), base_id)
        package_match = PACKAGE_FIELD_RE.search(rendered)
        if not package_match:
            continue
        rendered_controls[package_match.group(1).strip()] = rendered

    provides = collect_provides(rendered_controls)

    def satisfied(alt: str) -> bool:
        return alt in locked or bool(provides.get(alt, set()) & locked.keys())

    failures = []
    for package_name, rendered in rendered_controls.items():
        if package_name not in locked:
            continue  # not part of this image; nothing to check
        for field, alts in dependency_clauses(rendered):
            if not any(satisfied(alt) for alt in alts):
                failures.append({
                    "package": package_name, "field": field, "alternatives": alts,
                    "note": f"{package_name} ({field}): none of {' | '.join(alts)} is installed or provided "
                            f"by anything installed in {lock_path.name}",
                })

    status = "failed" if failures else "passed"
    return {
        "lock": str(lock_path), "status": status, "base": base_id, "suite": manifest["suite"],
        "checked_packages": sorted(set(rendered_controls) & set(locked)), "failures": failures,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("locks", nargs="+", type=Path, help="one or more dist/<name>.packages.lock files")
    parser.add_argument("--base", help="base id (debian, ubuntu, ...) when no resolved.json sits next to the lock")
    parser.add_argument("--suite", help="suite (trixie, noble, ...) when no resolved.json sits next to the lock")
    parser.add_argument("--version", help="manifest version substituted into ${VERSION}; defaults to 0.0.0")
    parser.add_argument("--arch", help="manifest arch substituted into ${ARCH}; defaults to amd64")
    args = parser.parse_args(argv)

    results = [check_lock(lock, args.base, args.suite, args.version, args.arch) for lock in args.locks]
    print(json.dumps(results, indent=2))

    if any(r["status"] == "failed" for r in results):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
