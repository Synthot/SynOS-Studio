"""tools/packages_map_conformance.py: every concrete name in bases/*/
packages.map, checked against a real archive's full apt view. Hermetic: a
fake fetcher stands in for the network (same pattern
tests/unit/test_catalog_conformance.py uses for its own fetcher), so this
suite never touches a real archive."""
from __future__ import annotations

import gzip
import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("packages_map_conformance", ROOT / "tools" / "packages_map_conformance.py")
pmc = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(pmc)


def _gz(text: str) -> bytes:
    return gzip.compress(text.encode("utf-8"))


class FetchIndexTests(unittest.TestCase):
    def test_falls_back_from_gz_to_xz_when_gz_is_absent(self) -> None:
        import lzma
        calls: list[str] = []

        def fetcher(url: str) -> bytes:
            calls.append(url)
            if url.endswith(".gz"):
                raise FileNotFoundError("404")
            return lzma.compress(b"Package: foo\n")

        raw = pmc.fetch_index("http://example/Packages", fetcher=fetcher)
        self.assertEqual(b"Package: foo\n", raw)
        self.assertEqual(["http://example/Packages.gz", "http://example/Packages.xz"], calls)

    def test_returns_none_when_neither_extension_exists(self) -> None:
        def fetcher(url: str) -> bytes:
            raise FileNotFoundError("404")

        self.assertIsNone(pmc.fetch_index("http://example/Packages", fetcher=fetcher))

    def test_prefers_gz_without_trying_xz(self) -> None:
        calls: list[str] = []

        def fetcher(url: str) -> bytes:
            calls.append(url)
            return gzip.compress(b"Package: foo\n")

        pmc.fetch_index("http://example/Packages", fetcher=fetcher)
        self.assertEqual(["http://example/Packages.gz"], calls)

    def test_a_decompress_failure_on_a_fetched_extension_is_not_silently_swallowed(self) -> None:
        def fetcher(url: str) -> bytes:
            return b"not actually gzip"

        with self.assertRaises(pmc.ConformanceError):
            pmc.fetch_index("http://example/Packages", fetcher=fetcher)


class ParsePackagesIndexTests(unittest.TestCase):
    def test_captures_names_and_provides(self) -> None:
        text = (
            "Package: real-pkg\n"
            "Version: 1.0\n"
            "Provides: virtual-a, virtual-b (= 1.0)\n"
            "\n"
            "Package: other-pkg\n"
            "Provides: virtual-a\n"
        )
        names: set[str] = set()
        provides: dict[str, set[str]] = {}
        pmc.parse_packages_index(text, names, provides)
        self.assertEqual({"real-pkg", "other-pkg"}, names)
        self.assertEqual({"real-pkg", "other-pkg"}, provides["virtual-a"])
        self.assertEqual({"real-pkg"}, provides["virtual-b"])

    def test_a_provides_line_before_any_package_line_is_ignored(self) -> None:
        names: set[str] = set()
        provides: dict[str, set[str]] = {}
        pmc.parse_packages_index("Provides: orphan\n", names, provides)
        self.assertEqual({}, provides)


class PocketUrlsTests(unittest.TestCase):
    def test_covers_suite_updates_backports_and_security_from_the_right_mirrors(self) -> None:
        base_env = {"APT_MIRROR": "http://archive.example/ubuntu/", "SECURITY_MIRROR": "http://security.example/ubuntu/",
                    "COMPONENTS": "main universe"}
        urls = {url for _component, url in pmc.pocket_urls(base_env, "noble", "amd64")}
        self.assertIn("http://archive.example/ubuntu/dists/noble/main/binary-amd64/Packages", urls)
        self.assertIn("http://archive.example/ubuntu/dists/noble-updates/universe/binary-amd64/Packages", urls)
        self.assertIn("http://archive.example/ubuntu/dists/noble-backports/main/binary-amd64/Packages", urls)
        self.assertIn("http://security.example/ubuntu/dists/noble-security/universe/binary-amd64/Packages", urls)
        self.assertEqual(8, len(urls))  # 2 components x 4 pockets

    def test_security_mirror_defaults_to_the_apt_mirror_when_unset(self) -> None:
        base_env = {"APT_MIRROR": "http://archive.example/debian/", "COMPONENTS": "main"}
        urls = {url for _component, url in pmc.pocket_urls(base_env, "trixie", "amd64")}
        self.assertIn("http://archive.example/debian/dists/trixie-security/main/binary-amd64/Packages", urls)


class ExpandTests(unittest.TestCase):
    def test_lang_placeholder_expands_across_every_code(self) -> None:
        self.assertEqual(["hunspell-en", "hunspell-fr"], pmc.expand("hunspell-${LANG}", ["en", "fr"], "amd64"))

    def test_arch_placeholder_is_substituted_once_no_lang_present(self) -> None:
        self.assertEqual(["linux-image-arm64"], pmc.expand("linux-image-${ARCH}", ["en"], "arm64"))

    def test_a_plain_template_is_returned_unchanged(self) -> None:
        self.assertEqual(["git"], pmc.expand("git", ["en", "fr"], "amd64"))


class ArchesForTests(unittest.TestCase):
    def test_only_amd64_when_nothing_names_arch(self) -> None:
        self.assertEqual(["amd64"], pmc.arches_for(["python3-torch", "llama.cpp"]))

    def test_adds_arm64_only_when_a_template_names_it(self) -> None:
        self.assertEqual(list(pmc.ARCHES), pmc.arches_for(["linux-image-${ARCH}"]))


class LangCodesTests(unittest.TestCase):
    def test_includes_en_and_a_known_region_locale(self) -> None:
        codes = pmc.lang_codes(ROOT)
        self.assertIn("en", codes)
        self.assertIn("fr", codes)  # regions/fr.yml
        self.assertEqual(codes, sorted(set(codes)))  # deduplicated and sorted


class CheckBaseTests(unittest.TestCase):
    """check_base against a small, fully fake base — never the real bases/,
    so this exercises the suite-override lookup order and the
    missing/provides-only classification without any network."""

    def _make_base(self, directory: Path, *, packages_map: str, supported_suites: str = "sid",
                   live_map: str | None = None) -> None:
        base_dir = directory / "bases" / "fake"
        base_dir.mkdir(parents=True)
        (base_dir / "base.env").write_text(
            f"BASE_ID=fake\nAPT_MIRROR=http://archive.example/fake/\n"
            f"SECURITY_MIRROR=http://security.example/fake/\nCOMPONENTS=main\n"
            f"SUPPORTED_SUITES=\"{supported_suites}\"\n", encoding="utf-8")
        (base_dir / "packages.map").write_text(packages_map, encoding="utf-8")
        if live_map is not None:
            (base_dir / "live.map").write_text(live_map, encoding="utf-8")
        (directory / "regions").mkdir(exist_ok=True)

    def test_a_real_package_is_never_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_base(root, packages_map="widget = real-pkg\n")

            def fetcher(url: str) -> bytes:
                if "binary-amd64/Packages.gz" in url and "/sid/" in url:
                    return _gz("Package: real-pkg\n")
                raise FileNotFoundError("404")

            result = pmc.check_base("fake", root, fetcher=fetcher)
            self.assertEqual([], result["missing"])
            self.assertEqual([], result["provides_only"])
            self.assertEqual(1, result["packages_checked"])

    def test_a_genuinely_absent_package_is_reported_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_base(root, packages_map="widget = ghost-pkg\n")

            def fetcher(url: str) -> bytes:
                if "binary-amd64/Packages.gz" in url and "/sid/" in url:
                    return _gz("Package: something-else\n")
                raise FileNotFoundError("404")

            result = pmc.check_base("fake", root, fetcher=fetcher)
            self.assertEqual(1, len(result["missing"]))
            self.assertEqual("ghost-pkg", result["missing"][0]["package"])
            self.assertEqual("widget", result["missing"][0]["role"])

    def test_a_package_real_only_via_provides_is_reported_separately_not_as_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_base(root, packages_map="widget = renamed-pkg\n")

            def fetcher(url: str) -> bytes:
                if "binary-amd64/Packages.gz" in url and "/sid/" in url:
                    return _gz("Package: real-name\nProvides: renamed-pkg\n")
                raise FileNotFoundError("404")

            result = pmc.check_base("fake", root, fetcher=fetcher)
            self.assertEqual([], result["missing"])
            self.assertEqual(1, len(result["provides_only"]))
            self.assertEqual(["real-name"], result["provides_only"][0]["provided_by"])

    def test_an_unavailable_role_is_never_fetched_against(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_base(root, packages_map="widget = unavailable: no such role here\n")

            def fetcher(url: str) -> bytes:
                raise AssertionError(f"must never fetch for an Unavailable role: {url}")

            result = pmc.check_base("fake", root, fetcher=fetcher)
            self.assertEqual([], result["missing"])
            self.assertEqual(0, result["packages_checked"])

    def test_a_per_suite_override_is_checked_only_against_its_own_suite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_base(
                root, supported_suites="alpha beta",
                packages_map="widget          = alpha-name\nwidget@beta     = beta-name\n")

            def fetcher(url: str) -> bytes:
                if "/alpha/" in url and "binary-amd64/Packages.gz" in url:
                    return _gz("Package: alpha-name\n")
                if "/beta/" in url and "binary-amd64/Packages.gz" in url:
                    return _gz("Package: beta-name\n")
                raise FileNotFoundError("404")

            result = pmc.check_base("fake", root, fetcher=fetcher)
            self.assertEqual([], result["missing"], result["missing"])
            # 1 name checked on alpha (bare, no override there) + 1 on beta (the override) = 2
            self.assertEqual(2, result["packages_checked"])

    def test_a_suite_live_map_marks_unavailable_is_never_checked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_base(root, supported_suites="dead alive", packages_map="widget = real-pkg\n",
                            live_map="dead = unavailable: no live boot support\n")

            def fetcher(url: str) -> bytes:
                if "/dead/" in url:
                    raise AssertionError(f"must never fetch a suite live.map marks unavailable: {url}")
                if "/alive/" in url and "binary-amd64/Packages.gz" in url:
                    return _gz("Package: real-pkg\n")
                raise FileNotFoundError("404")

            result = pmc.check_base("fake", root, fetcher=fetcher)
            self.assertEqual(["alive"], result["suites"])
            self.assertEqual([], result["missing"])

    def test_an_unknown_suite_override_is_refused_like_live_map_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_base(root, packages_map="widget@nosuchsuite = real-pkg\n")
            with self.assertRaises(pmc.rm.ManifestError):
                pmc.check_base("fake", root, fetcher=lambda url: (_ for _ in ()).throw(FileNotFoundError()))


class SummaryTextTests(unittest.TestCase):
    def test_reports_counts_missing_and_provides_only_lines(self) -> None:
        report = {"bases": [{"base": "fake", "suites": ["sid"], "packages_checked": 3,
                             "missing": [{"role": "widget", "package": "ghost", "suite": "sid", "arch": "amd64"}],
                             "provides_only": [{"role": "widget", "package": "renamed", "suite": "sid", "arch": "amd64",
                                               "provided_by": ["real-name"]}]}]}
        text = pmc.summary_text(report)
        self.assertIn("3 name(s) checked", text)
        self.assertIn("MISSING widget: 'ghost'", text)
        self.assertIn("provides-only widget: 'renamed'", text)
        self.assertIn("real-name", text)


class MainCliTests(unittest.TestCase):
    def test_an_unknown_base_is_refused_before_any_network_use(self) -> None:
        code = pmc.main(["--base", "not-a-real-base"])
        self.assertEqual(2, code)


if __name__ == "__main__":
    unittest.main()
