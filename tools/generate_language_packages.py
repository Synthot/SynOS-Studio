#!/usr/bin/env python3
"""generate_language_packages — write bases/<base>/language-packages.map: the
real, archive-verified concrete package name for every language this engine
can be asked for, for every `${LANG}` template in bases/<base>/packages.map,
on every suite that base can Live-boot.

THE PROBLEM this replaces: bases/*/packages.map's `${LANG}` templates
(`hunspell-${LANG}`, `libreoffice-l10n-${LANG}`, `language-pack-${LANG}`,
`language-pack-gnome-${LANG}`, `firefox-esr-l10n-${LANG}`) assume one naming
convention holds for every one of this engine's 39 language codes (derived
from regions/*.yml, see lang_codes() below). It does not. `hunspell-de` is
not a package (the real name is `hunspell-de-de`); `libreoffice-l10n-en`
never existed and never will (English is LibreOffice's own source language);
`hunspell-et` exists only as a `Provides:` of `myspell-et`; Finnish has no
hunspell dictionary in either archive at all, only an entirely different
package family (`voikko-fi`). tools/packages_map_conformance.py's own first
real run against the archive (2026-09-25) found 67 such misses across 39
distinct names, all in the spellcheck and translations roles.

THE FIX: this script queries the *real* archive (the same full apt view
tools/packages_map_conformance.py checks against — suite + updates +
backports + security, every component base.env names) for every language
code, every `${LANG}` template, every live-capable suite, and writes the
answer to bases/<base>/language-packages.map — never a hand-written guess.
tools/render_manifest.py's resolve_packages() (via its `lang_pkg_map`
parameter) and tools/packages_map_conformance.py's own check_base() both
read that file and use its answer instead of naively substituting the
language code into the template; mods/02-hostname-language-pack-mod and
mods/06-profile-software never see a name this file did not already verify.

RESOLUTION ORDER, per (template, language, base, suite):
  1. The literal substitution (`hunspell-de`) if the archive has it as a
     real `Package:`.
  2. If the literal substitution is only a `Provides:` of some other real
     package (`hunspell-et` -> `myspell-et`), the *real* package's name —
     never the virtual name, so nothing here depends on Provides
     resolution the way packages.map historically has for `et`.
  3. A same-family regional variant (`hunspell-de-de`, `hunspell-en-us`)
     when the literal substitution does not exist but the family does,
     split by country the way upstream actually packages some languages'
     dictionaries/localisations (German, English, Portuguese, Spanish,
     Chinese). Chosen by, in order: an explicit, documented correction for
     the one case this engine's own language-code scheme (LANG_PACK_MAP)
     diverges from the archive's own locale-style suffix (`zh-hans`/
     `zh-hant` vs. the archive's `zh-cn`/`zh-tw`); the country of the
     region in regions/ whose own id equals this language code (`de.yml`
     for `de`, `pt.yml` for `pt`, `es.yml` for `es` — this engine's own
     catalogue, not an assumption); this engine's own baseline locale
     (`en_US`, tools/render_manifest.py's hardcoded `LC_ALL`) for `en`
     specifically; or, when none of those apply but exactly one real
     regional variant exists, that one (unambiguous either way).
  4. For the spellcheck role only (any `hunspell-${LANG}` template): the
     same three checks again against the `myspell-` family — a mechanical
     prefix swap, not a per-language guess — since Debian/Ubuntu ship a
     real, working dictionary under the older `myspell-` name for several
     languages that were never renamed to `hunspell-` at all (`ga`, `nb`).
  5. A short, explicit, *verified* table of languages whose spellchecker is
     not a hunspell/myspell dictionary at all, e.g. Finnish, whose
     spellcheck is provided by an entirely different technology, Voikko
     (`voikko-fi` plus the `libenchant-2-voikko` bridge so the desktop's
     enchant-based spellchecker can actually find it, plus
     `libreoffice-voikko` for LibreOffice's own native support) — a
     documented human claim (openscap@noble/@resolute's fix is the same
     kind of exception), but every package it names is still checked
     against the real archive before this script ever writes it out, so a
     wrong or stale claim here fails the generation, not silently ships.
  6. `unavailable: <reason>`, generated from what was actually checked
     (which packages, which Provides, which regional variants) — a
     language this software genuinely has nothing for, on this base and
     suite, not a name this script failed to guess. Distinguishes the
     the two cases the finding asked for: English needing no
     `libreoffice-l10n-en` is a fact about English, not a gap; Luxembourgish
     having no package in *any* of these families on *either* archive is a
     fact about Luxembourgish's own packaging, not a bug in this file.

    python3 tools/generate_language_packages.py [--base ubuntu|debian|all] [--check]

--check writes nothing and exits 1 if bases/<base>/language-packages.map is
stale (regenerating it would produce different content) — for CI, or before
trusting a checker run against it. Needs the network, like
tools/packages_map_conformance.py; never wired into --level fast/unit/all.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import render_manifest as rm  # noqa: E402
import packages_map_conformance as pmc  # noqa: E402

# The one place this engine's own zh-hans/zh-hant script codes (LANG_PACK_MAP)
# are known to diverge from the archive's own locale-style code entirely for
# a *regional-variant* package (as opposed to Ubuntu's language-pack family,
# which natively ships "language-pack-zh-hans"/"-zh-hant" and needs no
# correction at all): checked against archive.ubuntu.com's and
# deb.debian.org's real indexes, 2026-09-25 — no "libreoffice-l10n-zh-hans"
# or "firefox-esr-l10n-zh-hans" package or Provides exists on either archive;
# "libreoffice-l10n-zh-cn"/"firefox-esr-l10n-zh-cn" (and the -zh-tw pair) do.
# The value is the *whole* replacement code (not a country suffix appended
# after the language code the way the regional-variant regex works for every
# other language below) because the archive's own code for these two is not
# "zh-hans-cn", it is "zh-cn" — a total rename, not a regional split.
LANG_CODE_OVERRIDE = {"zh-hans": "zh-cn", "zh-hant": "zh-tw"}

# Languages whose spellchecker is not a hunspell/myspell dictionary at all —
# checked package-by-package against the real archive before being written
# (see verify_technology_override below), not merely asserted here.
SPELLCHECK_TECHNOLOGY_OVERRIDE = {
    "fi": {
        "packages": ["voikko-fi", "libenchant-2-voikko", "libreoffice-voikko"],
        "reason": "Finnish has no hunspell or myspell dictionary in either archive at all; Finnish "
                   "spellcheck is provided by an entirely different technology, Voikko, which needs "
                   "voikko-fi (the dictionary/engine), libenchant-2-voikko (the bridge so the desktop's "
                   "enchant-based spellchecker can use it) and libreoffice-voikko (LibreOffice's own "
                   "native Voikko support) — not a rename of this role, three packages this base's "
                   "hunspell-${LANG}/myspell-${LANG} templates could never have named.",
    },
}

# Templates this script resolves; every one of these appears verbatim in at
# least one of bases/*/packages.map's ${LANG} lines today. A future template
# added there that is not in this list is reported by main() rather than
# silently skipped (see UNHANDLED_TEMPLATE below).
KNOWN_TEMPLATE_PREFIXES = ("hunspell-", "libreoffice-l10n-", "firefox-esr-l10n-",
                           "language-pack-gnome-", "language-pack-")


REGIONAL_VARIANT_RE_CACHE: dict[str, re.Pattern] = {}


def regional_variant_re(prefix: str, lang: str) -> re.Pattern:
    key = f"{prefix}\0{lang}"
    if key not in REGIONAL_VARIANT_RE_CACHE:
        REGIONAL_VARIANT_RE_CACHE[key] = re.compile(rf"^{re.escape(prefix)}{re.escape(lang)}-[a-z0-9]+$")
    return REGIONAL_VARIANT_RE_CACHE[key]


def namesake_region_country(lang: str, root: Path) -> str | None:
    """The country suffix (lowercase) of regions/<lang>.yml's own locale, when
    such a region exists and actually lists `lang` as one of its locales —
    this engine's own region catalogue, read fresh each run, never a
    hardcoded language->country table (the one exception, CJK's script-code
    vs. locale-code mismatch, is COUNTRY_HINT_OVERRIDE above, and is checked
    first by resolve_regional_variant, not here)."""
    path = root / "regions" / f"{lang}.yml"
    if not path.is_file():
        return None
    data = rm.load_yaml(path)
    for locale in data.get("locales", []) or []:
        code = locale.replace(".UTF-8", "")
        if "_" not in code:
            continue
        code_lang, _, country = code.partition("_")
        if rm.lang_pack_code(code) == lang or code_lang == lang:
            return country.lower()
    return None


def resolve_regional_variant(names: set[str], provides: dict[str, set[str]], prefix: str, lang: str,
                              root: Path) -> tuple[str, str] | None:
    """A same-family package for `lang` under `prefix` when the bare
    `prefix+lang` name is not real but a country-suffixed variant is. Returns
    (concrete_name, how) or None. `how` is folded into the generated
    comment so a person can see *why* this suffix was picked, not just that
    it was."""
    replacement = LANG_CODE_OVERRIDE.get(lang)
    if replacement:
        candidate = f"{prefix}{replacement}"
        if candidate in names:
            return candidate, (f"{lang!r} is this engine's own script code (render_manifest.LANG_PACK_MAP); "
                               f"the archive's own package for this family uses the locale-style code "
                               f"{replacement!r} instead")
        if candidate in provides:
            real_names = sorted(provides[candidate])
            if len(real_names) == 1:
                return real_names[0], (f"{lang!r} is this engine's own script code; the archive's own real "
                                       f"package behind the Provides: {candidate!r}")
    pat = regional_variant_re(prefix, lang)
    real = sorted(n for n in names if pat.match(n))
    if not real:
        return None
    home = namesake_region_country(lang, root)
    if home:
        candidate = f"{prefix}{lang}-{home}"
        if candidate in real:
            return candidate, f"regions/{lang}.yml's own locale uses country {home!r}"
    if lang == "en":
        candidate = f"{prefix}en-us"
        if candidate in real:
            return candidate, "en_US is this engine's own baseline locale (render_manifest.py's hardcoded LC_ALL)"
    if len(real) == 1:
        return real[0], "the only real regional variant for this language in this family"
    return None  # genuinely ambiguous: multiple regional variants, no signal to pick one — do not guess


def resolve_one(names: set[str], provides: dict[str, set[str]], prefix: str, lang: str,
                 root: Path) -> tuple[str, str] | None:
    """(concrete_name, how) for `prefix+lang} against this one family
    (`prefix`, e.g. "hunspell-"), trying the exact name, then a real
    package behind a Provides:, then a regional variant. None when this
    family has nothing for this language at all."""
    exact = f"{prefix}{lang}"
    if exact in names:
        return exact, "exact match in the archive's own Package: list"
    if exact in provides:
        real_names = sorted(provides[exact])
        if len(real_names) == 1:
            return real_names[0], f"the archive's own real package behind the Provides: {exact!r}"
        return None  # multiple real packages provide the same virtual name: ambiguous, do not guess
    variant = resolve_regional_variant(names, provides, prefix, lang, root)
    if variant:
        return variant
    return None


def verify_technology_override(names: set[str], provides: dict[str, set[str]], lang: str) -> list[str] | None:
    override = SPELLCHECK_TECHNOLOGY_OVERRIDE.get(lang)
    if not override:
        return None
    missing = [p for p in override["packages"] if p not in names and p not in provides]
    if missing:
        raise pmc.ConformanceError(
            f"SPELLCHECK_TECHNOLOGY_OVERRIDE for {lang!r} names {missing!r}, which this archive does not have "
            f"(checked Package: and Provides:) — the override is stale; fix tools/generate_language_packages.py")
    return list(override["packages"])


def resolve_template(names: set[str], provides: dict[str, set[str]], template: str, lang: str,
                      root: Path) -> tuple[list[str], str] | tuple[None, str]:
    """(concrete_packages, how) or (None, reason) for one `${LANG}` template
    and one language, against one archive view. `how`/`reason` are written
    into bases/<base>/language-packages.map as the line's comment."""
    prefix = template.split("${LANG}")[0]
    suffix = template.split("${LANG}")[1] if "${LANG}" in template else ""
    hit = resolve_one(names, provides, prefix, lang, root)
    if hit:
        name, how = hit
        return [name + suffix], how
    if prefix == "hunspell-":
        hit = resolve_one(names, provides, "myspell-", lang, root)
        if hit:
            name, how = hit
            return [name + suffix], f"no hunspell-family package for {lang!r}; {how}, under myspell- instead"
        override = verify_technology_override(names, provides, lang)
        if override:
            reason = SPELLCHECK_TECHNOLOGY_OVERRIDE[lang]["reason"]
            return override, reason
        return None, (f"no {prefix}{lang}[-<country>] or myspell-{lang}[-<country>] package or Provides "
                      f"found in the archive")
    return None, f"no {template.replace('${LANG}', lang)} (or regional variant, or Provides) found in the archive"


# ------------------------------------------------------------- packages.map
def templates_in(pkg_map_path: Path) -> list[str]:
    """Every distinct `${LANG}` template appearing in this base's
    packages.map, in first-seen order — read fresh, never hand-listed, so a
    packages.map line added later is picked up the next time this runs."""
    pkg_map = rm.load_package_map(pkg_map_path)
    seen: list[str] = []
    for entry in pkg_map.values():
        if isinstance(entry, rm.Unavailable):
            continue
        for template in entry:
            if "${LANG}" in template and template not in seen:
                seen.append(template)
    return seen


UNHANDLED_TEMPLATE = ("a ${{LANG}} template {template!r} in bases/{base}/packages.map has no resolution logic in "
                      "tools/generate_language_packages.py (KNOWN_TEMPLATE_PREFIXES) — add one before trusting "
                      "this file for it")


# --------------------------------------------------------------- rendering
def render_map_file(base_id: str, per_template: dict[str, dict[str, dict]]) -> str:
    """bases/<base_id>/language-packages.map's text. `per_template` is
    {template: {lang_or_lang@suite: {"packages": [...], "how": str} |
    {"unavailable": reason}}}."""
    generated = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# GENERATED FILE — do not hand-edit.",
        "#",
        f"# Written by tools/generate_language_packages.py on {generated}, checked against the",
        "# real archive (suite + updates + backports + security, every component base.env",
        "# names) this base actually configures — see that script's own module docstring for",
        "# the resolution order. Re-run it after a region/language is added, or when this base's",
        "# own archive snapshot has moved on.",
        "#",
        "# key = template/language-code[@suite]",
        "# value = concrete package name(s), space-separated, or `unavailable: <reason>` (same",
        "#         marker as packages.map's own Unavailable — a language this software genuinely",
        "#         has nothing for on this base/suite, not a name this script failed to guess).",
        "#",
        "# Read by tools/render_manifest.py's resolve_packages() (its `lang_pkg_map` parameter) in",
        "# place of naively substituting the language code into the ${LANG} template, and by",
        "# tools/packages_map_conformance.py's check_base() for the same reason.",
        "",
    ]
    for template, langs in per_template.items():
        lines.append(f"# {template}")
        for key in sorted(langs):
            entry = langs[key]
            if "unavailable" in entry:
                lines.append(f"{template}/{key} = unavailable: {entry['unavailable']}")
            else:
                comment = f"  # {entry['how']}" if entry.get("how") else ""
                lines.append(f"{template}/{key} = {' '.join(entry['packages'])}{comment}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def generate_for_base(base_id: str, root: Path = ROOT, fetcher=pmc._http_get) -> str:
    base_dir = root / "bases" / base_id
    base_env = rm.load_env(base_dir / "base.env")
    pkg_map = rm.load_package_map(base_dir / "packages.map")
    rm.check_packages_map_suite_overrides_are_known(base_id, base_env, pkg_map)
    suites = rm.live_capable_suites(base_dir, base_env)
    codes = pmc.lang_codes(root)
    templates = templates_in(base_dir / "packages.map")

    unhandled = [t for t in templates if not any(t.startswith(p) for p in KNOWN_TEMPLATE_PREFIXES)]
    if unhandled:
        raise pmc.ConformanceError(UNHANDLED_TEMPLATE.format(template=unhandled[0], base=base_id))

    archives = {s: pmc.load_archive_view(base_env, s, "amd64", fetcher=fetcher) for s in suites}

    per_template: dict[str, dict[str, dict]] = {}
    for template in templates:
        per_lang: dict[str, dict] = {}
        for lang in codes:
            by_suite: dict[str, tuple[list[str] | None, str]] = {}
            for suite in suites:
                names, provides = archives[suite]
                by_suite[suite] = resolve_template(names, provides, template, lang, root)
            resolutions = {(tuple(pkgs) if pkgs is not None else None, reason) for pkgs, reason in by_suite.values()}
            if len(resolutions) == 1:
                packages, how_or_reason = next(iter(resolutions))
                packages = list(packages) if packages is not None else None
                per_lang[lang] = ({"packages": packages, "how": how_or_reason} if packages is not None
                                  else {"unavailable": how_or_reason})
            else:
                for suite in suites:
                    packages, how_or_reason = by_suite[suite]
                    per_lang[f"{lang}@{suite}"] = ({"packages": packages, "how": how_or_reason} if packages is not None
                                                    else {"unavailable": how_or_reason})
        per_template[template] = per_lang

    return render_map_file(base_id, per_template)


def _drop_timestamp_line(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.startswith("# Written by "))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default="all", help="ubuntu, debian, or all (default); comma-separated")
    parser.add_argument("--check", action="store_true", help="write nothing; exit 1 if the file is stale")
    args = parser.parse_args(argv)

    bases = pmc.real_bases() if args.base == "all" else [b.strip() for b in args.base.split(",") if b.strip()]
    unknown = set(bases) - set(pmc.real_bases())
    if unknown:
        print(f"error: --base names unknown base(s): {', '.join(sorted(unknown))}", file=sys.stderr)
        return 2

    stale = []
    for base_id in bases:
        try:
            text = generate_for_base(base_id)
        except (pmc.ConformanceError, rm.ManifestError) as exc:
            print(f"error: {base_id}: {exc}", file=sys.stderr)
            return 2
        out_path = ROOT / "bases" / base_id / "language-packages.map"
        if args.check:
            current = out_path.read_text(encoding="utf-8") if out_path.is_file() else ""
            # The header's "Written by ... on <timestamp>" line always differs
            # between two real runs even when nothing else does; --check compares
            # everything else, the part that would actually mean a real, wrong
            # answer, not merely a re-run.
            if _drop_timestamp_line(current) != _drop_timestamp_line(text):
                stale.append(base_id)
            continue
        out_path.write_text(text, encoding="utf-8")
        print(f"wrote {out_path.relative_to(ROOT)}", file=sys.stderr)

    if args.check and stale:
        print(f"stale: {', '.join(stale)} — run tools/generate_language_packages.py to regenerate", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
