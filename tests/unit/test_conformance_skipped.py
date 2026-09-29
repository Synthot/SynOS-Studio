"""catalog_conformance's skip-vs-fail distinction (item 199): a catalogue
entry that could never be attempted at all -- today, only "no browser
available to drive the page" produces that -- must never be reported,
summarized, or exited as though it had been attempted and had failed.

Split out of test_catalog_conformance.py to stay under the reviewable
line-count cap tests/unit/test_architecture.py enforces; shares that
module's fixtures rather than duplicating them, the same way
test_conformance_publish.py does.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from test_catalog_conformance import (  # shared fixtures, not duplicated
    cc, real_catalog, entry_by_id, make_bundle_archive,
    FakeSession, FAKE_BUILD_SH_ENGINE_FAIL,
)


class PartialBrowserSession(FakeSession):
    """Like FakeSession, but only entry ids in `unavailable_ids` raise
    BrowserUnavailable -- every other entry behaves exactly like a normal
    FakeSession. A real devtools_browser.StudioSession can raise
    BrowserUnavailable either from `__enter__` (chrome missing entirely)
    or from inside the `with` block (chrome present but this particular
    page crashes it) -- build_one()'s own try/except around both covers
    either identically, so raising from download_bundle() here proves
    exactly the same "skipped" path a real per-page browser crash would,
    without needing two different fixture shapes for the same outcome."""

    def __init__(self, *, unavailable_ids: frozenset = frozenset(), **kwargs) -> None:
        super().__init__(**kwargs)
        self.unavailable_ids = unavailable_ids

    def download_bundle(self, entry_id: str, name: str | None = None) -> tuple[bytes, str]:
        if entry_id in self.unavailable_ids:
            raise cc.devtools_browser.BrowserUnavailable(f"no browser available (test fixture, {entry_id})")
        return super().download_bundle(entry_id, name)


class SkippedEntryExitCodeTests(unittest.TestCase):
    """item 199: cc._run()'s own exit code, and its printed/persisted
    summary, both distinguish "this entry could not be attempted" from
    "this entry was attempted and failed". Real cc._run() throughout
    (never run_build() directly, since the exit code and the
    previous-report comparison are both _run()'s own logic) -- same
    monkeypatch/isolation convention PublishExitCodeTests in
    test_conformance_publish.py uses; a real container runtime is never
    touched."""

    def _run(self, tmp: str, *, ids: list[str], unavailable_ids: frozenset = frozenset(),
             failing_ids: frozenset = frozenset(), previous_report: dict | None = None,
             destinations_yaml: str = "", upload_transport=None,
             upload_verify_fetcher=None) -> tuple[int, dict, str]:
        catalog = real_catalog()
        entries = [entry_by_id(catalog, entry_id) for entry_id in ids]
        fake_catalog = {"bundle_catalog": entries}
        archives = {
            entry_id: make_bundle_archive(entry_id=entry_id,
                                          build_sh=FAKE_BUILD_SH_ENGINE_FAIL if entry_id in failing_ids else None)
            for entry_id in ids
        }
        session = PartialBrowserSession(archives_by_id=archives, unavailable_ids=unavailable_ids)

        original_fetch = cc.fetch_catalog
        cc.fetch_catalog = lambda url, **kwargs: fake_catalog
        original_session = cc.devtools_browser.StudioSession
        cc.devtools_browser.StudioSession = lambda *a, **k: session
        # No real podman/docker query for the end-of-run cache-volume
        # reclaim step (item 91) -- same isolation PublishExitCodeTests
        # uses, for the same reason: a handful of slow, real subprocess
        # calls that would tell this test nothing.
        original_container_store = cc.host_resources.container_store
        cc.host_resources.container_store = lambda engine, container_root=None: None
        try:
            work = Path(tmp) / "work"
            work.mkdir(parents=True)
            if previous_report is not None:
                (work / "build-report.json").write_text(json.dumps(previous_report), encoding="utf-8")
            config_path = Path(tmp) / "conformance.yml"
            config_path.write_text(
                "catalog_url: http://x\n"
                f"workdir: {work}\n"
                "site_url: http://dev-site.example\n"
                "smoke: false\n"
                f"{destinations_yaml}\n",
                encoding="utf-8")
            config = cc.Config.load(config_path)
            code = cc._run("build", config, only=ids,
                           upload_transport=upload_transport, upload_verify_fetcher=upload_verify_fetcher)
        finally:
            cc.fetch_catalog = original_fetch
            cc.devtools_browser.StudioSession = original_session
            cc.host_resources.container_store = original_container_store
        report = json.loads((work / "build-report.json").read_text(encoding="utf-8"))
        summary = (work / "build-summary.txt").read_text(encoding="utf-8")
        return code, report, summary

    def test_a_fully_clean_run_with_no_skips_exits_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            code, report, summary = self._run(tmp, ids=["web-server-nginx"])
        self.assertEqual(cc.EXIT_OK, code)
        self.assertEqual(0, code)
        self.assertEqual("success", report["targets"]["web-server-nginx"]["status"])

    def test_every_entry_skipped_exits_entries_skipped_not_entries_failed(self) -> None:
        ids = ["web-server-nginx", "git-server"]
        with tempfile.TemporaryDirectory() as tmp:
            code, report, summary = self._run(tmp, ids=ids, unavailable_ids=frozenset(ids))
        self.assertEqual(cc.EXIT_ENTRIES_SKIPPED, code)
        self.assertEqual(4, code)
        self.assertEqual({"skipped"}, {record["status"] for record in report["targets"].values()})
        self.assertIn("newly skipped:", summary)
        self.assertNotIn("newly failing:", summary)

    def test_one_real_failure_alongside_a_skip_still_exits_entries_failed(self) -> None:
        """The real gate (item 198) still wins: a skip elsewhere in the
        same run must never soften a genuine failure's exit code, and a
        genuine failure must never be reported as though it were merely
        skipped."""
        with tempfile.TemporaryDirectory() as tmp:
            code, report, summary = self._run(
                tmp, ids=["web-server-nginx", "git-server"],
                unavailable_ids=frozenset({"git-server"}), failing_ids=frozenset({"web-server-nginx"}))
        self.assertEqual(cc.EXIT_ENTRIES_FAILED, code)
        self.assertEqual(1, code)
        self.assertEqual("skipped", report["targets"]["git-server"]["status"])
        self.assertEqual("build_failed", report["targets"]["web-server-nginx"]["status"])

    def test_a_clean_publish_failure_outranks_a_mere_skip(self) -> None:
        """Precedence (item 199's own comment beside the exit codes):
        EXIT_PUBLISH_FAILED (3) still wins over EXIT_ENTRIES_SKIPPED (4)
        -- a stale badge is worse than an entry nobody could attempt."""
        def always_fails(config, path):
            raise RuntimeError("network unreachable")

        destinations_yaml = (
            "upload:\n"
            "  - name: production\n"
            "    protocol: sftp\n"
            "    host: prod.example\n"
            "    retries: 1\n"
            "    remote_path: /remote/build-status.json\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            code, report, summary = self._run(
                tmp, ids=["web-server-nginx", "git-server"], unavailable_ids=frozenset({"git-server"}),
                destinations_yaml=destinations_yaml, upload_transport=always_fails)
        self.assertEqual(cc.EXIT_PUBLISH_FAILED, code)
        self.assertEqual(3, code)
        self.assertEqual("skipped", report["targets"]["git-server"]["status"])

    def test_end_to_end_previously_ok_now_skipped_says_newly_skipped_not_newly_failing(self) -> None:
        """The exact bug report this item fixes: an entry that was
        "success" last run and is "skipped" this run (the browser just
        vanished from PATH) must read as "newly skipped", never "newly
        failing" -- and the run's own exit code must be the new,
        weakest one, not the real-failure gate."""
        entry_id = "web-server-nginx"
        previous_report = {"targets": {entry_id: {"status": "success"}}}
        with tempfile.TemporaryDirectory() as tmp:
            code, report, summary = self._run(tmp, ids=[entry_id], unavailable_ids=frozenset({entry_id}),
                                              previous_report=previous_report)
        self.assertEqual(cc.EXIT_ENTRIES_SKIPPED, code)
        self.assertIn(f"newly skipped: {entry_id}", summary)
        self.assertNotIn("newly failing", summary)

    def test_a_repeat_skip_is_not_newly_anything_end_to_end(self) -> None:
        """An entry skipped last run and skipped again this run is not
        news -- the browser was missing before and still is."""
        entry_id = "web-server-nginx"
        previous_report = {"targets": {entry_id: {"status": "skipped"}}}
        with tempfile.TemporaryDirectory() as tmp:
            code, report, summary = self._run(tmp, ids=[entry_id], unavailable_ids=frozenset({entry_id}),
                                              previous_report=previous_report)
        self.assertEqual(cc.EXIT_ENTRIES_SKIPPED, code)
        self.assertNotIn("newly skipped", summary)
        self.assertNotIn("newly failing", summary)


class SkippedEntryBuildStatusTests(unittest.TestCase):
    """item 199, point 3: build_status.py's map_state() already folds a
    "skipped" build_one() result to its own "skipped" state in the public
    build-status.json (never "failed"), and the run's own build-report.json
    keeps "skipped" too. This pins both with a test rather than changing
    either -- they were already honest before this item."""

    def test_map_state_folds_skipped_to_its_own_state_not_failed(self) -> None:
        self.assertEqual("skipped", cc.build_status.map_state("skipped"))
        self.assertNotEqual("failed", cc.build_status.map_state("skipped"))

    def test_a_skipped_entrys_build_status_state_is_skipped_not_failed(self) -> None:
        entry_id = "web-server-nginx"
        catalog = real_catalog()
        session = PartialBrowserSession(unavailable_ids=frozenset({entry_id}))
        with tempfile.TemporaryDirectory() as tmp:
            config = cc.Config(catalog_url="http://x", workdir=Path(tmp) / "work", site_url="http://fake",
                               smoke=False)
            report = cc.run_build(config, catalog=catalog, only=[entry_id], browser_factory=session)
            status = json.loads((Path(tmp) / "work" / "build-status.json").read_text(encoding="utf-8"))
        self.assertEqual("skipped", report["targets"][entry_id]["status"])
        self.assertEqual("skipped", status["entries"][entry_id]["bases"]["ubuntu"]["state"])


class DiffReportsSkipMatrixTests(unittest.TestCase):
    """item 199's own previous/current state matrix, worked through in
    full: 4 previous categories (never seen before, ok, skipped, failed)
    x 3 current categories (ok, skipped, failed) = 12 cells, each pinned
    to a real cc.diff_reports() call -- not just the three cases the task
    text calls out by name ("was skipped, still skipped" is NOT newly
    anything; "was ok, now skipped" IS newly skipped; "was skipped, now
    genuinely fails" is newly failed)."""

    STATUS_BY_CATEGORY = {"ok": "success", "skipped": "skipped", "failed": "build_failed"}

    def _diff(self, prev_category: str, current_category: str) -> dict:
        previous = None if prev_category == "absent" else {
            "targets": {"a": {"status": self.STATUS_BY_CATEGORY[prev_category]}}}
        current = {"targets": {"a": {"status": self.STATUS_BY_CATEGORY[current_category]}}}
        return cc.diff_reports(previous, current)

    def test_absent_then_ok_is_unchanged(self) -> None:
        self.assertEqual(["a"], self._diff("absent", "ok")["unchanged"])

    def test_absent_then_skipped_is_newly_skipped(self) -> None:
        self.assertEqual(["a"], self._diff("absent", "skipped")["newly_skipped"])

    def test_absent_then_failed_is_newly_failed(self) -> None:
        self.assertEqual(["a"], self._diff("absent", "failed")["newly_failed"])

    def test_ok_then_ok_is_unchanged(self) -> None:
        self.assertEqual(["a"], self._diff("ok", "ok")["unchanged"])

    def test_ok_then_skipped_is_newly_skipped(self) -> None:
        self.assertEqual(["a"], self._diff("ok", "skipped")["newly_skipped"])

    def test_ok_then_failed_is_newly_failed(self) -> None:
        self.assertEqual(["a"], self._diff("ok", "failed")["newly_failed"])

    def test_skipped_then_ok_is_recovered(self) -> None:
        self.assertEqual(["a"], self._diff("skipped", "ok")["recovered"])

    def test_skipped_then_skipped_is_unchanged_not_newly_anything(self) -> None:
        diff = self._diff("skipped", "skipped")
        self.assertEqual(["a"], diff["unchanged"])
        self.assertEqual([], diff["newly_skipped"])
        self.assertEqual([], diff["newly_failed"])

    def test_skipped_then_failed_is_newly_failed_not_still_failing(self) -> None:
        diff = self._diff("skipped", "failed")
        self.assertEqual(["a"], diff["newly_failed"])
        self.assertEqual([], diff["still_failing"])

    def test_failed_then_ok_is_recovered(self) -> None:
        self.assertEqual(["a"], self._diff("failed", "ok")["recovered"])

    def test_failed_then_skipped_is_newly_skipped(self) -> None:
        self.assertEqual(["a"], self._diff("failed", "skipped")["newly_skipped"])

    def test_failed_then_failed_is_still_failing(self) -> None:
        self.assertEqual(["a"], self._diff("failed", "failed")["still_failing"])


if __name__ == "__main__":
    unittest.main()
