#!/usr/bin/env python3
"""build_saturation — generate the saturation-<base> configuration bundle.

    python3 tools/build_saturation.py [--base ubuntu|debian|all] [--output DIR] [--check]

Internal test-engine machinery, not a product. No `bundle-catalog/index.yml`
entry names a saturation bundle, `tools/export_catalog.py` never reads one,
and the Studio page never lists one (`tests/unit/test_saturation.py` proves
both, and fails if a future change starts exporting one). A person still
buys one bundle from the real catalog and runs its own `tools/synos build`
or launcher, exactly as today; nothing here changes that path or runs on
their machine (docs/BUILD_MATRIX.md, "What the saturation targets are, and
are not"). The bundle this tool writes only ever lives under `--output`
(default `.build/saturation/`, this engine's own gitignored build state —
the same place a "core" target's own generated manifest already lives,
tools/build_matrix.py's `_write_core_manifest`); it is written fresh every
time this runs, never committed.

Why this exists (the measurement that justified it): 51 catalogued bundles
each pin one base and resolve their own small package list; building every
one of them separately proves nothing that a single image installing every
package the engine ships would not also prove, at 1/50th the machine-hours.
The two things a per-entry build proves that this cannot are covered in
"What this proves, and does not" below.

The saturation profile for one base is the union of:
  - every application group in profiles/bundles.yml (every one, read
    fresh each run — a group added there needs no update here to be
    covered by the next saturation build)
  - every machine profile's (profiles/*.yml) own `software.packages.add`
    (not what it inherits through `extends` — its own list only, the same
    list tools/render_manifest.py reads before resolving it)

resolved for that base through bases/<base>/packages.map, the exact
function (`resolve_packages`) every other profile is resolved through.

Two honest ways a name can be missing from the result, never confused with
each other:

  unavailable  bases/<base>/packages.map marks the name `unavailable:
               <reason>` — this base's archive has nothing that fills the
               role at all. resolve_packages() already refuses this; the
               whole bundle group (or machine profile's add list) that
               reached it is skipped for this base, named and reasoned in
               the generated profile's own `description:` and in this
               tool's own output. Never silently, and never by inventing a
               substitute package. (Example: "test-engine" needs
               browser-headless, which bases/ubuntu/packages.map marks
               unavailable — no non-snap Chromium on Ubuntu's own archive —
               so the ubuntu saturation bundle skips that one group and the
               debian one does not.)

  excluded     bases/<base>/saturation-exclusions.map (optional, same
               marker idiom as packages.map's `unavailable:` and
               live.map — see load_exclusions()) names a bundle-group id or
               a concrete package name this union would otherwise install
               alongside something it genuinely collides with in the real
               archive (a Conflicts:/Breaks: field, or a maintainer's own
               documented reason for a non-package collision, e.g. a kiosk
               policy against a full desktop). Every entry carries the
               reason in the data; a red saturation build must mean
               something. This tool refuses to start if an entry names
               something that never appears anywhere in the union on that
               base — a stale exclusion is exactly the kind of silent drift
               this idiom exists to prevent.

As of this writing, the real archive check documented in
bases/*/saturation-exclusions.map's own header found zero genuine
Conflicts:/Breaks: collisions in either base's union — both files are
present, and empty of active entries, on purpose: the mechanism exists for
the day a group is added that does collide, checked against the real
archive rather than assumed, exactly the way this repository's own
packages.map and live.map already work.

What this proves, and does not
-------------------------------
A green saturation build means every package this engine ships resolves
and installs together on that base, in one image, today. It cannot see a
bundle's own *configuration* — software.files, security.open_ports, a
service's cap_add/env_file/exec, policy.kiosk_app, a dconf lockdown, a
pinned container image tag — because none of that lives in
profiles/bundles.yml or a machine profile's software.packages.add; it
lives in each bundle-catalog entry's own profile, which this tool never
reads. tools/catalog_conformance.py's `check` mode and the offline,
per-entry package/image checks (docs/BUILD_MATRIX.md) exist for exactly
that reason and are not replaced by anything here.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PROFILES_SKIP = {"bundles.yml", "catalog.yml", "catalog.apps.yml"}
GENERATOR_NAME = "SynOS saturation generator"
GENERATOR_VERSION = "1.0.0"
ARCH = "amd64"


def _load_render_manifest():
    spec = importlib.util.spec_from_file_location("render_manifest_saturation", ROOT / "tools" / "render_manifest.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


rm = _load_render_manifest()


class SaturationError(Exception):
    """A saturation-exclusions.map entry, or the generated bundle itself, is invalid."""


# --------------------------------------------------------------- inputs
def real_bases(root: Path = ROOT) -> list[str]:
    """Every base adapter this engine has (bases/_template excluded), the same
    filter tools/build_matrix.py's own _bases() uses."""
    return sorted(p.name for p in (root / "bases").iterdir() if p.is_dir() and not p.name.startswith("_"))


def machine_profile_files(root: Path = ROOT) -> list[Path]:
    """Every machine profile file (profiles/*.yml), excluding the catalog data
    files that live in the same directory but are not profiles."""
    return sorted(p for p in (root / "profiles").glob("*.yml") if p.name not in PROFILES_SKIP)


def profile_own_add(path: Path) -> tuple[str, list[str]]:
    """A machine profile's own, un-inherited software.packages.add, as plain
    package-reference names (a {name, version} entry contributes its name)."""
    data = rm.load_yaml(path)
    add = ((data.get("software") or {}).get("packages") or {}).get("add") or []
    names = [a if isinstance(a, str) else a["name"] for a in add]
    return str(data.get("id", path.stem)), names


def load_exclusions(base_id: str, root: Path = ROOT) -> dict[str, str]:
    """bases/<base>/saturation-exclusions.map (optional): one `name = excluded:
    <reason>` line per excluded bundle-group id (profiles/bundles.yml) or
    concrete package name — the same marker idiom bases/*/packages.map uses
    for `unavailable:` and bases/*/live.map uses throughout: a reason lives in
    the data, and a name absent from this file is not known to collide with
    anything, never a second "these are fine" list that could drift out of
    step with the union as profiles/bundles.yml grows."""
    path = root / "bases" / base_id / "saturation-exclusions.map"
    if not path.is_file():
        return {}
    mapping: dict[str, str] = {}
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not value.startswith("excluded:"):
            raise SaturationError(f"{path}:{lineno}: {key!r} must be `excluded: <reason>` "
                                   f"(a name that collides with nothing needs no entry in this file at all)")
        mapping[key] = value[len("excluded:"):].strip()
    return mapping


# ------------------------------------------------------------- resolution
def target_suite(base_id: str, root: Path = ROOT) -> str:
    """The one suite the saturation bundle for `base_id` actually targets:
    base.env's own DEFAULT_SUITE when it can back a Live image, else the
    first suite that can (render_manifest.live_capable_suites) — the same
    choice generate_manifest() writes into the generated manifest's own
    `suite:` field, factored out here so plan()'s own package resolution
    (which needs to know which suite it is planning for, now that
    packages.map can carry a per-suite override — see load_package_map)
    can never drift from the suite the generated bundle will actually try
    to build against."""
    base_dir = root / "bases" / base_id
    env = rm.load_env(base_dir / "base.env")
    live_suites = rm.live_capable_suites(base_dir, env)
    suite = env.get("DEFAULT_SUITE", "")
    if suite not in live_suites:
        suite = live_suites[0] if live_suites else suite
    return suite


def _resolve_whole(items: list, pkg_map, base_id: str, label: str, suite: str | None = None) -> tuple[list[str] | None, str | None]:
    """Resolve one bundle group or one profile's whole add list; if the base
    cannot serve any item in it (Unavailable) *on this suite*, the whole
    thing is skipped — (concrete, None) on success, (None, reason) when this
    base/suite says no."""
    try:
        concrete, _unmapped = rm.resolve_packages(items, pkg_map, ARCH, ["en"], base_id, profile_id=label, suite=suite)
    except rm.ManifestError as exc:
        return None, str(exc)
    return concrete, None


def plan(base_id: str, root: Path = ROOT) -> dict:
    """Everything tools/build_matrix.py's saturation target and this tool's own
    --check need for one base: which bundle groups and profile add-lists it
    can serve, which it cannot (named, reasoned), which are excluded (named,
    reasoned), and the resulting resolved concrete package union.

    Resolved against target_suite(base_id) — the exact suite
    generate_manifest() will pin the generated bundle to — so a role that is
    unavailable only on *that* suite (bases/ubuntu/packages.map's ai-dev@noble)
    is named and skipped here the same way a role unavailable on the whole
    base always was, instead of plan() reporting a group as included when the
    generated bundle's own real render() would go on to refuse it."""
    base_dir = root / "bases" / base_id
    pkg_map = rm.load_package_map(base_dir / "packages.map")
    suite = target_suite(base_id, root)
    bundles = rm.load_bundles()
    exclusions = load_exclusions(base_id, root)
    bundle_ids = set(bundles)

    included_bundles: list[str] = []
    skipped_bundles: list[tuple[str, str]] = []     # (id, reason) — this base cannot serve it
    excluded_groups: list[tuple[str, str]] = []      # (id, reason) — saturation-exclusions.map says no

    for gid, abstract in bundles.items():
        if gid in exclusions:
            excluded_groups.append((gid, exclusions[gid]))
            continue
        concrete, reason = _resolve_whole(abstract, pkg_map, base_id, f"saturation-{base_id}:bundle:{gid}", suite=suite)
        if reason is not None:
            skipped_bundles.append((gid, reason))
            continue
        included_bundles.append(gid)

    add_by_profile: dict[str, list[str]] = {}
    skipped_add: list[tuple[str, str]] = []          # (profile_id, reason)
    for path in machine_profile_files(root):
        pid, names = profile_own_add(path)
        if not names:
            continue
        concrete, reason = _resolve_whole(names, pkg_map, base_id, f"saturation-{base_id}:profile:{pid}", suite=suite)
        if reason is not None:
            skipped_add.append((pid, reason))
            continue
        add_by_profile[pid] = names

    concrete_union: set[str] = set()
    for gid in included_bundles:
        concrete, _ = _resolve_whole(bundles[gid], pkg_map, base_id, f"saturation-{base_id}:bundle:{gid}", suite=suite)
        concrete_union.update(concrete or [])
    for pid, names in add_by_profile.items():
        concrete, _ = _resolve_whole(names, pkg_map, base_id, f"saturation-{base_id}:profile:{pid}", suite=suite)
        concrete_union.update(concrete or [])

    package_exclusions: list[tuple[str, str]] = []
    for name, reason in exclusions.items():
        if name in bundle_ids:
            continue  # already applied above, as a whole-group exclusion
        if name not in concrete_union:
            raise SaturationError(
                f"bases/{base_id}/saturation-exclusions.map names {name!r}, which is neither a "
                f"profiles/bundles.yml group nor a package anywhere in the saturation union on base "
                f"{base_id!r} — remove the stale entry or fix the name")
        concrete_union.discard(name)
        package_exclusions.append((name, reason))

    return {
        "base": base_id,
        "included_bundles": included_bundles,
        "skipped_bundles": skipped_bundles,
        "add_by_profile": add_by_profile,
        "skipped_add": skipped_add,
        "excluded_groups": excluded_groups,
        "package_exclusions": package_exclusions,
        "remove": sorted(name for name, _ in package_exclusions),
        "concrete_union": sorted(concrete_union),
    }


# --------------------------------------------------------------- output
def _reasoned_lines(result: dict) -> list[str]:
    """Human-readable "skipped/excluded, and why" lines — the text that goes
    into both the generated profile's own description and this tool's --check
    output, so neither can say something the other does not."""
    lines = []
    for gid, reason in result["skipped_bundles"]:
        lines.append(f"Skipped group {gid!r} (unavailable on {result['base']}): {reason}")
    for gid, reason in result["excluded_groups"]:
        lines.append(f"Excluded group {gid!r}: {reason}")
    for pid, reason in result["skipped_add"]:
        lines.append(f"Skipped profile {pid!r}'s software.packages.add (unavailable on {result['base']}): {reason}")
    for name, reason in result["package_exclusions"]:
        lines.append(f"Excluded package {name!r}: {reason}")
    return lines


def generate_profile(base_id: str, result: dict) -> dict:
    profile_id = f"saturation-{base_id}"
    add = sorted({name for names in result["add_by_profile"].values() for name in names})
    reasoned = _reasoned_lines(result)
    description = (
        f"Internal test-engine bundle, not a product: every application group in "
        f"profiles/bundles.yml this base can serve, plus every machine profile's own "
        f"software.packages.add, resolved for {base_id} — proves every package this "
        f"engine ships resolves and installs together in one image, instead of one "
        f"build per catalogued bundle (docs/BUILD_MATRIX.md). It cannot prove a "
        f"bundle's own configuration (software.files, security.open_ports, a "
        f"service's cap_add/env_file/exec, policy.kiosk_app, a pinned container "
        f"image tag) — that stays with tools/catalog_conformance.py and the "
        f"per-entry offline checks."
    )
    if reasoned:
        description += " " + " ".join(reasoned)
    profile: dict = {
        "id": profile_id,
        "extends": "minimal",
        "description": description,
        "software": {
            "bundles": list(result["included_bundles"]),
            "packages": {"add": add, "remove": list(result["remove"])},
        },
    }
    return profile


def generate_manifest(base_id: str, root: Path = ROOT) -> tuple[dict, str]:
    suite = target_suite(base_id, root)
    manifest = {
        "schema_version": 1,
        "name": f"saturation-{base_id}",
        "version": "1.0.0",
        "base": base_id,
        "suite": suite,
        "arch": ARCH,
        "profile": f"saturation-{base_id}",
        "regions": ["us"],
        "brand": "synos",
        "mirrors": {},
        "packages": {"repository": "", "takeover": "all"},
        "overrides": {},
    }
    return manifest, suite


def write_bundle(base_id: str, dest: Path, root: Path = ROOT) -> dict:
    """Writes one saturation bundle (bundle.json, manifests/, profiles/) to
    `dest`, in the same on-disk shape as a bundle-catalog entry so
    tools/synos bundle validate and tools/build_matrix.py can treat it like
    one — without it ever being one: nothing under bundle-catalog/ names
    this, and dest is always under a gitignored --output, never committed."""
    import yaml

    result = plan(base_id, root)
    profile_id = f"saturation-{base_id}"
    profile = generate_profile(base_id, result)
    manifest, suite = generate_manifest(base_id, root)

    if dest.exists():
        import shutil
        shutil.rmtree(dest)
    (dest / "manifests").mkdir(parents=True)
    (dest / "profiles").mkdir(parents=True)

    manifest_rel = f"manifests/{profile_id}.yml"
    profile_rel = f"profiles/{profile_id}.yml"
    (dest / manifest_rel).write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    (dest / profile_rel).write_text(yaml.safe_dump(profile, sort_keys=False, allow_unicode=True, width=100), encoding="utf-8")

    descriptor = {
        "format": 1,
        "name": profile_id,
        "manifest": manifest_rel,
        "engine": {"min": (root / "VERSION").read_text(encoding="utf-8").strip() if (root / "VERSION").is_file() else "0.1.0"},
        "generator": {"name": GENERATOR_NAME, "version": GENERATOR_VERSION},
        "created": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "description": f"Saturation test bundle (internal, not catalogued) — {base_id} {suite} {ARCH}, "
                        f"profile {profile_id}, {len(result['concrete_union'])} packages",
        "files": [manifest_rel, profile_rel],
    }
    (dest / "bundle.json").write_text(json.dumps(descriptor, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    result["profile_id"] = profile_id
    result["suite"] = suite
    result["dest"] = str(dest)
    return result


# ------------------------------------------------------------------ CLI
def print_report(result: dict, *, dest: Path | None = None) -> None:
    base = result["base"]
    print(f"saturation-{base}: {len(result['concrete_union'])} concrete package(s), "
          f"{len(result['included_bundles'])}/{len(result['included_bundles']) + len(result['skipped_bundles']) + len(result['excluded_groups'])} "
          f"bundle group(s), {len(result['add_by_profile'])} profile add-list(s)"
          + (f" -> {dest}" if dest else ""))
    for line in _reasoned_lines(result):
        print(f"  {line}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="build_saturation", description=__doc__.splitlines()[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default="all", help="ubuntu, debian, or all (default); comma-separated")
    parser.add_argument("--output", type=Path, default=ROOT / ".build" / "saturation")
    parser.add_argument("--check", action="store_true", help="print the plan and package counts; write nothing")
    args = parser.parse_args(argv)

    bases = real_bases() if args.base == "all" else args.base.split(",")
    try:
        for base_id in bases:
            if args.check:
                result = plan(base_id)
                print_report(result)
            else:
                dest = args.output / base_id
                result = write_bundle(base_id, dest)
                print_report(result, dest=dest)
    except SaturationError as exc:
        print(f"saturation error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
