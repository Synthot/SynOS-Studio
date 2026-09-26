"""tools/build_catalog.py: an AppStream candidate must also be verified
against the real binary package index before it is recorded as available,
and only a suite the engine can actually build a Live image for is ever
considered. Every test here uses an in-memory fake fetcher — no network,
no .build/catalog cache writes, no dependence on the real archives."""
from __future__ import annotations

import gzip
import importlib.util
import sys
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def load_module(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


bc = load_module("build_catalog_under_test", "tools/build_catalog.py")


def appstream_doc(package: str, name: str, *, categories=("Utility",)) -> dict:
    return {"Type": "desktop-application", "Package": package, "Name": name, "Summary": "a summary",
            "Categories": list(categories), "ID": f"{package}.desktop"}


def gz_appstream(*docs: dict) -> bytes:
    text = "---\n".join(yaml.safe_dump(d) for d in docs)
    return gzip.compress(text.encode())


def gz_packages(*names: str) -> bytes:
    return gzip.compress("\n\n".join(f"Package: {n}" for n in names).encode())


class FakeFetcher:
    """AppStream (dep11) urls are served from `appstream`; every other url
    (the real Packages.gz the binary check reads) is served from
    `packages`, keyed by base name substring for convenience since the
    fixture bases only ever have one suite each here. `broken` names
    Packages.gz urls that should raise, standing in for an unreachable
    mirror."""

    def __init__(self, appstream: dict[str, bytes], packages: dict[str, bytes], broken: set[str] | None = None) -> None:
        self.appstream = appstream
        self.packages = packages
        self.broken = broken or set()
        self.calls: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.calls.append(url)
        if url in self.broken:
            raise ConnectionError(f"could not reach {url}")
        if "dep11" in url:
            return self.appstream.get(url, gz_appstream())
        return self.packages.get(url, gz_packages())


class VerifiedAgainstBinaryIndexTests(unittest.TestCase):
    def test_a_candidate_missing_from_the_binary_index_is_never_claimed(self) -> None:
        # AppStream says both exist on ubuntu:noble; the real archive only
        # actually has one of them.
        appstream_url = "http://archive.ubuntu.com/ubuntu/dists/noble/main/dep11/Components-amd64.yml.gz"
        appstream = {appstream_url: gz_appstream(appstream_doc("real-pkg", "Real"), appstream_doc("ghost-pkg", "Ghost"))}
        binary_url = "http://archive.ubuntu.com/ubuntu/dists/noble/main/binary-amd64/Packages.gz"
        packages = {binary_url: gz_packages("real-pkg")}
        fetcher = FakeFetcher(appstream, packages)
        data, warnings = bc.collect([("ubuntu", "noble")], components={"ubuntu": ("main",)}, fetcher=fetcher)
        self.assertEqual([], warnings)
        names = {p["package"] for p in data["packages"]}
        self.assertIn("real-pkg", names)
        self.assertNotIn("ghost-pkg", names, "AppStream metadata alone must never be enough to claim availability")

    def test_a_package_can_be_claimed_on_one_suite_and_not_another(self) -> None:
        ubuntu_appstream = "http://archive.ubuntu.com/ubuntu/dists/noble/main/dep11/Components-amd64.yml.gz"
        debian_appstream = "http://deb.debian.org/debian/dists/trixie/main/dep11/Components-amd64.yml.gz"
        appstream = {ubuntu_appstream: gz_appstream(appstream_doc("cross-pkg", "Cross")),
                     debian_appstream: gz_appstream(appstream_doc("cross-pkg", "Cross"))}
        ubuntu_binary = "http://archive.ubuntu.com/ubuntu/dists/noble/main/binary-amd64/Packages.gz"
        debian_binary = "http://deb.debian.org/debian/dists/trixie/main/binary-amd64/Packages.gz"
        packages = {ubuntu_binary: gz_packages("cross-pkg")}  # absent from debian's real index
        fetcher = FakeFetcher(appstream, packages)
        data, warnings = bc.collect([("ubuntu", "noble"), ("debian", "trixie")],
                                    components={"ubuntu": ("main",), "debian": ("main",)}, fetcher=fetcher)
        self.assertEqual([], warnings)
        entry = next(p for p in data["packages"] if p["package"] == "cross-pkg")
        self.assertEqual({"ubuntu": ["noble"]}, entry["available"])

    def test_one_fetch_per_binary_index_no_matter_how_many_candidates_share_it(self) -> None:
        appstream_url = "http://archive.ubuntu.com/ubuntu/dists/noble/main/dep11/Components-amd64.yml.gz"
        docs = [appstream_doc(f"pkg-{i}", f"Pkg {i}") for i in range(30)]
        appstream = {appstream_url: gz_appstream(*docs)}
        binary_url = "http://archive.ubuntu.com/ubuntu/dists/noble/main/binary-amd64/Packages.gz"
        packages = {binary_url: gz_packages(*[f"pkg-{i}" for i in range(30)])}
        fetcher = FakeFetcher(appstream, packages)
        bc.collect([("ubuntu", "noble")], components={"ubuntu": ("main",)}, fetcher=fetcher)
        self.assertEqual(1, fetcher.calls.count(binary_url), "the binary index must be fetched exactly once, not once per candidate")


class UnverifiableSuiteTests(unittest.TestCase):
    def test_a_suite_whose_binary_index_cannot_be_fetched_claims_nothing_and_warns(self) -> None:
        appstream_url = "http://archive.ubuntu.com/ubuntu/dists/noble/main/dep11/Components-amd64.yml.gz"
        appstream = {appstream_url: gz_appstream(appstream_doc("some-pkg", "Some"))}
        binary_url = "http://archive.ubuntu.com/ubuntu/dists/noble/main/binary-amd64/Packages.gz"
        fetcher = FakeFetcher(appstream, {}, broken={binary_url})
        data, warnings = bc.collect([("ubuntu", "noble")], components={"ubuntu": ("main",)}, fetcher=fetcher)
        self.assertEqual([], data["packages"], "an unverifiable suite must never fall back to trusting AppStream alone")
        self.assertEqual(1, len(warnings))
        self.assertIn("ubuntu:noble", warnings[0])


class LiveCapableSuitesOnlyTests(unittest.TestCase):
    def test_default_suites_never_includes_jammy(self) -> None:
        suites = bc.default_suites()
        self.assertNotIn(("ubuntu", "jammy"), suites)
        self.assertIn(("ubuntu", "noble"), suites)
        self.assertIn(("ubuntu", "resolute"), suites)
        self.assertIn(("debian", "trixie"), suites)

    def test_validator_refuses_a_suite_the_engine_cannot_build(self) -> None:
        refused = bc._validate_suites_are_live_capable([("ubuntu", "jammy"), ("ubuntu", "noble")])
        self.assertEqual(["ubuntu:jammy"], refused)

    def test_main_refuses_an_explicit_unbuildable_suite_without_writing_anything(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "catalog.apps.yml"
            rc = bc.main(["--suites", "ubuntu:jammy", "--output", str(output)])
            self.assertEqual(2, rc)
            self.assertFalse(output.exists())


class OutputPathSurvivesOutsideRepoTests(unittest.TestCase):
    """Item 184: --output outside this checkout used to crash on the final
    summary print (args.output.relative_to(ROOT) raising ValueError) after
    the file had already been written. collect() is monkeypatched with a
    fake fetcher so this never touches the network."""

    def test_an_absolute_output_path_outside_the_repo_does_not_crash(self) -> None:
        import tempfile
        appstream = {}  # every dep11 url serves an empty index; nothing to verify
        fetcher = FakeFetcher(appstream, {})
        original_collect = bc.collect
        bc.collect = lambda suites, *a, **k: original_collect(suites, *a, fetcher=fetcher, **k)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                output = Path(tmp) / "outside-the-repo.yml"
                rc = bc.main(["--suites", "debian:trixie", "--output", str(output)])
                self.assertEqual(0, rc)
                self.assertTrue(output.is_file())
        finally:
            bc.collect = original_collect


if __name__ == "__main__":
    unittest.main()
