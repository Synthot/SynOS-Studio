"""tools/catalog_apt_solver.py: can apt actually install a catalogued
entry on top of the engine's own floor? Every test here uses a FakeRunner
(no podman, no network) and, where a failure's exact wording matters, the
real text apt-get produced when this was calibrated against a live
container (see the module docstring's exim4-daemon-light/postfix example
for the "conflict" case) — captured as fixtures below, not re-derived from
guesswork."""
from __future__ import annotations

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


cas = load_module("catalog_apt_solver_under_test", "tools/catalog_apt_solver.py")


# ------------------------------------------------------ real apt fixtures
# Captured for real, --no-remove, against a live debian:trixie container
# with the engine's own sources configured (this module's own calibration
# run; see the module docstring). Never hand-written to fit the parser.
REAL_CLEAN_OUTPUT = (
    "Reading package lists...\nBuilding dependency tree...\nReading state information...\n"
    "The following NEW packages will be installed:\n  tintin++\n"
    "0 upgraded, 1 newly installed, 0 to remove and 0 not upgraded.\n"
    "Inst tintin++ (2.02.20-1+b1 Debian:13.7/stable [amd64])\n"
    "Conf tintin++ (2.02.20-1+b1 Debian:13.7/stable [amd64])\n"
)

REAL_CONFLICT_NEEDS_REMOVAL_OUTPUT = (
    "Reading package lists...\nBuilding dependency tree...\nReading state information...\n"
    "The following packages were automatically installed and are no longer required:\n"
    "  libicu76 libtlsrpt0\nUse 'apt autoremove' to remove them.\n"
    "The following additional packages will be installed:\n"
    "  bsd-mailx exim4-base exim4-config libevent-2.1-7t64 libfile-fcntllock-perl\n"
    "  libgdbm-compat4t64 libgdbm6t64 libgnutls-dane0t64 libidn12 liblockfile-bin\n"
    "  liblockfile1 libperl5.40 libunbound8 perl perl-modules-5.40\n"
    "Suggested packages:\n  exim4-doc-html | exim4-doc-info eximon4 file spf-tools-perl swaks gcc\n"
    "The following packages will be REMOVED:\n  postfix\n"
    "The following NEW packages will be installed:\n"
    "  bsd-mailx exim4-base exim4-config exim4-daemon-light libevent-2.1-7t64\n"
    "0 upgraded, 16 newly installed, 1 to remove and 0 not upgraded.\n"
    "E: Packages need to be removed but remove is disabled.\n"
)

# The classic apt unmet-Depends message: stable, documented apt-get wording
# unchanged across every version this engine targets (real archives were
# not observed to produce this against the actual floor — the floor,
# desktop apps aside, is too small to reliably trigger a genuine
# depwait/unsatisfiable case; the message format itself is not in doubt).
UNMET_DEPENDS_OUTPUT = (
    "Reading package lists...\nBuilding dependency tree...\nReading state information...\n"
    "Some packages could not be installed. This may mean that you have\n"
    "requested an impossible situation or if you are using the unstable\n"
    "distribution that some required packages have not yet been created\n"
    "or been moved out of Incoming.\n"
    "The following information may help to resolve the situation:\n\n"
    "The following packages have unmet dependencies:\n"
    " some-broken-app : Depends: libsome-removed-lib (>= 2.0) but it is not installable\n"
    "E: Unable to correct problems, you have held broken packages.\n"
)

UNMET_CONFLICTS_OUTPUT = (
    "Reading package lists...\nBuilding dependency tree...\nReading state information...\n"
    "The following packages have unmet dependencies:\n"
    " some-app : Conflicts: ufw but 0.36.2-9 is to be installed\n"
    "E: Unable to correct problems, you have held broken packages.\n"
)

NOT_LOCATED_OUTPUT = "Reading package lists...\nE: Unable to locate package ghost-package\n"


class ClassifySimulationTests(unittest.TestCase):
    def test_a_zero_return_is_always_clean(self) -> None:
        status, detail = cas.classify_simulation(0, REAL_CLEAN_OUTPUT, "")
        self.assertEqual((cas.STATUS_CLEAN, None), (status, detail))

    def test_needs_removal_with_remove_disabled_is_a_conflict_naming_what_would_go(self) -> None:
        status, detail = cas.classify_simulation(100, REAL_CONFLICT_NEEDS_REMOVAL_OUTPUT, "")
        self.assertEqual(cas.STATUS_CONFLICT, status)
        self.assertIn("postfix", detail)

    def test_unmet_depends_is_unsatisfiable_naming_the_missing_dependency(self) -> None:
        status, detail = cas.classify_simulation(100, UNMET_DEPENDS_OUTPUT, "")
        self.assertEqual(cas.STATUS_UNSATISFIABLE, status)
        self.assertIn("libsome-removed-lib", detail)

    def test_unmet_conflicts_is_a_conflict_naming_the_conflicting_package(self) -> None:
        status, detail = cas.classify_simulation(100, UNMET_CONFLICTS_OUTPUT, "")
        self.assertEqual(cas.STATUS_CONFLICT, status)
        self.assertIn("ufw", detail)

    def test_conflicts_wins_over_depends_when_both_are_present(self) -> None:
        mixed = UNMET_DEPENDS_OUTPUT.replace(
            "E: Unable to correct", " some-app : Conflicts: ufw but 0.36.2-9 is to be installed\nE: Unable to correct")
        status, _detail = cas.classify_simulation(100, mixed, "")
        self.assertEqual(cas.STATUS_CONFLICT, status)

    def test_an_unrecognised_failure_is_error_not_silently_folded_into_another_bucket(self) -> None:
        status, detail = cas.classify_simulation(100, "E: something apt has never said before\n", "")
        self.assertEqual(cas.STATUS_ERROR, status)
        self.assertIn("something apt has never said before", detail)

    def test_not_located_falls_to_error_here_since_presence_is_pre_filtered_upstream(self) -> None:
        # solve_one_combo() never calls classify_simulation for a package
        # catalog_apps_audit already found absent - this is just proving
        # classify_simulation itself does not misreport it as clean or as a
        # dependency problem if it were ever reached by mistake.
        status, _detail = cas.classify_simulation(100, NOT_LOCATED_OUTPUT, "")
        self.assertEqual(cas.STATUS_ERROR, status)


class RenderSourcesTests(unittest.TestCase):
    def test_substitutes_every_placeholder_from_base_env_and_suite(self) -> None:
        template = ("Types: deb\nURIs: ${APT_MIRROR}\nSuites: ${SUITE} ${SUITE}-updates ${SUITE}-backports\n"
                   "Components: ${COMPONENTS}\nSigned-By: ${KEYRING}\n\n"
                   "Types: deb\nURIs: ${SECURITY_MIRROR}\nSuites: ${SUITE}-security\n"
                   "Components: ${COMPONENTS}\nSigned-By: ${KEYRING}\n")
        base_env = {"APT_MIRROR": "http://example/debian/", "SECURITY_MIRROR": "http://example/debian-security/",
                    "COMPONENTS": "main contrib", "KEYRING": "/usr/share/keyrings/x.gpg"}
        text = cas.render_sources(template, base_env, "trixie")
        self.assertNotIn("${", text)
        self.assertIn("Suites: trixie trixie-updates trixie-backports", text)
        self.assertIn("Suites: trixie-security", text)
        self.assertIn("URIs: http://example/debian/", text)
        self.assertIn("Components: main contrib", text)

    def test_falls_back_to_apt_mirror_when_security_mirror_is_absent(self) -> None:
        template = "URIs: ${SECURITY_MIRROR}\n"
        base_env = {"APT_MIRROR": "http://example/debian/", "COMPONENTS": "main", "KEYRING": "/x.gpg"}
        text = cas.render_sources(template, base_env, "trixie")
        self.assertIn("URIs: http://example/debian/", text)


class FloorPackagesTests(unittest.TestCase):
    """No podman needed - resolve_profile("minimal") just reads
    profiles/minimal.yml and bases/<base>/packages.map, both local files."""

    def test_a_package_the_real_archive_does_not_have_is_named_absent_not_installed(self) -> None:
        present, absent = cas.floor_packages("debian", frozenset({"locales", "ufw", "auditd"}))
        self.assertIn("locales", present)
        self.assertNotIn("hunspell-en", present)
        self.assertIn("hunspell-en", absent)

    def test_every_present_package_is_also_wanted(self) -> None:
        present, absent = cas.floor_packages("ubuntu", frozenset({"language-pack-en"}))
        self.assertEqual(["language-pack-en"], present)
        self.assertGreater(len(absent), 0)


# --------------------------------------------------------------- FakeRunner
class FakeRunner(cas.Runner):
    """Scripted responses keyed by package name for simulate(); everything
    else is configurable per instance. Records every call for assertions."""

    def __init__(self, *, sim_by_package: dict[str, tuple[int, str, str]] | None = None,
                update_rc: int = 0, floor_install_rc: int = 0, pull_ok: bool = True, start_ok: bool = True) -> None:
        self.sim_by_package = sim_by_package or {}
        self.update_rc = update_rc
        self.floor_install_rc = floor_install_rc
        self.pull_ok = pull_ok
        self.start_ok = start_ok
        self.calls: list[str] = []
        self.cleaned_up = False
        self.installed_floor: list[str] | None = None

    def pull(self) -> None:
        self.calls.append("pull")
        if not self.pull_ok:
            raise cas.SolverError("could not pull the image")

    def start(self) -> None:
        self.calls.append("start")
        if not self.start_ok:
            raise cas.SolverError("could not start the container")

    def configure_sources(self, sources_text: str) -> cas.ExecResult:
        self.calls.append("configure_sources")
        return cas.ExecResult(0, "", "")

    def apt_update(self) -> cas.ExecResult:
        self.calls.append("apt_update")
        return cas.ExecResult(self.update_rc, "", "" if self.update_rc == 0 else "update failed")

    def install_floor(self, packages: list[str]) -> cas.ExecResult:
        self.calls.append("install_floor")
        self.installed_floor = list(packages)
        return cas.ExecResult(self.floor_install_rc, "", "" if self.floor_install_rc == 0 else "floor install failed")

    def simulate(self, package: str) -> cas.ExecResult:
        self.calls.append(f"simulate:{package}")
        rc, out, err = self.sim_by_package.get(package, (0, REAL_CLEAN_OUTPUT, ""))
        return cas.ExecResult(rc, out, err)

    def cleanup(self) -> None:
        self.calls.append("cleanup")
        self.cleaned_up = True


def entry(entry_id: str, package: str, available: dict) -> dict:
    return {"id": entry_id, "package": package, "title": entry_id, "available": available}


MATRIX = {"debian": ["trixie"], "ubuntu": ["noble"]}
BASE_ENVS = {
    "debian": {"APT_MIRROR": "http://deb.debian.org/debian/", "SECURITY_MIRROR": "http://security.debian.org/debian-security/",
              "COMPONENTS": "main contrib non-free non-free-firmware", "KEYRING": "/usr/share/keyrings/debian-archive-keyring.gpg"},
    "ubuntu": {"APT_MIRROR": "http://archive.ubuntu.com/ubuntu/", "SECURITY_MIRROR": "http://security.ubuntu.com/ubuntu/",
              "COMPONENTS": "main restricted universe multiverse", "KEYRING": "/usr/share/keyrings/ubuntu-archive-keyring.gpg"},
}


def gz_packages(*names: str) -> bytes:
    import gzip
    return gzip.compress("\n\n".join(f"Package: {n}" for n in names).encode())


class ArchiveAwareFetcher:
    """A fake fetcher for tools/catalog_apps_audit.fetch_index (the exact
    thing this module reuses, never re-implemented): every dep11-shaped url
    is irrelevant here (the solver never reads AppStream), every
    Packages.gz url returns the same fixed set of names, or raises for a
    url in `broken`."""

    def __init__(self, names: set[str], broken: set[str] | None = None) -> None:
        self.names = names
        self.broken = broken or set()
        self.calls: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.calls.append(url)
        if url in self.broken:
            raise ConnectionError(f"could not reach {url}")
        return gz_packages(*self.names)


FLOOR_NAMES = {"firmware-iwlwifi", "firmware-realtek", "firmware-atheros", "firmware-linux", "firmware-sof-signed",
              "locales", "apparmor-profiles-extra", "ufw", "auditd", "fwupd", "unattended-upgrades",
              "linux-firmware", "language-pack-en", "language-pack-en-base", "language-pack-gnome-en",
              "language-pack-gnome-en-base"}


class RunSolverTests(unittest.TestCase):
    def test_a_clean_entry_reports_clean_and_installs_the_floor_first(self) -> None:
        e = entry("a", "some-clean-app", {"debian": ["trixie"]})
        fetcher = ArchiveAwareFetcher(FLOOR_NAMES | {"some-clean-app"})
        runners: list[FakeRunner] = []

        def factory(base, suite):
            r = FakeRunner()
            runners.append(r)
            return r

        report = cas.run_solver([e], MATRIX, BASE_ENVS, fetcher=fetcher, runner_factory=factory)
        self.assertEqual(1, report["checks_total"])
        self.assertEqual(cas.STATUS_CLEAN, report["results"][0]["status"])
        self.assertEqual(1, len(runners))
        self.assertIn("install_floor", runners[0].calls)
        self.assertIn("simulate:some-clean-app", runners[0].calls)
        self.assertTrue(runners[0].cleaned_up, "the container must always be cleaned up")

    def test_an_unsatisfiable_entry_is_reported_with_its_detail(self) -> None:
        e = entry("a", "broken-app", {"debian": ["trixie"]})
        fetcher = ArchiveAwareFetcher(FLOOR_NAMES | {"broken-app"})
        sim = {"broken-app": (100, UNMET_DEPENDS_OUTPUT, "")}

        report = cas.run_solver([e], MATRIX, BASE_ENVS, fetcher=fetcher,
                                runner_factory=lambda b, s: FakeRunner(sim_by_package=sim))
        self.assertEqual(cas.STATUS_UNSATISFIABLE, report["results"][0]["status"])
        self.assertIn("libsome-removed-lib", report["results"][0]["detail"])
        self.assertEqual(1, cas.exit_code(report))

    def test_a_conflicting_entry_is_reported_separately_from_unsatisfiable(self) -> None:
        e = entry("a", "mta-app", {"debian": ["trixie"]})
        fetcher = ArchiveAwareFetcher(FLOOR_NAMES | {"mta-app"})
        sim = {"mta-app": (100, REAL_CONFLICT_NEEDS_REMOVAL_OUTPUT, "")}
        report = cas.run_solver([e], MATRIX, BASE_ENVS, fetcher=fetcher,
                                runner_factory=lambda b, s: FakeRunner(sim_by_package=sim))
        self.assertEqual(cas.STATUS_CONFLICT, report["results"][0]["status"])
        self.assertEqual(1, cas.exit_code(report))

    def test_an_entry_absent_from_the_archive_is_reused_from_the_existing_audit_never_simulated(self) -> None:
        e = entry("a", "ghost-app", {"debian": ["trixie"]})
        fetcher = ArchiveAwareFetcher(FLOOR_NAMES)  # ghost-app is not in the index at all
        runner = FakeRunner()
        report = cas.run_solver([e], MATRIX, BASE_ENVS, fetcher=fetcher, runner_factory=lambda b, s: runner)
        self.assertEqual(cas.STATUS_NOT_IN_ARCHIVE, report["results"][0]["status"])
        self.assertFalse(any(c.startswith("simulate:") for c in runner.calls),
                         "an entry already known absent must never reach apt's solver at all")
        self.assertEqual(0, cas.exit_code(report), "not-in-archive alone must not fail this tool's own gate")

    def test_an_unreachable_archive_index_is_could_not_check_and_never_starts_a_container(self) -> None:
        e = entry("a", "some-app", {"debian": ["trixie"]})
        broken_url = cas.catalog_conformance._component_urls(BASE_ENVS["debian"], "trixie", cas.DEFAULT_ARCH)[0]
        fetcher = ArchiveAwareFetcher(FLOOR_NAMES, broken={broken_url})
        runner = FakeRunner()
        report = cas.run_solver([e], MATRIX, BASE_ENVS, fetcher=fetcher, runner_factory=lambda b, s: runner)
        self.assertEqual(0, report["checks_total"])
        self.assertEqual(1, len(report["could_not_check"]))
        self.assertEqual([], runner.calls, "a runner must never even be created for an unreachable archive")
        self.assertEqual(2, cas.exit_code(report))

    def test_a_floor_that_fails_to_install_is_could_not_check_for_the_whole_combo(self) -> None:
        e1 = entry("a", "app-one", {"debian": ["trixie"]})
        e2 = entry("b", "app-two", {"debian": ["trixie"]})
        fetcher = ArchiveAwareFetcher(FLOOR_NAMES | {"app-one", "app-two"})
        runner = FakeRunner(floor_install_rc=100)
        report = cas.run_solver([e1, e2], MATRIX, BASE_ENVS, fetcher=fetcher, runner_factory=lambda b, s: runner)
        self.assertEqual([], report["results"])
        self.assertEqual(1, len(report["could_not_check"]))
        self.assertEqual(2, report["could_not_check"][0]["entries_affected"])
        self.assertTrue(runner.cleaned_up)

    def test_a_container_that_fails_to_start_is_could_not_check_and_still_cleaned_up_is_a_noop(self) -> None:
        e = entry("a", "some-app", {"debian": ["trixie"]})
        fetcher = ArchiveAwareFetcher(FLOOR_NAMES | {"some-app"})
        runner = FakeRunner(start_ok=False)
        report = cas.run_solver([e], MATRIX, BASE_ENVS, fetcher=fetcher, runner_factory=lambda b, s: runner)
        self.assertEqual(1, len(report["could_not_check"]))
        self.assertIn("cleanup", runner.calls)

    def test_one_archive_fetch_per_combo_no_matter_how_many_entries_share_it(self) -> None:
        entries = [entry(str(i), f"pkg-{i}", {"ubuntu": ["noble"]}) for i in range(20)]
        names = FLOOR_NAMES | {f"pkg-{i}" for i in range(20)}
        fetcher = ArchiveAwareFetcher(names)
        runner = FakeRunner()
        cas.run_solver(entries, MATRIX, BASE_ENVS, fetcher=fetcher, runner_factory=lambda b, s: runner)
        expected_urls = cas.catalog_conformance._component_urls(BASE_ENVS["ubuntu"], "noble", cas.DEFAULT_ARCH)
        self.assertEqual(len(expected_urls), len(fetcher.calls))

    def test_a_combo_with_no_claimed_entries_never_creates_a_runner(self) -> None:
        e = entry("a", "some-app", {"debian": ["trixie"]})  # never claims ubuntu at all
        fetcher = ArchiveAwareFetcher(FLOOR_NAMES | {"some-app"})
        factories_called: list[tuple[str, str]] = []

        def factory(base, suite):
            factories_called.append((base, suite))
            return FakeRunner()

        cas.run_solver([e], MATRIX, BASE_ENVS, fetcher=fetcher, runner_factory=factory)
        self.assertEqual([("debian", "trixie")], factories_called)

    def test_a_clean_run_with_nothing_unverifiable_exits_zero(self) -> None:
        e = entry("a", "some-clean-app", {"debian": ["trixie"]})
        fetcher = ArchiveAwareFetcher(FLOOR_NAMES | {"some-clean-app"})
        report = cas.run_solver([e], MATRIX, BASE_ENVS, fetcher=fetcher, runner_factory=lambda b, s: FakeRunner())
        self.assertEqual(0, cas.exit_code(report))


if __name__ == "__main__":
    unittest.main()
