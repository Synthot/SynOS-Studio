"""tools/generate_language_packages.py: writes bases/<base>/language-packages.map,
the real, archive-verified concrete package for every ${LANG} template in
bases/<base>/packages.map and every language this engine can be asked for.
Hermetic: a fake fetcher stands in for the network, same pattern
tests/unit/test_packages_map_conformance.py uses for its own."""
from __future__ import annotations

import gzip
import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


glp = _load("generate_language_packages", "tools/generate_language_packages.py")
# glp.py itself does `import packages_map_conformance as pmc` (a plain import,
# not importlib) — reused here, rather than loading a second copy of that
# module under a different name, so pmc.ConformanceError here and the one
# glp.py's own code raises are the exact same class object (assertRaises
# checks identity, and two importlib.util loads of the same file are not
# the same class even with an identical name).
pmc = glp.pmc


def _gz(text: str) -> bytes:
    return gzip.compress(text.encode("utf-8"))


def _make_region(root: Path, region_id: str, locale: str) -> None:
    (root / "regions").mkdir(exist_ok=True)
    (root / "regions" / f"{region_id}.yml").write_text(
        f"id: {region_id}\nname: {region_id.upper()}\nlocales: [{locale}]\nkeyboard: us\ntimezone: UTC\n",
        encoding="utf-8")


class NamesakeRegionTests(unittest.TestCase):
    def test_a_region_whose_id_equals_the_language_code_gives_its_country(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_region(root, "de", "de_DE.UTF-8")
            self.assertEqual("de", glp.namesake_region_country("de", root))

    def test_no_such_region_gives_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(glp.namesake_region_country("xx", Path(tmp)))

    def test_a_region_that_exists_but_does_not_actually_carry_this_language_gives_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_region(root, "pt", "fr_FR.UTF-8")  # region id matches, locale does not
            self.assertIsNone(glp.namesake_region_country("pt", root))


class ResolveRegionalVariantTests(unittest.TestCase):
    def test_the_cjk_script_code_override_replaces_the_whole_code_not_a_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            names = {"libreoffice-l10n-zh-cn"}
            got = glp.resolve_regional_variant(names, {}, "libreoffice-l10n-", "zh-hans", Path(tmp))
            self.assertEqual("libreoffice-l10n-zh-cn", got[0])

    def test_the_home_region_country_is_preferred_when_its_variant_is_real(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _make_region(root, "de", "de_DE.UTF-8")
            names = {"hunspell-de-at", "hunspell-de-de", "hunspell-de-ch"}
            got = glp.resolve_regional_variant(names, {}, "hunspell-", "de", root)
            self.assertEqual("hunspell-de-de", got[0])

    def test_english_prefers_the_engines_own_baseline_locale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            names = {"hunspell-en-gb", "hunspell-en-us", "hunspell-en-au"}
            got = glp.resolve_regional_variant(names, {}, "hunspell-", "en", Path(tmp))
            self.assertEqual("hunspell-en-us", got[0])

    def test_english_with_no_us_variant_at_all_is_not_guessed(self) -> None:
        """libreoffice-l10n-en-us never exists (LibreOffice ships US English
        built in) — only enhancement packs like en-gb do. Must not fall through
        to picking one of those; that is a source-language gap, not this
        function's job to resolve (resolve_template calls it unavailable)."""
        with tempfile.TemporaryDirectory() as tmp:
            names = {"libreoffice-l10n-en-gb", "libreoffice-l10n-en-za"}
            got = glp.resolve_regional_variant(names, {}, "libreoffice-l10n-", "en", Path(tmp))
            self.assertIsNone(got)

    def test_a_single_real_variant_with_no_other_signal_is_used(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            names = {"firefox-esr-l10n-hi-in"}
            got = glp.resolve_regional_variant(names, {}, "firefox-esr-l10n-", "hi", Path(tmp))
            self.assertEqual("firefox-esr-l10n-hi-in", got[0])

    def test_multiple_variants_with_no_signal_at_all_are_not_guessed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            names = {"widget-xx-aa", "widget-xx-bb"}
            got = glp.resolve_regional_variant(names, {}, "widget-", "xx", Path(tmp))
            self.assertIsNone(got)


class ResolveOneTests(unittest.TestCase):
    def test_an_exact_match_wins_immediately(self) -> None:
        got = glp.resolve_one({"hunspell-fr"}, {}, "hunspell-", "fr", Path("."))
        self.assertEqual(("hunspell-fr", "exact match in the archive's own Package: list"), got)

    def test_a_unique_provides_resolves_to_the_real_package_not_the_virtual_name(self) -> None:
        got = glp.resolve_one(set(), {"hunspell-et": {"myspell-et"}}, "hunspell-", "et", Path("."))
        self.assertEqual("myspell-et", got[0])

    def test_an_ambiguous_provides_with_two_real_providers_is_not_guessed(self) -> None:
        got = glp.resolve_one(set(), {"hunspell-zz": {"pkg-a", "pkg-b"}}, "hunspell-", "zz", Path("."))
        self.assertIsNone(got)

    def test_nothing_at_all_gives_none(self) -> None:
        got = glp.resolve_one(set(), {}, "hunspell-", "zz", Path("."))
        self.assertIsNone(got)


class ResolveTemplateTests(unittest.TestCase):
    def test_hunspell_falls_back_to_the_myspell_family_mechanically(self) -> None:
        """A generic prefix swap, not a per-language hardcoded name: any
        hunspell-${LANG} template retries under myspell- when the hunspell
        family has nothing, real for 'ga' and 'nb' on the real archive."""
        names = {"myspell-nb"}
        packages, how = glp.resolve_template(names, {}, "hunspell-${LANG}", "nb", Path("."))
        self.assertEqual(["myspell-nb"], packages)
        self.assertIn("myspell", how)

    def test_the_verified_technology_override_is_used_when_named_packages_are_real(self) -> None:
        names = {"voikko-fi", "libenchant-2-voikko", "libreoffice-voikko"}
        packages, how = glp.resolve_template(names, {}, "hunspell-${LANG}", "fi", Path("."))
        self.assertEqual(["voikko-fi", "libenchant-2-voikko", "libreoffice-voikko"], packages)
        self.assertIn("Voikko", how)

    def test_a_stale_technology_override_raises_rather_than_writing_a_wrong_answer(self) -> None:
        with self.assertRaises(pmc.ConformanceError):
            glp.resolve_template(set(), {}, "hunspell-${LANG}", "fi", Path("."))

    def test_a_language_with_nothing_anywhere_is_unavailable_with_a_reason(self) -> None:
        packages, reason = glp.resolve_template(set(), {}, "hunspell-${LANG}", "ja", Path("."))
        self.assertIsNone(packages)
        self.assertIn("ja", reason)

    def test_a_non_hunspell_template_with_nothing_is_unavailable_without_a_myspell_retry(self) -> None:
        packages, reason = glp.resolve_template(set(), {}, "libreoffice-l10n-${LANG}", "sw", Path("."))
        self.assertIsNone(packages)
        self.assertIn("libreoffice-l10n-sw", reason)

    def test_a_suffix_after_the_placeholder_is_preserved(self) -> None:
        names = {"language-pack-de", "language-pack-de-base"}  # both ship as a pair, the real archive's own shape
        packages, _ = glp.resolve_template(names, {}, "language-pack-${LANG}-base", "de", Path("."))
        self.assertEqual(["language-pack-de-base"], packages)


class TemplatesInTests(unittest.TestCase):
    def test_reads_every_distinct_lang_template_from_packages_map(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "packages.map"
            path.write_text(
                "office = libreoffice\n"
                "translations = language-pack-${LANG} language-pack-${LANG}-base\n"
                "spellcheck = hunspell-${LANG}\n"
                "gone = unavailable: nothing here\n",
                encoding="utf-8")
            self.assertEqual(["language-pack-${LANG}", "language-pack-${LANG}-base", "hunspell-${LANG}"],
                             glp.templates_in(path))


class GenerateForBaseTests(unittest.TestCase):
    """End-to-end against a small, fully fake base — never the real bases/."""

    def _make_base(self, root: Path, packages_map: str) -> None:
        base_dir = root / "bases" / "fake"
        base_dir.mkdir(parents=True)
        (base_dir / "base.env").write_text(
            "BASE_ID=fake\nAPT_MIRROR=http://archive.example/fake/\n"
            "SECURITY_MIRROR=http://security.example/fake/\nCOMPONENTS=main\n"
            "SUPPORTED_SUITES=\"sid\"\n", encoding="utf-8")
        (base_dir / "packages.map").write_text(packages_map, encoding="utf-8")
        (root / "regions").mkdir(exist_ok=True)

    def test_writes_a_resolved_line_and_an_unavailable_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_base(root, "spellcheck = hunspell-${LANG}\n")

            def fetcher(url: str) -> bytes:
                if "binary-amd64/Packages.gz" in url and "/sid/" in url:
                    return _gz("Package: hunspell-en\n")
                raise FileNotFoundError("404")

            text = glp.generate_for_base("fake", root, fetcher=fetcher)
            self.assertIn("hunspell-${LANG}/en = hunspell-en", text)
            self.assertIn("GENERATED FILE", text)

    def test_an_unhandled_template_prefix_is_refused_rather_than_silently_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_base(root, "widget = something-brand-new-${LANG}\n")
            with self.assertRaises(pmc.ConformanceError):
                glp.generate_for_base("fake", root, fetcher=lambda url: (_ for _ in ()).throw(FileNotFoundError()))

    def test_a_per_suite_difference_is_written_as_two_at_suite_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base_dir = root / "bases" / "fake"
            base_dir.mkdir(parents=True)
            (base_dir / "base.env").write_text(
                "BASE_ID=fake\nAPT_MIRROR=http://archive.example/fake/\n"
                "SECURITY_MIRROR=http://security.example/fake/\nCOMPONENTS=main\n"
                "SUPPORTED_SUITES=\"alpha beta\"\n", encoding="utf-8")
            (base_dir / "packages.map").write_text("translations = language-pack-${LANG}\n", encoding="utf-8")
            (root / "regions").mkdir(exist_ok=True)

            def fetcher(url: str) -> bytes:
                if "binary-amd64/Packages.gz" in url and "/alpha/" in url:
                    return _gz("Package: language-pack-en\n")
                if "binary-amd64/Packages.gz" in url and "/beta/" in url:
                    return _gz("Package: something-else\n")
                raise FileNotFoundError("404")

            text = glp.generate_for_base("fake", root, fetcher=fetcher)
            self.assertIn("language-pack-${LANG}/en@alpha = language-pack-en", text)
            self.assertIn("language-pack-${LANG}/en@beta = unavailable:", text)
            self.assertNotIn("language-pack-${LANG}/en =", text)  # no un-suffixed line when suites disagree


if __name__ == "__main__":
    unittest.main()
