#!/usr/bin/env python3
"""Render args.sh from manifest.yml.

manifest.yml is the single source of truth for a SynOS build. This script
validates it, resolves the base adapter, the profile chain, the selected
regions and the brand kit, and writes the args.sh the build engine sources.

    python3 tools/render_manifest.py [--manifest manifest.yml] [--output args.sh]
                                     [--resolved .build/resolved.json] [--check]

Exit codes: 0 rendered, 1 validation error, 2 usage error.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import shlex
import shutil
import sys
from pathlib import Path
from typing import NamedTuple

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("PyYAML is required: apt install python3-yaml")

ROOT = Path(__file__).resolve().parent.parent


class ManifestError(Exception):
    """A manifest, profile, region or brand file is invalid."""


# ----------------------------------------------------------------- loading
def load_yaml(path: Path) -> dict:
    if not path.is_file():
        raise ManifestError(f"missing file: {path.relative_to(ROOT)}")
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ManifestError(f"{path.relative_to(ROOT)} must be a mapping")
    return _stringify_dates(data)


def _stringify_dates(value):
    """YAML turns 2026-09-01 into a date object; the schema wants the string."""
    if isinstance(value, (_dt.date, _dt.datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _stringify_dates(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_stringify_dates(v) for v in value]
    return value


def file_name(display_name: str) -> str:
    """The display name as it appears in file names (ISO, evidence): spaces
    become dashes, anything outside letters, digits, dot, dash is dropped."""
    import unicodedata
    ascii_name = unicodedata.normalize("NFKD", display_name).encode("ascii", "ignore").decode()
    cleaned = re.sub(r"-{2,}", "-", re.sub(r"[^A-Za-z0-9.-]+", "-", ascii_name)).strip("-.")
    return cleaned or "synos"


def load_env(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE lines of a base.env file (shell-quoted values allowed)."""
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        parts = shlex.split(value)
        values[key.strip()] = " ".join(parts)
    return values


def validate_schema(instance: dict, schema_name: str) -> None:
    schema_path = ROOT / "schema" / schema_name
    try:
        import jsonschema
        from jsonschema import Draft202012Validator
        from referencing import Registry, Resource  # type: ignore
    except ImportError:
        try:
            import jsonschema  # noqa: F401
        except ImportError:
            return  # schema validation is best effort without jsonschema
        Registry = None  # type: ignore
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    try:
        if Registry is not None:
            registry = Registry()
            for sibling in (ROOT / "schema").glob("*.schema.json"):
                doc = json.loads(sibling.read_text(encoding="utf-8"))
                registry = registry.with_resource(doc["$id"], Resource.from_contents(doc))
                registry = registry.with_resource(sibling.name, Resource.from_contents(doc))
            validator = Draft202012Validator(schema, registry=registry)
        else:  # older jsonschema: resolve sibling refs by file
            store = {}
            for sibling in (ROOT / "schema").glob("*.schema.json"):
                doc = json.loads(sibling.read_text(encoding="utf-8"))
                store[doc["$id"]] = doc
                store[sibling.name] = doc
            resolver = jsonschema.RefResolver(schema["$id"], schema, store=store)
            validator = jsonschema.Draft202012Validator(schema, resolver=resolver)
        errors = sorted(validator.iter_errors(instance), key=lambda e: list(e.path))
    except Exception as exc:  # pragma: no cover - validator setup problems
        raise ManifestError(f"schema validation failed to run: {exc}") from exc
    if errors:
        lines = [f"{schema_name}: {'/'.join(str(p) for p in e.path) or '<root>'}: {e.message}" for e in errors]
        raise ManifestError("\n".join(lines))


# ----------------------------------------------------------------- resolve
def deep_merge(base: dict, override: dict) -> dict:
    """Merge profile mappings. Lists named add/remove/bundles concatenate; others replace."""
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        elif isinstance(value, list) and isinstance(out.get(key), list) and key in {"add", "remove", "bundles", "apps", "remotes", "repositories", "debs", "appimages", "playbooks", "first_boot_roles", "allowed_groups", "autostart"}:
            merged = list(out[key])
            for item in value:
                if item not in merged:
                    merged.append(item)
            out[key] = merged
        else:
            out[key] = value
    return out


def resolve_profile(profile_id: str, seen: tuple[str, ...] = ()) -> tuple[dict, list[str]]:
    if profile_id in seen:
        raise ManifestError(f"profile extends cycle: {' -> '.join(seen + (profile_id,))}")
    data = load_yaml(ROOT / "profiles" / f"{profile_id}.yml")
    validate_schema(data, "profile.schema.json")
    if data.get("id") != profile_id:
        raise ManifestError(f"profiles/{profile_id}.yml declares id {data.get('id')!r}")
    chain = [profile_id]
    parent = data.get("extends")
    if parent:
        parent_data, parent_chain = resolve_profile(parent, seen + (profile_id,))
        chain = parent_chain + chain
        body = {k: v for k, v in data.items() if k not in {"id", "extends", "description"}}
        merged = deep_merge(parent_data, body)
    else:
        merged = {k: v for k, v in data.items() if k not in {"id", "extends", "description"}}
    return merged, chain


LANG_PACK_MAP = {"zh_CN": "zh-hans", "zh_SG": "zh-hans", "zh_TW": "zh-hant", "zh_HK": "zh-hant"}


def lang_pack_code(locale: str) -> str:
    return LANG_PACK_MAP.get(locale, locale.split("_", 1)[0])


LOCALE_RE = re.compile(r"^[a-z]{2}_[A-Z]{2}(\.UTF-8)?$")


def resolve_regions(ids: list[str]) -> list[dict]:
    regions = []
    for region_id in ids:
        data = load_yaml(ROOT / "regions" / f"{region_id}.yml")
        for key in ("id", "name", "locales", "keyboard", "timezone"):
            if key not in data:
                raise ManifestError(f"regions/{region_id}.yml is missing {key!r}")
        if data["id"] != region_id:
            raise ManifestError(f"regions/{region_id}.yml declares id {data['id']!r}")
        if re.search(r'["\\$|]', data["name"]):
            raise ManifestError(f"regions/{region_id}.yml name contains characters GRUB cannot show")
        locales = [loc.replace(".UTF-8", "") for loc in data["locales"]]
        for loc in locales:
            if not LOCALE_RE.match(loc):
                raise ManifestError(f"regions/{region_id}.yml has malformed locale {loc!r}")
        if not re.fullmatch(r"[a-z0-9_-]+", str(data["keyboard"])):
            raise ManifestError(f"regions/{region_id}.yml keyboard layout is unsafe")
        if not re.fullmatch(r"[A-Za-z0-9_+/-]+", str(data["timezone"])):
            raise ManifestError(f"regions/{region_id}.yml timezone is unsafe")
        data["_locales"] = locales
        regions.append(data)
    return regions


LANGUAGE_NAMES = {
    "fr": "French", "de": "German", "nl": "Dutch", "en": "English", "it": "Italian", "es": "Spanish",
    "pt": "Portuguese", "da": "Danish", "sv": "Swedish", "fi": "Finnish", "pl": "Polish", "ro": "Romanian",
    "el": "Greek", "tr": "Turkish", "uk": "Ukrainian", "ru": "Russian", "ja": "Japanese", "ko": "Korean",
    "zh": "Chinese", "ar": "Arabic", "hi": "Hindi", "id": "Indonesian", "vi": "Vietnamese", "th": "Thai",
    "cs": "Czech", "hu": "Hungarian", "sk": "Slovak", "sl": "Slovenian", "hr": "Croatian", "bg": "Bulgarian",
    "et": "Estonian", "lv": "Latvian", "lt": "Lithuanian", "no": "Norwegian", "nb": "Norwegian", "ga": "Irish",
    "mt": "Maltese", "is": "Icelandic", "he": "Hebrew",
}


def live_region_rows(regions: list[dict]) -> list[tuple[str, str, str, str]]:
    """One GRUB Live entry per (region, locale): code|label|timezone|keyboard."""
    rows: list[tuple[str, str, str, str]] = []
    seen: set[str] = set()
    codes: set[str] = set()
    for region in regions:
        for loc in region["_locales"]:
            if loc in codes:   # two regions sharing a locale (fr_FR in France and Senegal): the first region, the default, wins
                continue
            codes.add(loc)
            lang = LANGUAGE_NAMES.get(loc.split("_")[0], loc.split("_")[0].upper())
            label = f"{region['name']} - {lang}" if len(region["_locales"]) > 1 else region["name"]
            label = str((region.get("labels") or {}).get(loc, label))
            if re.search(r'["\\$|]', label):
                raise ManifestError(f"regions/{region['id']}.yml label {label!r} contains characters GRUB cannot show")
            if label in seen:
                label = f"{label} ({loc})"
            seen.add(label)
            rows.append((loc, label, region["timezone"], str(region["keyboard"])))
    return rows


def resolve_brand(brand_id: str) -> dict:
    data = load_yaml(ROOT / "branding" / brand_id / "brand.yml")
    if data.get("id") != brand_id:
        raise ManifestError(f"branding/{brand_id}/brand.yml declares id {data.get('id')!r}")
    if not re.fullmatch(r"[a-z0-9-]{1,32}", brand_id):
        raise ManifestError("brand id must be lowercase [a-z0-9-], at most 32 characters (ISO volume label)")
    name = str(data.get("display_name", "")).strip()
    if not name or re.search(r'["\\$]', name):
        raise ManifestError("brand display_name is empty or contains characters GRUB cannot show")
    logo = data.get("assets", {}).get("logo", "logo.svg")
    if not (ROOT / "branding" / brand_id / logo).is_file():
        raise ManifestError(f"branding/{brand_id}/{logo} is missing")
    return data


# ------------------------------------------------------------ package map
class Unavailable(NamedTuple):
    """abstract-name = unavailable: <reason> (bases/*/packages.map): this base's
    archive has nothing that fills the role at all — not merely a different
    name, an actual gap (see bases/ubuntu/packages.map's browser-headless for
    the case this exists for). resolve_packages() refuses a profile that
    reaches this name on this base, with the reason, rather than silently
    dropping it the way an empty mapping does for a role that is genuinely
    optional. The same marker, keyed by suite instead of abstract package
    name, is what bases/*/live.map uses (load_live_suite_map) for a suite
    whose archive cannot back a Live image."""
    reason: str


def load_package_map(path: Path) -> dict[str, list[str] | Unavailable]:
    """abstract-name = concrete packages (may be empty: provided elsewhere on this
    base) or `unavailable: <reason>` (see Unavailable).

    A key may instead be `abstract-name@suite` — a per-suite override, for the
    case a base's own archive names a role differently on one of its supported
    suites than on another (or has it on one suite and not another at all):
    availability is decided per *suite*, not per base, and packages.map is
    otherwise a per-base file with no suite dimension at all. resolve_packages()
    tries `f"{name}@{suite}"` first when it is given a suite, falling back to
    the bare `name` when no override exists for that suite — so a role with no
    suite-specific quirk needs no `@suite` line at all, and a base gains suite
    awareness one override at a time rather than every role having to declare
    one. main() checks every `@suite` here actually names a suite the base's
    own SUPPORTED_SUITES lists (check_packages_map_suite_overrides_are_known),
    so a typo or a renamed suite is a repository bug caught here, not a role
    that silently never applies.

    The concrete case this exists for: bases/ubuntu/packages.map's ai-dev
    (python3-torch, llama.cpp) is real on suite resolute and does not exist at
    all on suite noble — checked against archive.ubuntu.com's own indexes,
    including the -updates/-backports/-security pockets bases/ubuntu/
    sources.tmpl actually configures, not merely the bare suite pocket. See
    ai-dev@noble there, and openscap@noble / openscap@resolute for a role
    whose *name* itself (not merely its presence) differs by suite
    (libopenscap25t64 vs libopenscap33 — an upstream SONAME/soversion bump
    landed on different suites' own archive snapshots)."""
    mapping: dict[str, list[str] | Unavailable] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if value.startswith("unavailable:"):
            mapping[key.strip()] = Unavailable(value[len("unavailable:"):].strip())
        else:
            mapping[key.strip()] = value.split()
    return mapping


def check_packages_map_suite_overrides_are_known(base_id: str, base: dict[str, str],
                                                  pkg_map: dict[str, list[str] | Unavailable]) -> None:
    """Every `abstract-name@suite` key in bases/<base_id>/packages.map (see
    load_package_map) must name a suite base.env's own SUPPORTED_SUITES
    actually lists — the same "never a second, independently maintained list
    that can drift" discipline check_live_map_agrees_with_supported_suites
    already applies to live.map, extended to packages.map's own per-suite
    overrides: a typo or a suite renamed on one side and not the other is a
    repository bug, refused here, rather than an override that silently never
    applies to anything."""
    supported = set(base.get("SUPPORTED_SUITES", "").split())
    for key in pkg_map:
        if "@" not in key:
            continue
        _name, _, suite = key.partition("@")
        if suite not in supported:
            raise ManifestError(f"bases/{base_id}/packages.map names {key!r}, but {suite!r} is not one of "
                                f"base.env's SUPPORTED_SUITES ({sorted(supported)}) — fix whichever one is wrong")


def load_live_suite_map(path: Path) -> dict[str, Unavailable]:
    """suite = unavailable: <reason> (bases/*/live.map, optional — one line per
    suite that base.env's SUPPORTED_SUITES accepts but whose own archive
    cannot back a Live image: no package on it provides the dracut modules
    mods/stack.sh's ensure_dracut_live_modules() requires (LIVE_DRACUT_MODULES
    there). Same marker syntax as packages.map's `unavailable:` (see
    Unavailable), keyed by suite instead of abstract package name.

    A suite absent from this file, or a base with no live.map at all (every
    one of its SUPPORTED_SUITES can back a Live image), is not refused here —
    this file only ever lists exceptions, so it can never drift from
    SUPPORTED_SUITES by omission the way a second, independently maintained
    list of "the suites that are fine" could.

    This is render/--check time's early refusal, from what this repository
    already knows about a suite's archive, before a chroot ever runs.
    mods/stack.sh's own ensure_dracut_live_modules() stays in place unchanged
    as the backstop for a suite whose archive changes under us after this
    file was written — this is not a replacement for that check, only an
    earlier, cheaper one for a gap already known."""
    if not path.is_file():
        return {}
    mapping: dict[str, Unavailable] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not value.startswith("unavailable:"):
            raise ManifestError(f"{path}: suite {key!r} must be `unavailable: <reason>` "
                                 f"(a suite that can back a Live image needs no entry in live.map at all)")
        mapping[key] = Unavailable(value[len("unavailable:"):].strip())
    return mapping


def check_live_map_agrees_with_supported_suites(base_id: str, base: dict[str, str], live_map: dict[str, Unavailable]) -> None:
    """bases/<base_id>/live.map must never silently disagree with base.env's
    SUPPORTED_SUITES: a suite it names that SUPPORTED_SUITES does not list is
    a repository bug (a typo, a suite renamed on one side and not the
    other), refused here rather than ignored; likewise a base's own
    DEFAULT_SUITE can never be one live.map marks unavailable — a base that
    cannot even build its own default Live image is not something any
    manifest should be trusted to silently work around."""
    supported = set(base.get("SUPPORTED_SUITES", "").split())
    drift = set(live_map) - supported
    if drift:
        raise ManifestError(f"bases/{base_id}/live.map names suite(s) {sorted(drift)} that "
                             f"base.env's SUPPORTED_SUITES does not — fix whichever one is wrong")
    default_suite = base.get("DEFAULT_SUITE")
    if default_suite in live_map:
        raise ManifestError(f"bases/{base_id}/base.env's DEFAULT_SUITE {default_suite!r} cannot back a Live "
                             f"image: {live_map[default_suite].reason}")


def live_capable_suites(base_dir: Path, env: dict[str, str]) -> list[str]:
    """base.env's SUPPORTED_SUITES minus whatever bases/<base>/live.map marks
    unavailable for a Live image. The one place this exclusion is computed;
    every consumer that lists or builds "the suites this base supports" for
    real (export_catalog's Studio suite choices, build_matrix's core targets)
    calls this instead of reading SUPPORTED_SUITES directly, so a suite the
    engine refuses at render time is never offered or attempted in the first
    place, and no tool hardcodes a suite name to get there."""
    live_map = load_live_suite_map(base_dir / "live.map")
    return [s for s in env.get("SUPPORTED_SUITES", "").split() if s not in live_map]


def load_bundles() -> dict[str, list[str]]:
    data = load_yaml(ROOT / "profiles" / "bundles.yml")
    for name, items in data.items():
        if not isinstance(items, list) or not all(isinstance(i, str) for i in items):
            raise ManifestError(f"profiles/bundles.yml: bundle {name!r} must be a list of abstract names")
    return data


def resolve_packages(
    abstract: list[str], pkg_map: dict[str, list[str] | Unavailable], arch: str, lang_codes: list[str], base_id: str,
    profile_id: str = "", refuse_unavailable: bool = True, suite: str | None = None,
) -> tuple[list[str], list[str]]:
    """Expand abstract names to concrete packages. Unknown names pass through as
    concrete. A name mapped `unavailable: <reason>` (Unavailable) on this base
    fails loudly instead of being dropped — the profile actually reached it
    (it is not merely absent from the map), so silently shipping one package
    short is exactly the bug this refuses. refuse_unavailable=False is for
    resolving a *removal* list, where a role this base never had is simply
    nothing to remove, not a reason to refuse the build.

    `suite`, when given, makes `f"{item}@{suite}"` (load_package_map's
    per-suite override) take precedence over the bare `item` entry — the one
    place that lookup order is decided, so every caller that knows which
    suite it is rendering for (render(), catalog_conformance.py's
    resolved_install_packages(), tools/build_saturation.py's plan()) gets a
    role that differs by suite resolved correctly instead of falling back to
    a name that may only be right for *some* of a base's suites. Left as
    None, resolution is exactly what it was before per-suite overrides
    existed — every existing caller that does not pass it is unaffected."""
    concrete: list[str] = []
    unmapped: list[str] = []

    def add(name: str) -> None:
        if name not in concrete:
            concrete.append(name)

    for item in abstract:
        if isinstance(item, dict):
            only_on = item.get("only_on")
            if only_on and base_id not in only_on:
                continue
            item = item["name"]
        key = f"{item}@{suite}" if suite and f"{item}@{suite}" in pkg_map else item
        if key not in pkg_map:
            unmapped.append(item)
            add(item)
            continue
        entry = pkg_map[key]
        if isinstance(entry, Unavailable):
            if not refuse_unavailable:
                continue
            where = f"profile {profile_id!r} " if profile_id else ""
            raise ManifestError(f"{where}needs {item!r}, which is unavailable on base {base_id!r}: {entry.reason}")
        for template in entry:
            if "${LANG}" in template:
                for code in lang_codes:
                    add(template.replace("${LANG}", code).replace("${ARCH}", arch))
            else:
                add(template.replace("${ARCH}", arch))
    return concrete, unmapped


# -------------------------------------------------------------- profile files
FILES_ALLOWED_PATH = re.compile(r"^/(etc|srv|opt|usr/local)/.+$")
FILES_MODE = re.compile(r"^0[0-7]{3}$")
FILES_MAX_FILE_BYTES = 16 * 1024
FILES_MAX_TOTAL_BYTES = 64 * 1024


def validate_profile_files(files: list[dict]) -> None:
    """software.files (schema/profile.schema.json): configuration written into the
    image by the synos.workstation.profile_files role. The schema already checks
    the path pattern, the mode pattern and an approximate per-file size; this is
    the exact, byte-accurate re-check plus the one thing no schema can express,
    the total across the profile, and a best-effort symlink check (this render
    host is always Debian- or Ubuntu-family, like the eventual build chroot, but
    it is not that chroot, so this can only catch symlinks conventional to the
    family - /etc/mtab and the like - not ones a base image adds after the fact)."""
    total = 0
    for entry in files:
        path = entry["path"]
        if not FILES_ALLOWED_PATH.match(path):
            raise ManifestError(f"software.files: {path!r} must be an absolute path under /etc, /srv, /opt or /usr/local")
        if ".." in path:
            raise ManifestError(f"software.files: {path!r} contains '..'")
        mode = entry.get("mode", "0644")
        if not FILES_MODE.match(mode):
            raise ManifestError(f"software.files: {path!r} has mode {mode!r}, expected e.g. 0644")
        size = len(entry["content"].encode("utf-8"))
        if size > FILES_MAX_FILE_BYTES:
            raise ManifestError(f"software.files: {path!r} is {size} bytes, over the {FILES_MAX_FILE_BYTES}-byte per-file limit")
        total += size
        real = os.path.realpath(path)
        if real != path:
            raise ManifestError(f"software.files: {path!r} resolves through a symlink to {real!r} on this build host")
    if total > FILES_MAX_TOTAL_BYTES:
        raise ManifestError(f"software.files: {total} bytes across the profile, over the {FILES_MAX_TOTAL_BYTES}-byte total limit")


# ------------------------------------------------------- third-party software
def stage_software(profile: dict, base: dict, manifest: dict, staging: Path) -> dict:
    """Write the third-party software a profile declares into a folder the build
    copies to /root/software in the chroot (consumed by mod 08).

    repos/<name>.sources + keyrings/<name>.asc   apt repositories (deb822, Signed-By)
    repos.txt         name<TAB>packages           packages to install from each repository
    debs.txt          url<TAB>sha256              vendor .deb files, verified before install
    flatpak.txt       remote|name|url  app|id     remotes, apps and the preinstall flag
    appimages.txt     name<TAB>url<TAB>sha256
    """
    software = profile.get("software", {}) or {}
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    summary = {"repositories": [], "debs": [], "flatpak": {"remotes": [], "apps": [], "preinstall": True}, "appimages": []}
    arch = manifest["arch"]

    repos = software.get("repositories", []) or []
    if repos:
        (staging / "repos").mkdir()
        (staging / "keyrings").mkdir()
    lines = []
    for repo in repos:
        name = repo["name"]
        if not re.fullmatch(r"[a-z0-9][a-z0-9.+-]*", name):
            raise ManifestError(f"repository name {name!r} must be lowercase [a-z0-9.+-]")
        key = ROOT / repo["key"]
        if not key.is_file():
            raise ManifestError(f"repository {name!r}: key {repo['key']!r} does not exist (paths are relative to the repository root)")
        if not key.read_text(encoding="utf-8", errors="replace").startswith("-----BEGIN PGP PUBLIC KEY BLOCK-----"):
            raise ManifestError(f"repository {name!r}: {repo['key']!r} is not an ASCII-armored public key")
        shutil.copy(key, staging / "keyrings" / f"{name}.asc")
        suite = repo.get("suite", "stable")
        components = repo.get("components", "main")
        url = repo["url"].replace("${ARCH}", arch)      # per-architecture repositories (NVIDIA's container toolkit)
        # a flat repository (Suites: /) has no components line
        components_line = "" if suite == "/" else f"Components: {components}\n"
        (staging / "repos" / f"{name}.sources").write_text(
            f"Types: deb\nURIs: {url}\nSuites: {suite}\n{components_line}"
            f"Architectures: {arch}\nSigned-By: /etc/apt/keyrings/{name}.asc\n", encoding="utf-8")
        pkgs = list(repo.get("packages", []))
        lines.append(f"{name}\t{' '.join(pkgs)}\n")
        summary["repositories"].append({"name": name, "url": url, "suite": suite, "packages": pkgs})
    if lines:
        (staging / "repos.txt").write_text("".join(lines), encoding="utf-8")

    debs = software.get("debs", []) or []
    if debs:
        (staging / "debs.txt").write_text("".join(f"{d['url']}\t{d['sha256']}\n" for d in debs), encoding="utf-8")
        summary["debs"] = [{"url": d["url"], "sha256": d["sha256"]} for d in debs]

    flatpak = software.get("flatpak", {}) or {}
    remotes = flatpak.get("remotes", []) or []
    apps = flatpak.get("apps", []) or []
    preinstall = bool(flatpak.get("preinstall", True))
    if remotes or apps:
        text = f"preinstall|{'true' if preinstall else 'false'}\n"
        if not remotes:
            text += "remote|flathub|https://dl.flathub.org/repo/flathub.flatpakrepo\n"
        for remote in remotes:
            if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", remote["name"]):
                raise ManifestError(f"flatpak remote name {remote['name']!r} must be lowercase [a-z0-9.-]")
            text += f"remote|{remote['name']}|{remote['url']}\n"
        for app in apps:
            text += f"app|{app}\n"
        (staging / "flatpak.txt").write_text(text, encoding="utf-8")
        summary["flatpak"] = {"remotes": remotes or [{"name": "flathub", "url": "https://dl.flathub.org/repo/flathub.flatpakrepo"}], "apps": apps, "preinstall": preinstall}

    appimages = software.get("appimages", []) or []
    if appimages:
        for app in appimages:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", app["name"]):
                raise ManifestError(f"appimage name {app['name']!r} must be [A-Za-z0-9._-]")
        (staging / "appimages.txt").write_text("".join(f"{a['name']}\t{a['url']}\t{a['sha256']}\n" for a in appimages), encoding="utf-8")
        summary["appimages"] = [{"name": a["name"], "url": a["url"]} for a in appimages]
    return summary


# --------------------------------------------------------------- stack
def resolve_stack(manifest: dict) -> dict[str, dict]:
    """Resolve packages/stack.yml into per-group concrete package lists.

    Returns {group: {"packages": [...], "install_recommends": bool, "exclude": [...],
    "only_arch": [...], "optional": [...]}}. packages.takeover in the manifest is
    accepted for compatibility and ignored: every role has its package in the stack.

    `only_base` drops an entry entirely for the other base, decided here at
    render time (this host never has an apt connection to the target suite).
    `optional: true` is for the case only the target suite's own archive can
    settle — a base package folded into a sibling on some releases and not
    installable as its own package there at all (dracut-install into
    dracut-core on Ubuntu jammy) — so the package stays in the list and
    install_stack_group (mods/stack.sh) drops it at install time only if this
    suite's archive does not actually have it."""
    stack = load_yaml(ROOT / "packages" / "stack.yml")
    groups = stack.get("groups") or {}
    resolved: dict[str, dict] = {}
    for group_id, group in groups.items():
        packages: list[str] = []
        optional: list[str] = []
        for entry in group.get("packages") or []:
            if entry.get("only_base") and manifest["base"] not in entry["only_base"]:
                continue          # not part of the stack on this base
            package = str(entry["package"])
            if (ROOT / "packages" / package).is_dir() and not (ROOT / "packages" / package / "control").is_file():
                raise ManifestError(f"packages/stack.yml names {package!r} but packages/{package}/control does not exist")
            packages.append(package)
            if entry.get("optional"):
                optional.append(package)
        resolved[group_id] = {
            "packages": packages,
            "install_recommends": bool(group.get("install_recommends", False)),
            "exclude": list(group.get("exclude") or []),
            "only_arch": list(group.get("only_arch") or []),
            "optional": optional,
        }
    return resolved


def _short(path: Path) -> str:
    return str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)


# ------------------------------------------------------------------ render
def q(value: object) -> str:
    """Double-quoted shell literal; values are validated upstream, this only escapes."""
    text = str(value)
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`") + '"'


def render(manifest: dict, base: dict, profile: dict, chain: list[str], regions: list[dict], brand: dict, manifest_path: Path, pkg_map: dict[str, list[str] | Unavailable], bundles: dict[str, list[str]], live_map: dict[str, Unavailable] | None = None) -> tuple[str, dict]:
    suite = manifest["suite"]
    arch = manifest["arch"]
    if suite not in base.get("SUPPORTED_SUITES", suite).split():
        raise ManifestError(f"suite {suite!r} is not supported by base {manifest['base']!r} ({base.get('SUPPORTED_SUITES')})")
    live_unavailable = (live_map or {}).get(suite)
    if live_unavailable is not None:
        # Every image this engine builds is a live-boot installer ISO
        # (mods/stack.sh's ensure_dracut_live_modules); a suite that cannot
        # back one cannot build at all. Refuse here, at render/--check time,
        # instead of inside the chroot 30+ minutes into a download.
        raise ManifestError(f"suite {suite!r} of base {manifest['base']!r} cannot back a Live image: {live_unavailable.reason}")
    mirrors = manifest.get("mirrors", {}) or {}
    apt_source = mirrors.get("apt") or base["APT_MIRROR"]
    security_source = mirrors.get("security") or base.get("SECURITY_MIRROR", apt_source)
    snapshot = mirrors.get("snapshot") or ""
    snapshot_pinned = False
    if snapshot and not mirrors.get("apt") and base.get("SNAPSHOT_MIRROR"):
        # Pin the whole build to one archive state: every customer rebuild of
        # this manifest resolves the same package versions.
        stamp = snapshot.replace("-", "") + "T000000Z/"
        apt_source = base["SNAPSHOT_MIRROR"].rstrip("/") + "/" + stamp
        security_source = (base.get("SNAPSHOT_SECURITY_MIRROR") or base["SNAPSHOT_MIRROR"]).rstrip("/") + "/" + stamp
        snapshot_pinned = True
    rows = live_region_rows(regions)
    lang_codes: list[str] = ["en"]
    for code, *_ in rows:
        lp = lang_pack_code(code)
        if lp not in lang_codes:
            lang_codes.append(lp)
    language_packs, _ = resolve_packages(["translations"], pkg_map, arch, lang_codes, base["BASE_ID"], suite=suite)
    packages = " ".join(language_packs)
    software = profile.get("software", {}) or {}
    pkgs = software.get("packages", {}) or {}
    add_refs = list(pkgs.get("add", []))
    add = [p if isinstance(p, str) else p["name"] for p in add_refs]
    remove = list(pkgs.get("remove", []))
    bundle_ids = list(software.get("bundles", []))
    unknown_bundles = [b for b in bundle_ids if b not in bundles]
    if unknown_bundles:
        raise ManifestError(f"unknown bundle(s) {unknown_bundles} — define them in profiles/bundles.yml")
    profile_files = list(software.get("files", []))
    validate_profile_files(profile_files)
    abstract: list = []
    for bundle_id in bundle_ids:
        abstract.extend(bundles[bundle_id])
    abstract.extend(add_refs)
    security = profile.get("security", {}) or {}
    if security.get("usbguard"):
        abstract.append("usbguard")
    if security.get("compliance", "none") != "none":
        abstract.append("openscap")
    directory = profile.get("directory", {}) or {}
    if directory.get("join", "none") != "none":
        abstract.append("directory")
    gpu = (profile.get("hardware", {}) or {}).get("gpu", "none")
    if gpu != "none":
        abstract.append(f"gpu-{gpu}")            # the compute stack, per base (bases/*/packages.map)
    install_packages, unmapped = resolve_packages(abstract, pkg_map, arch, lang_codes, base["BASE_ID"], manifest["profile"],
                                                  suite=suite)
    remove_packages, _ = resolve_packages(remove, pkg_map, arch, lang_codes, base["BASE_ID"], manifest["profile"],
                                          refuse_unavailable=False, suite=suite)
    install_packages = [p for p in install_packages if p not in remove_packages]
    installer = profile.get("installer", {}) or {}
    ansible_cfg = profile.get("ansible", {}) or {}
    playbooks = list(ansible_cfg.get("playbooks", []))
    for playbook in playbooks:
        if not (ROOT / playbook).is_file():
            raise ManifestError(f"ansible.playbooks entry {playbook!r} does not exist (paths are relative to the repository root)")
    policy = profile.get("policy", {}) or {}
    ansible_required = bool(
        playbooks or policy.get("dconf") or policy.get("browser") or policy.get("ai")
        or security.get("usbguard") or security.get("compliance", "none") != "none"
        or ansible_cfg.get("first_boot_roles") or ansible_cfg.get("pull_url")
        or installer.get("ssh") == "enabled" or security.get("open_ports")
        or (profile.get("software") or {}).get("services") or (profile.get("hardware") or {}).get("gpu", "none") != "none"
        or profile_files
    )
    stack = resolve_stack(manifest)
    stack_exports = "".join(
        f"export STACK_{gid.upper()}_PACKAGES={q(' '.join(g['packages']))}\n"
        f"export STACK_{gid.upper()}_RECOMMENDS={q('true' if g['install_recommends'] else 'false')}\n"
        f"export STACK_{gid.upper()}_EXCLUDE={q(' '.join(g['exclude']))}\n"
        f"export STACK_{gid.upper()}_ONLY_ARCH={q(' '.join(g['only_arch']))}\n"
        f"export STACK_{gid.upper()}_OPTIONAL={q(' '.join(g['optional']))}\n"
        for gid, g in stack.items()
    )
    packages_cfg = manifest.get("packages") or {}
    generated = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    region_lines = "\n".join("|".join(row) for row in rows)
    out = f"""#!/bin/bash
#=================================================
#  GENERATED FILE — DO NOT EDIT
#=================================================
# Rendered from {_short(manifest_path)} by tools/render_manifest.py
# on {generated}. Edit the manifest, profile, region or brand files and run
# `make args`. This file is sourced by build.sh and copied into the chroot.

#==========================
# Builder Environment Variables
#==========================
export DEBIAN_FRONTEND=noninteractive
SCRIPT_DIR="$(dirname "$(readlink -f "$0")")"
export SCRIPT_DIR
export LC_ALL=en_US.UTF-8
export LANG=en_US.UTF-8
export LANGUAGE=en_US:en

#==========================
# Manifest identity
#==========================
export MANIFEST_NAME={q(manifest['name'])}
export MANIFEST_VERSION={q(manifest['version'])}
export PROFILE_ID={q(chain[-1])}
export PROFILE_CHAIN={q(' '.join(chain))}
export PROFILE_BUNDLES={q(' '.join(bundle_ids))}
export PROFILE_PACKAGES_ADD={q(' '.join(add))}
export PROFILE_PACKAGES_REMOVE={q(' '.join(remove))}
# Concrete package lists resolved through bases/{manifest['base']}/packages.map (mod 06).
export PROFILE_INSTALL_PACKAGES={q(' '.join(install_packages))}
export PROFILE_REMOVE_PACKAGES={q(' '.join(remove_packages))}
export INSTALLER_FILESYSTEM={q(installer.get('filesystem', 'btrfs'))}
export INSTALLER_ENCRYPTION={q(installer.get('encryption', 'default-on'))}
export INSTALLER_SSH={q(installer.get('ssh', 'disabled'))}
export SECURITY_COMPLIANCE={q(security.get('compliance', 'none'))}
# Ansible runs inside the chroot (build.sh) when the profile needs policy,
# hardening, or customer playbooks; the variables file carries the profile.
export ANSIBLE_CHROOT_REQUIRED={q('true' if ansible_required else 'false')}
export ANSIBLE_PLAYBOOKS={q(' '.join(playbooks))}
export ANSIBLE_VARS_FILE=".build/ansible-vars.json"
# Third-party software staged by the renderer for mod 08 (repositories, debs, flatpak, appimages).
export SOFTWARE_STAGING_DIR=".build/software"

#==========================
# Base adapter: bases/{manifest['base']}/
#==========================
export BASE_ID={q(base['BASE_ID'])}
export BASE_FAMILY={q(base.get('BASE_FAMILY', 'deb'))}
export BOOTSTRAP_TOOL={q(base.get('BOOTSTRAP_TOOL', 'debootstrap'))}
export TARGET_SUITE={q(suite)}
# Legacy name still read by build.sh and the makefile; equals TARGET_SUITE.
export TARGET_UBUNTU_VERSION={q(suite)}
export APT_SOURCE={q(apt_source)}
export SECURITY_SOURCE={q(security_source)}
export APT_COMPONENTS={q(base.get('COMPONENTS', 'main'))}
export APT_KEYRING={q(base.get('KEYRING', ''))}
export SQUASHFS_LEVEL={q(str((manifest.get('image') or {}).get('squashfs_level', 19)))}
export SNAPSHOT_DATE={q(snapshot)}
export SNAPSHOT_PINNED={q('true' if snapshot_pinned else 'false')}
# The live mirror installed systems update from (the snapshot is build-time only).
export RELEASE_APT_SOURCE={q(mirrors.get('apt') or base['APT_MIRROR'])}
export RELEASE_SECURITY_SOURCE={q(mirrors.get('security') or base.get('SECURITY_MIRROR', base['APT_MIRROR']))}
export KERNEL_PACKAGE={q(base.get('KERNEL_PACKAGE', '').replace('${ARCH}', arch))}
export GRUB_SIGNED_PACKAGES={q(base.get('GRUB_SIGNED_PACKAGES', ''))}

#==========================
# Product identity: branding/{brand['id']}/
#==========================
export TARGET_NAME={q(brand['id'])}
export TARGET_BUSINESS_NAME={q(brand['display_name'])}
export TARGET_FILE_NAME={q(file_name(brand['display_name']))}
export TARGET_BUILD_VERSION={q(manifest['version'])}
export TARGET_ARCH="${{TARGET_ARCH:-{arch}}}"
export BRAND_ID={q(brand['id'])}
export BRAND_VENDOR={q(brand.get('vendor', brand['display_name']))}
export BRAND_TAGLINE={q(brand.get('tagline', ''))}
export BRAND_ACCENT={q((brand.get('colors') or {}).get('accent', '#0E6B7A'))}
export BRAND_HOSTNAME_PREFIX={q(brand.get('hostname_prefix', brand['id']))}
export BRAND_URL_HOME={q((brand.get('urls') or {}).get('home', ''))}
export BRAND_URL_SUPPORT={q((brand.get('urls') or {}).get('support', ''))}
export BRAND_URL_BUGS={q((brand.get('urls') or {}).get('bug_report', ''))}
export BRAND_URL_PRIVACY={q((brand.get('urls') or {}).get('privacy', ''))}

#==========================
# Regions: {' '.join(r['id'] for r in regions)} (default {regions[0]['id']})
#==========================
export REGION_IDS={q(' '.join(r['id'] for r in regions))}
export REGION_DEFAULT={q(regions[0]['id'])}
export LANG_PACK_CODES={q(' '.join(lang_codes))}
export LANGUAGE_PACKS={q(packages)}

# GRUB / Live regional policy. One entry per region locale.
# Format: locale_code|GRUB label|timezone|XKB layout
export SUPPORTED_LIVE_REGIONS="
{region_lines}
"

#============================
# Product stack: packages/stack.yml resolved per group
#============================
export STACK_GROUPS={q(' '.join(stack))}
export SYNOS_REPO_URL={q(packages_cfg.get('repository', '') or '')}
export LOCAL_REPO_DIR=".build/repo"
{stack_exports}"""
    resolved = {
        "generated": generated,
        "manifest": manifest,
        "base": base,
        "profile_chain": chain,
        "profile": profile,
        "regions": [{k: v for k, v in r.items() if not k.startswith("_")} for r in regions],
        "live_entries": [dict(zip(("locale", "label", "timezone", "keyboard"), row)) for row in rows],
        "brand": brand,
        "language_packs": lang_codes,
        "stack": {"groups": stack},
        "packages": {
            "abstract": [a if isinstance(a, str) else a["name"] for a in abstract],
            "install": install_packages,
            "remove": remove_packages,
            "unmapped": unmapped,
        },
    }
    return out, resolved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", default=str(ROOT / "manifest.yml"))
    parser.add_argument("--output", default=str(ROOT / "args.sh"))
    parser.add_argument("--resolved", default=str(ROOT / ".build" / "resolved.json"))
    parser.add_argument("--check", action="store_true", help="validate only, write nothing")
    args = parser.parse_args(argv)
    manifest_path = Path(args.manifest).resolve()
    try:
        manifest = load_yaml(manifest_path)
        validate_schema(manifest, "manifest.schema.json")
        base_dir = ROOT / "bases" / manifest["base"]
        if not (base_dir / "base.env").is_file():
            raise ManifestError(f"unknown base {manifest['base']!r}: bases/{manifest['base']}/base.env is missing")
        base = load_env(base_dir / "base.env")
        profile, chain = resolve_profile(manifest["profile"])
        overrides = manifest.get("overrides") or {}
        if overrides:
            profile = deep_merge(profile, overrides)
        regions = resolve_regions(list(manifest["regions"]))
        brand = resolve_brand(manifest["brand"])
        pkg_map = load_package_map(base_dir / "packages.map")
        check_packages_map_suite_overrides_are_known(manifest["base"], base, pkg_map)
        bundles = load_bundles()
        live_map = load_live_suite_map(base_dir / "live.map")
        check_live_map_agrees_with_supported_suites(manifest["base"], base, live_map)
        text, resolved = render(manifest, base, profile, chain, regions, brand, manifest_path, pkg_map, bundles, live_map)
    except ManifestError as exc:
        print(f"manifest error: {exc}", file=sys.stderr)
        return 1
    unmapped = resolved["packages"]["unmapped"]
    if unmapped:
        print(f"note: {len(unmapped)} package name(s) not in bases/{manifest['base']}/packages.map, "
              f"passed through as concrete: {' '.join(unmapped)}", file=sys.stderr)
    if args.check:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            try:
                stage_software(profile, base, manifest, Path(tmp) / "software")
            except ManifestError as exc:
                print(f"manifest error: {exc}", file=sys.stderr)
                return 1
        print(f"OK: {_short(manifest_path)} -> base {manifest['base']} {manifest['suite']} {manifest['arch']}, "
              f"profile {' > '.join(chain)}, regions {' '.join(r['id'] for r in regions)}, brand {brand['id']}, "
              f"{len(resolved['live_entries'])} live entries")
        return 0
    output = Path(args.output)
    output.write_text(text, encoding="utf-8")
    resolved_path = Path(args.resolved)
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_path.write_text(json.dumps(resolved, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    software_dir = resolved_path.with_name("software")
    resolved["software"] = stage_software(profile, base, manifest, software_dir)
    resolved_path.write_text(json.dumps(resolved, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    ansible_vars = resolved_path.with_name("ansible-vars.json")
    ansible_vars.write_text(json.dumps({
        "synos_profile": resolved["profile"], "synos_brand": resolved["brand"],
        "synos_manifest": resolved["manifest"], "synos_regions": resolved["regions"],
    }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"rendered {_short(output)} ({len(resolved['live_entries'])} live entries) and {_short(resolved_path)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
