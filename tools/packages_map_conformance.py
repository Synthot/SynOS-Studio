#!/usr/bin/env python3
"""packages_map_conformance — prove every concrete package name in
bases/*/packages.map actually exists in the real archive it will be
installed from, on every suite that base's own bases/<base>/sources.tmpl
configures for it.

Nothing has ever checked this before. packages.map is hand-maintained, one
line per abstract role, and until this tool existed nothing pointed those
concrete names at a real archive: ten were found wrong by hand (2026-09-25) —
a plain typo (`hunspell-en`, which is not a package), a rename the archive
itself now records under a different name (`libopenscap8` -> `libopenscap25t64`
on Ubuntu noble, `libopenscap33` on resolute and Debian trixie), something
genuinely unpackaged anywhere (`code`/`code-oss`, VS Code OSS/Codium — no
free-software build exists in either archive), and, the structural case,
a role real on one of a base's own supported suites and genuinely absent on
another (`python3-torch`/`llama.cpp`: real on Ubuntu resolute, absent from
noble). See bases/ubuntu/packages.map's and bases/debian/packages.map's own
comments on `openscap`, `code-oss`, `browser-esr` and `ai-dev@noble` for the
fixes this check's first real run produced, and
tools/render_manifest.py's `load_package_map` for the `abstract-name@suite`
override syntax that exists because of what this tool found.

Checked against the *full* apt view a real build actually configures
(bases/<base>/sources.tmpl: `<suite> <suite>-updates <suite>-backports` from
the main mirror, plus `<suite>-security` from the security mirror) — not
merely the bare suite pocket tools/catalog_conformance.py's own `check` mode
reads for its narrower, per-catalog-entry purpose (a pinned bundle's package
list against its one pinned suite). Checking only the bare pocket
under-reports what a real build can actually install (nvidia-driver-580-open
is missing from plain "noble" but real via noble-updates, for one).

A concrete name absent from the Package: view but present as a Provides: of
some other real package (the archive's own way of recording a rename, e.g.
"myspell-et" providing "hunspell-et") is reported as a warning, not a
failure: apt can still resolve and install it. It is not the exact currency
this tool otherwise wants packages.map to hold — the real name is one
`apt-cache showpkg <name>` away and cheaper to fix than to keep explaining —
so it is still named, just never counted against exit code.

    python3 tools/packages_map_conformance.py [--base ubuntu|debian|all] [--json]

Needs the network (the real archive indexes — tens of megabytes per suite);
never wired into tools/run_checks.py's --level fast/unit/all, which must stay
usable offline. Reachable from there only with `--only packages-map-archives`
(see that file's own module docstring for why a network-needing stage is
handled this way instead of by a --level).

Never invents an archive layout: every URL is built from
bases/<base>/base.env's own APT_MIRROR/SECURITY_MIRROR/COMPONENTS and
bases/<base>/sources.tmpl's own suite list, the same source
tools/render_manifest.py's render() reads to write bases/<base>/sources.tmpl
into a real chroot (build.sh's setup_apt/render_sources_template). Hermetic
in its own test suite (tests/unit/test_packages_map_conformance.py): a fake
fetcher stands in for the network, same pattern as
tests/unit/test_catalog_conformance.py's fake fetcher.
"""
from __future__ import annotations

import argparse
import gzip
import json
import lzma
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTTP_TIMEOUT = 60

sys.path.insert(0, str(ROOT / "tools"))
import render_manifest as rm  # noqa: E402


class ConformanceError(Exception):
    """Configuration or an archive fetch is unusable; nothing to report."""


# --------------------------------------------------------------- fetching
def _http_get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "synos-packages-map-conformance/1"})
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:  # noqa: S310
        return response.read()


def fetch_index(base_url: str, fetcher=_http_get) -> bytes | None:
    """`base_url` is a Packages file's path with no extension; tries .gz
    (every suite pocket has historically carried one) then falls back to
    .xz (what a suite pocket with no separate point-release index — Debian's
    own <suite>-updates/-backports/-security today — actually publishes).
    None when neither exists: a component this suite genuinely has none of
    (Debian's non-free-firmware on an old suite, say), not an error."""
    for ext, decompress in ((".gz", gzip.decompress), (".xz", lzma.decompress)):
        try:
            raw = fetcher(base_url + ext)
        except Exception:  # noqa: BLE001 - 404/network error: try the other extension
            continue
        try:
            return decompress(raw)
        except Exception as exc:  # noqa: BLE001
            raise ConformanceError(f"{base_url}{ext}: could not decompress: {exc}") from exc
    return None


PROVIDES_NAME_RE = re.compile(r"^[^\s(]+")


def parse_packages_index(text: str, names: set[str], provides: dict[str, set[str]]) -> None:
    """Accumulates `Package:`/`Provides:` lines from one Packages index into
    `names` (real, installable-by-that-name packages) and `provides`
    (virtual/renamed name -> the real package(s) that provide it) — the
    archive's own record of a rename, same idiom apt itself resolves
    against."""
    current: str | None = None
    for line in text.splitlines():
        if line.startswith("Package: "):
            current = line[len("Package: "):].strip()
            names.add(current)
        elif line.startswith("Provides: ") and current:
            for item in line[len("Provides: "):].split(","):
                match = PROVIDES_NAME_RE.match(item.strip())
                if match:
                    provides.setdefault(match.group(0), set()).add(current)


def pocket_urls(base_env: dict[str, str], suite: str, arch: str) -> list[tuple[str, str]]:
    """(component, base_url) for every pocket bases/<base>/sources.tmpl
    actually adds for `suite` (the suite itself, -updates and -backports
    from APT_MIRROR; -security from SECURITY_MIRROR) — the exact set a real
    build's chroot gets, so "checked against the real archive" means the
    archive as a real build actually sees it, not only its release pocket."""
    mirror = base_env["APT_MIRROR"].rstrip("/")
    security_mirror = base_env.get("SECURITY_MIRROR", base_env["APT_MIRROR"]).rstrip("/")
    components = base_env.get("COMPONENTS", "main").split()
    pockets = [(mirror, s) for s in (suite, f"{suite}-updates", f"{suite}-backports")]
    pockets.append((security_mirror, f"{suite}-security"))
    return [(component, f"{m}/dists/{s}/{component}/binary-{arch}/Packages")
            for m, s in pockets for component in components]


def load_archive_view(base_env: dict[str, str], suite: str, arch: str, fetcher=_http_get) -> tuple[set[str], dict[str, set[str]]]:
    """Every package name and Provides: this base/suite/arch's full apt view
    (suite + updates + backports + security) actually has, across every
    component base.env names."""
    names: set[str] = set()
    provides: dict[str, set[str]] = {}
    for _component, base_url in pocket_urls(base_env, suite, arch):
        raw = fetch_index(base_url, fetcher=fetcher)
        if raw is None:
            continue
        parse_packages_index(raw.decode("utf-8", "replace"), names, provides)
    return names, provides


# ---------------------------------------------------------- lang/arch sets
def lang_codes(root: Path = ROOT) -> list[str]:
    """Every `${LANG}` a real render actually reaches (tools/render_manifest.py's
    render(): "en" unconditionally, plus lang_pack_code() of every region's
    own locale) — not a hardcoded guess, so a new regions/*.yml file is
    covered the next time this runs, with no update needed here."""
    codes = {"en"}
    for path in sorted((root / "regions").glob("*.yml")):
        data = rm.load_yaml(path)
        for locale in data.get("locales", []) or []:
            codes.add(rm.lang_pack_code(locale.replace(".UTF-8", "")))
    return sorted(codes)


ARCHES = ("amd64", "arm64")  # schema/manifest.schema.json's own arch enum


def arches_for(templates: list[str]) -> list[str]:
    """Only ever more than ["amd64"] if some template actually names
    `${ARCH}` — checking every arch for every role that never varies by
    arch would multiply this tool's own network cost for nothing."""
    if any("${ARCH}" in t for t in templates):
        return list(ARCHES)
    return ["amd64"]


# -------------------------------------------------------------- expansion
def expand(template: str, lang: list[str], arch: str) -> list[str]:
    if "${LANG}" in template:
        return [template.replace("${LANG}", code).replace("${ARCH}", arch) for code in lang]
    return [template.replace("${ARCH}", arch)]


# ----------------------------------------------------------------- check
def check_base(base_id: str, root: Path = ROOT, fetcher=_http_get) -> dict:
    """Checks every concrete name bases/<base_id>/packages.map names, on
    every suite render_manifest.live_capable_suites() says this base can
    actually build a Live image for — a role's own `@suite` override, when
    it has one, is checked only against that suite; the bare role is
    checked against every live-capable suite that has no override for it,
    exactly resolve_packages()'s own lookup order (load_package_map).

    For a `${LANG}` template bases/<base_id>/language-packages.map covers
    (tools/generate_language_packages.py's own output — optional; a base
    with no such file, or a template it does not cover, is checked the old
    way, naive substitution), this checks *that file's* resolved package(s)
    per language instead of the naive `hunspell-${LANG}` substitution this
    tool used before that file existed: a language it correctly marks
    `unavailable` (a fact about the language, checked when the file was
    generated — English needing no libreoffice-l10n-en, Finnish having no
    hunspell dictionary) is counted under `not_applicable`, never `missing`,
    so this stays green without stopping being honest about coverage. A
    language `lang_codes()` reaches that the file has no entry for at all
    (a region added since the file was last generated) is counted under
    `uncovered` — the file falls out of date silently otherwise, exactly the
    kind of gap this whole tool exists to catch."""
    base_dir = root / "bases" / base_id
    base_env = rm.load_env(base_dir / "base.env")
    pkg_map = rm.load_package_map(base_dir / "packages.map")
    rm.check_packages_map_suite_overrides_are_known(base_id, base_env, pkg_map)
    suites = rm.live_capable_suites(base_dir, base_env)
    codes = lang_codes(root)
    lang_pkg_map_path = base_dir / "language-packages.map"
    lang_pkg_map = rm.load_language_package_map(lang_pkg_map_path) if lang_pkg_map_path.is_file() else None

    archives: dict[str, tuple[set[str], dict[str, set[str]]]] = {}

    def archive_for(suite: str) -> tuple[set[str], dict[str, set[str]]]:
        if suite not in archives:
            archives[suite] = load_archive_view(base_env, suite, "amd64", fetcher=fetcher)
        return archives[suite]

    def archive_for_arch(suite: str, arch: str) -> tuple[set[str], dict[str, set[str]]]:
        if arch == "amd64":
            return archive_for(suite)
        key = f"{suite}@{arch}"
        if key not in archives:
            archives[key] = load_archive_view(base_env, suite, arch, fetcher=fetcher)
        return archives[key]

    missing: list[dict] = []
    provides_only: list[dict] = []
    not_applicable: list[dict] = []
    uncovered: list[dict] = []
    checked = 0

    def check_one(concrete: str, role: str, suite: str, arch: str) -> None:
        nonlocal checked
        checked += 1
        if concrete in names:
            return
        if concrete in provides:
            provides_only.append({"role": role, "package": concrete, "suite": suite, "arch": arch,
                                  "provided_by": sorted(provides[concrete])})
            return
        missing.append({"role": role, "package": concrete, "suite": suite, "arch": arch})

    for key, entry in sorted(pkg_map.items()):
        if isinstance(entry, rm.Unavailable) or not entry:
            continue
        role, _, override_suite = key.partition("@")
        target_suites = [override_suite] if override_suite else [s for s in suites if f"{role}@{s}" not in pkg_map]
        for suite in target_suites:
            if suite not in suites:
                continue  # an @suite override for a suite this base cannot even Live-boot: nothing to check for real
            for arch in arches_for(entry):
                names, provides = archive_for_arch(suite, arch)
                for template in entry:
                    if "${LANG}" in template and lang_pkg_map is not None and template in lang_pkg_map:
                        table = lang_pkg_map[template]
                        for code in codes:
                            lookup_key = f"{code}@{suite}" if f"{code}@{suite}" in table else code
                            if lookup_key not in table:
                                uncovered.append({"role": key, "template": template, "lang": code, "suite": suite})
                                continue
                            resolved = table[lookup_key]
                            if isinstance(resolved, rm.Unavailable):
                                not_applicable.append({"role": key, "template": template, "lang": code,
                                                       "suite": suite, "reason": resolved.reason})
                                continue
                            for concrete in resolved:
                                check_one(concrete.replace("${ARCH}", arch), key, suite, arch)
                    else:
                        for concrete in expand(template, codes, arch):
                            check_one(concrete, key, suite, arch)

    return {"base": base_id, "suites": suites, "packages_checked": checked,
            "missing": missing, "provides_only": provides_only,
            "not_applicable": not_applicable, "uncovered": uncovered}


def check_all(bases: list[str], root: Path = ROOT, fetcher=_http_get) -> dict:
    return {"generated_by": "packages_map_conformance", "bases": [check_base(b, root, fetcher=fetcher) for b in bases]}


def real_bases(root: Path = ROOT) -> list[str]:
    bases_dir = root / "bases"
    return sorted(p.name for p in bases_dir.iterdir() if p.is_dir() and not p.name.startswith("_")
                 and (p / "base.env").is_file())


# ------------------------------------------------------------ reporting
def summary_text(report: dict) -> str:
    lines = []
    for result in report["bases"]:
        not_applicable = result.get("not_applicable", [])
        uncovered = result.get("uncovered", [])
        extra = f", {len(not_applicable)} not applicable to their language" if not_applicable else ""
        lines.append(f"{result['base']} ({', '.join(result['suites'])}): "
                     f"{result['packages_checked']} name(s) checked, "
                     f"{len(result['missing'])} missing, {len(result['provides_only'])} via Provides only{extra}")
        for item in result["missing"]:
            lines.append(f"  MISSING {item['role']}: {item['package']!r} not on {result['base']}/{item['suite']} "
                         f"({item['arch']}), checked suite+updates+backports+security")
        for item in result["provides_only"]:
            lines.append(f"  provides-only {item['role']}: {item['package']!r} on {result['base']}/{item['suite']} "
                         f"({item['arch']}) only via Provides of {', '.join(item['provided_by'])} — installable, "
                         f"but packages.map does not name the real package")
        for item in uncovered:
            lines.append(f"  UNCOVERED {item['role']}: language {item['lang']!r} of template {item['template']!r} "
                         f"has no entry in bases/{result['base']}/language-packages.map for suite {item['suite']} — "
                         f"the table is stale (run tools/generate_language_packages.py)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default="all", help="ubuntu, debian, or all (default); comma-separated")
    parser.add_argument("--json", action="store_true", help="one JSON report on stdout instead of the text summary")
    args = parser.parse_args(argv)

    bases = real_bases() if args.base == "all" else [b.strip() for b in args.base.split(",") if b.strip()]
    unknown = set(bases) - set(real_bases())
    if unknown:
        print(f"error: --base names unknown base(s): {', '.join(sorted(unknown))}", file=sys.stderr)
        return 2

    try:
        report = check_all(bases)
    except (ConformanceError, rm.ManifestError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(summary_text(report))

    any_missing = any(result["missing"] or result.get("uncovered") for result in report["bases"])
    return 1 if any_missing else 0


if __name__ == "__main__":
    sys.exit(main())
