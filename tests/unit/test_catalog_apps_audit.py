"""tools/catalog_apps_audit.py: does every profiles/catalog.apps.yml entry's
`package` name actually exist in the real archive its own `available`
mapping claims? Every test here uses an in-memory fake fetcher and a
synthetic engine matrix/base_env — never a real network connection and
never the real bases/ adapters, so the suite is hermetic and does not drift
if the real archives or SUPPORTED_SUITES change."""
from __future__ import annotations

import gzip
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load_module(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


caa = load_module("catalog_apps_audit_under_test", "tools/catalog_apps_audit.py")


# A small, self-contained matrix/base_env pair, independent of the real
# bases/ directory: two bases, one suite each, with distinct COMPONENTS so a
# fetch to the wrong base's URL would show up as a distinct call.
MATRIX = {"ubuntu": ["noble"], "debian": ["trixie"]}
BASE_ENVS = {
    "ubuntu": {"APT_MIRROR": "http://archive.ubuntu.com/ubuntu/", "COMPONENTS": "main universe"},
    "debian": {"APT_MIRROR": "http://deb.debian.org/debian/", "COMPONENTS": "main contrib"},
}


def gz_packages(*names: str) -> bytes:
    return gzip.compress("\n\n".join(f"Package: {n}" for n in names).encode())


def entry(entry_id: str, package: str, available: dict) -> dict:
    return {"id": entry_id, "package": package, "title": entry_id, "available": available}


class FakeFetcher:
    """Records every URL asked for; `broken` names URLs that raise instead
    of returning bytes, standing in for "offline" / "mirror will not
    answer"."""

    def __init__(self, by_url: dict[str, bytes] | None = None, broken: set[str] | None = None) -> None:
        self.by_url = by_url or {}
        self.broken = broken or set()
        self.calls: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.calls.append(url)
        if url in self.broken:
            raise ConnectionError(f"could not reach {url}")
        return self.by_url.get(url, gzip.compress(b""))


class PresentMissingCheckedTests(unittest.TestCase):
    def test_present_package_is_not_reported_missing(self) -> None:
        e = entry("a", "present-pkg", {"ubuntu": ["noble"]})
        urls = caa.catalog_conformance._component_urls(BASE_ENVS["ubuntu"], "noble", "amd64")
        fetcher = FakeFetcher({urls[0]: gz_packages("present-pkg")})
        report = caa.build_report([e], MATRIX, BASE_ENVS, fetcher=fetcher)
        self.assertEqual([], report["missing"])
        self.assertEqual([], report["could_not_check"])
        self.assertEqual(0, caa.exit_code(report))

    def test_absent_package_is_reported_missing_with_its_base_and_suite(self) -> None:
        e = entry("b", "ghost-pkg", {"ubuntu": ["noble"]})
        fetcher = FakeFetcher()  # every url decompresses to an empty index
        report = caa.build_report([e], MATRIX, BASE_ENVS, fetcher=fetcher)
        self.assertEqual(1, len(report["missing"]))
        record = report["missing"][0]
        self.assertEqual("b", record["id"])
        self.assertEqual([{"base": "ubuntu", "suite": "noble"}], record["missing_from"])
        self.assertEqual([], record["present_on"])
        self.assertTrue(record["everywhere"])
        self.assertEqual(1, caa.exit_code(report))

    def test_missing_on_one_base_present_on_the_other_is_reported_as_both(self) -> None:
        e = entry("c", "cross-base-pkg", {"ubuntu": ["noble"], "debian": ["trixie"]})
        ubuntu_urls = caa.catalog_conformance._component_urls(BASE_ENVS["ubuntu"], "noble", "amd64")
        debian_urls = caa.catalog_conformance._component_urls(BASE_ENVS["debian"], "trixie", "amd64")
        by_url = {u: gz_packages() for u in ubuntu_urls}          # absent on ubuntu
        by_url.update({u: gz_packages("cross-base-pkg") for u in debian_urls})  # present on debian
        fetcher = FakeFetcher(by_url)
        report = caa.build_report([e], MATRIX, BASE_ENVS, fetcher=fetcher)
        self.assertEqual(1, len(report["missing"]))
        record = report["missing"][0]
        self.assertEqual([{"base": "ubuntu", "suite": "noble"}], record["missing_from"])
        self.assertEqual([{"base": "debian", "suite": "trixie"}], record["present_on"])
        self.assertFalse(record["everywhere"], "present on at least one claimed base/suite, not gone everywhere")

    def test_checks_total_counts_every_claimed_base_suite_pair(self) -> None:
        entries = [
            entry("a", "pkg-a", {"ubuntu": ["noble"]}),
            entry("b", "pkg-b", {"ubuntu": ["noble"], "debian": ["trixie"]}),
        ]
        fetcher = FakeFetcher()
        report = caa.build_report(entries, MATRIX, BASE_ENVS, fetcher=fetcher)
        self.assertEqual(3, report["checks_total"])  # a:ubuntu, b:ubuntu, b:debian


class CouldNotCheckTests(unittest.TestCase):
    def test_a_broken_index_is_could_not_check_not_missing(self) -> None:
        e = entry("d", "any-pkg", {"ubuntu": ["noble"]})
        urls = caa.catalog_conformance._component_urls(BASE_ENVS["ubuntu"], "noble", "amd64")
        fetcher = FakeFetcher(broken={urls[0]})
        report = caa.build_report([e], MATRIX, BASE_ENVS, fetcher=fetcher)
        self.assertEqual([], report["missing"], "a network failure must never be reported as a missing package")
        self.assertEqual(1, len(report["could_not_check"]))
        self.assertEqual("ubuntu", report["could_not_check"][0]["base"])
        self.assertEqual("noble", report["could_not_check"][0]["suite"])
        self.assertEqual(1, report["could_not_check"][0]["entries_affected"])
        self.assertEqual(2, caa.exit_code(report), "could-not-check alone must exit differently from missing")

    def test_one_broken_component_taints_the_whole_base_suite_not_just_that_component(self) -> None:
        # ubuntu:noble has two components (main, universe); only one is broken,
        # but a package that lives in the broken component would otherwise
        # look absent, so the whole (base, suite) is unverified.
        e = entry("e", "in-the-broken-component", {"ubuntu": ["noble"]})
        urls = caa.catalog_conformance._component_urls(BASE_ENVS["ubuntu"], "noble", "amd64")
        fetcher = FakeFetcher({urls[1]: gz_packages("something-unrelated")}, broken={urls[0]})
        report = caa.build_report([e], MATRIX, BASE_ENVS, fetcher=fetcher)
        self.assertEqual([], report["missing"])
        self.assertEqual(1, len(report["could_not_check"]))

    def test_corrupt_gzip_is_also_could_not_check(self) -> None:
        e = entry("f", "any-pkg", {"debian": ["trixie"]})
        urls = caa.catalog_conformance._component_urls(BASE_ENVS["debian"], "trixie", "amd64")
        fetcher = FakeFetcher({u: b"not actually gzip" for u in urls})
        report = caa.build_report([e], MATRIX, BASE_ENVS, fetcher=fetcher)
        self.assertEqual([], report["missing"])
        self.assertEqual(1, len(report["could_not_check"]))


class StaleSuiteClaimTests(unittest.TestCase):
    def test_a_suite_the_engine_no_longer_supports_is_neither_missing_nor_checked(self) -> None:
        e = entry("g", "old-claim-pkg", {"ubuntu": ["jammy"]})  # not in MATRIX["ubuntu"]
        fetcher = FakeFetcher()
        report = caa.build_report([e], MATRIX, BASE_ENVS, fetcher=fetcher)
        self.assertEqual([], report["missing"])
        self.assertEqual([], report["could_not_check"])
        self.assertEqual(0, report["checks_total"])
        self.assertEqual([], fetcher.calls, "an unsupported suite must never trigger a fetch")
        self.assertEqual(1, len(report["stale_suite_claims"]))
        self.assertEqual("g", report["stale_suite_claims"][0]["id"])

    def test_an_unknown_base_is_also_a_stale_claim(self) -> None:
        e = entry("h", "any-pkg", {"fedora": ["42"]})
        report = caa.build_report([e], MATRIX, BASE_ENVS, fetcher=FakeFetcher())
        self.assertEqual(1, len(report["stale_suite_claims"]))
        self.assertEqual("fedora", report["stale_suite_claims"][0]["base"])


class CachingTests(unittest.TestCase):
    def test_one_fetch_per_base_suite_component_no_matter_how_many_entries_share_it(self) -> None:
        entries = [entry(str(i), f"pkg-{i}", {"ubuntu": ["noble"]}) for i in range(50)]
        fetcher = FakeFetcher()
        caa.build_report(entries, MATRIX, BASE_ENVS, fetcher=fetcher)
        expected_urls = caa.catalog_conformance._component_urls(BASE_ENVS["ubuntu"], "noble", "amd64")
        self.assertEqual(len(expected_urls), len(fetcher.calls),
                         "50 entries sharing one (base, suite) must fetch its components exactly once, "
                         "not once per entry")
        self.assertEqual(set(expected_urls), set(fetcher.calls))

    def test_a_shared_cache_across_two_build_report_calls_is_not_refetched(self) -> None:
        cache: dict = {}
        e1 = entry("a", "pkg-a", {"ubuntu": ["noble"]})
        e2 = entry("b", "pkg-b", {"ubuntu": ["noble"]})
        fetcher = FakeFetcher()
        caa.build_report([e1], MATRIX, BASE_ENVS, fetcher=fetcher, cache=cache)
        first_call_count = len(fetcher.calls)
        caa.build_report([e2], MATRIX, BASE_ENVS, fetcher=fetcher, cache=cache)
        self.assertEqual(first_call_count, len(fetcher.calls), "a cache handed back in must not be refetched")


class ExitCodeTests(unittest.TestCase):
    def test_clean_run_exits_zero(self) -> None:
        e = entry("a", "present-pkg", {"ubuntu": ["noble"]})
        urls = caa.catalog_conformance._component_urls(BASE_ENVS["ubuntu"], "noble", "amd64")
        report = caa.build_report([e], MATRIX, BASE_ENVS, fetcher=FakeFetcher({urls[0]: gz_packages("present-pkg")}))
        self.assertEqual(0, caa.exit_code(report))

    def test_missing_beats_could_not_check_for_exit_code(self) -> None:
        # One entry genuinely missing, a *different* (base, suite) unreachable:
        # the gate must fire (1), the incompleteness must still be visible in
        # the report, but exit code communicates the worse problem.
        missing_e = entry("a", "ghost-pkg", {"ubuntu": ["noble"]})
        unverifiable_e = entry("b", "any-pkg", {"debian": ["trixie"]})
        debian_urls = caa.catalog_conformance._component_urls(BASE_ENVS["debian"], "trixie", "amd64")
        fetcher = FakeFetcher(broken=set(debian_urls))
        report = caa.build_report([missing_e, unverifiable_e], MATRIX, BASE_ENVS, fetcher=fetcher)
        self.assertEqual(1, len(report["missing"]))
        self.assertEqual(1, len(report["could_not_check"]))
        self.assertEqual(1, caa.exit_code(report))


class SummaryTextTests(unittest.TestCase):
    def test_summary_names_the_missing_entry_and_its_bases(self) -> None:
        e = entry("web-thing", "ghost-pkg", {"ubuntu": ["noble"]})
        report = caa.build_report([e], MATRIX, BASE_ENVS, fetcher=FakeFetcher())
        text = caa.summary_text(report)
        self.assertIn("web-thing", text)
        self.assertIn("ubuntu:noble", text)
        self.assertIn("MISSING", text)

    def test_summary_distinguishes_could_not_check_from_missing_in_wording(self) -> None:
        e = entry("a", "any-pkg", {"ubuntu": ["noble"]})
        urls = caa.catalog_conformance._component_urls(BASE_ENVS["ubuntu"], "noble", "amd64")
        report = caa.build_report([e], MATRIX, BASE_ENVS, fetcher=FakeFetcher(broken={urls[0]}))
        text = caa.summary_text(report)
        self.assertIn("COULD NOT CHECK", text)
        self.assertNotIn("MISSING:", text)


class RealCatalogSmokeTest(unittest.TestCase):
    """No network, no real bases/ live.map assumptions baked into the fake
    matrix above — just proves load_catalog_entries()/engine_matrix() read
    this actual checkout's real files without raising."""

    def test_engine_matrix_covers_ubuntu_and_debian_and_never_the_template(self) -> None:
        matrix, envs = caa.engine_matrix()
        self.assertIn("ubuntu", matrix)
        self.assertIn("debian", matrix)
        self.assertNotIn("_template", matrix)
        self.assertGreater(len(matrix["ubuntu"]), 0)
        self.assertIn("ubuntu", envs)

    def test_load_catalog_entries_reads_the_real_generated_file(self) -> None:
        entries = caa.load_catalog_entries(ROOT / "profiles" / "catalog.apps.yml")
        self.assertGreater(len(entries), 1500)
        self.assertIn("package", entries[0])
        self.assertIn("available", entries[0])


if __name__ == "__main__":
    unittest.main()
