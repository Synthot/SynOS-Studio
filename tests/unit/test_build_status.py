"""tools/build_status.py: the small, versioned file a browser fetches on
every page load to show a bundle's badge - queued/testing (both "not yet
tested" to a viewer), succeeded, or failed since a date - plus a bounded
history and a sanitized error summary. Pure functions and file I/O only -
no network, no browser, no launcher."""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import build_status as bs  # noqa: E402


def success_result(entry_id: str, *, size: int = 1000, checksum: str = "abc123",
                   smoke_status: str = "passed", engine: str = "0.2.0",
                   base: str = "ubuntu", suite: str = "noble", duration_s: float = 100.0,
                   end: str = "2026-09-24T16:00:00+00:00") -> dict:
    return {"id": entry_id, "status": "success", "engine": engine, "base": base, "suite": suite,
            "end": end, "start": "2026-09-24T15:50:00+00:00", "duration_s": duration_s, "stage": None,
            "iso": {"path": "/x.iso", "size": size, "sha256": checksum},
            "smoke": {"status": smoke_status}, "log_tail": []}


def failed_result(entry_id: str, *, stage: str = "engine", end: str = "2026-09-24T16:05:00+00:00",
                  log_tail: list | None = None, duration_s: float = 50.0) -> dict:
    return {"id": entry_id, "status": "build_failed", "stage": stage, "engine": "0.2.0",
            "base": "ubuntu", "suite": "noble", "end": end, "start": "2026-09-24T15:55:00+00:00",
            "duration_s": duration_s, "iso": None, "smoke": None,
            "log_tail": log_tail if log_tail is not None else ["error: build step 7 failed"]}


def skipped_result(entry_id: str, *, end: str = "2026-09-24T16:01:00+00:00",
                   log_tail: list | None = None) -> dict:
    return {"id": entry_id, "status": "skipped", "stage": "page", "engine": "0.2.0",
            "base": None, "suite": None, "end": end, "start": "2026-09-24T16:00:50+00:00",
            "duration_s": 0.1, "iso": None, "smoke": None,
            "log_tail": log_tail if log_tail is not None else ["no browser available"]}


class MapStateTests(unittest.TestCase):
    def test_success_maps_to_success(self) -> None:
        self.assertEqual("success", bs.map_state("success"))

    def test_skipped_maps_to_skipped(self) -> None:
        self.assertEqual("skipped", bs.map_state("skipped"))

    def test_every_other_finer_status_folds_to_failed(self) -> None:
        for status in ("failed", "error", "timeout", "invalid", "host", "build_failed", "running", None, "anything-else"):
            self.assertEqual("failed", bs.map_state(status), status)


class SanitizeErrorTests(unittest.TestCase):
    """Item 96: a failure summary is the useful line, not a wall, and
    never anything machine-specific."""

    def test_control_characters_are_stripped(self) -> None:
        text = "before\x00\x07\x1bafter"
        self.assertEqual("beforeafter", bs.sanitize_error(text))

    def test_only_the_first_non_blank_line_is_kept(self) -> None:
        text = "\n\n  real error here  \nsecond line\nthird line"
        self.assertEqual("real error here", bs.sanitize_error(text))

    def test_a_configured_own_path_is_redacted(self) -> None:
        text = "wrote log to /var/lib/synos-conformance/work/x/output/build.log"
        redacted = bs.sanitize_error(text, own_paths=["/var/lib/synos-conformance"])
        self.assertNotIn("/var/lib/synos-conformance", redacted)
        self.assertIn("<path>", redacted)

    def test_a_generic_home_directory_path_is_redacted_even_if_not_the_configured_one(self) -> None:
        text = "permission denied: /home/someoperator/.cache/thing"
        redacted = bs.sanitize_error(text)
        self.assertNotIn("/home/someoperator", redacted)
        self.assertIn("<path>", redacted)

    def test_a_root_home_path_is_redacted(self) -> None:
        redacted = bs.sanitize_error("failed writing /root/.config/synos/state")
        self.assertNotIn("/root/.config", redacted)

    def test_hostname_is_redacted(self) -> None:
        redacted = bs.sanitize_error("connection refused by build-runner-07", hostname="build-runner-07")
        self.assertNotIn("build-runner-07", redacted)
        self.assertIn("<host>", redacted)

    def test_an_ipv4_address_is_redacted(self) -> None:
        redacted = bs.sanitize_error("could not reach 10.20.30.40 on port 443")
        self.assertNotIn("10.20.30.40", redacted)
        self.assertIn("<ip>", redacted)

    def test_a_credential_looking_assignment_is_redacted(self) -> None:
        for text in ("auth failed: password=hunter2-secret", "token: abcdef0123456789",
                     "using api_key=sk-live-deadbeef", "SECRET=topsecretvalue rejected"):
            redacted = bs.sanitize_error(text)
            self.assertNotIn("hunter2-secret", redacted)
            self.assertNotIn("abcdef0123456789", redacted)
            self.assertNotIn("sk-live-deadbeef", redacted)
            self.assertNotIn("topsecretvalue", redacted)

    def test_a_log_line_with_a_path_an_address_and_a_credential_is_fully_sanitized(self) -> None:
        """The exact combined case item 97 asks for."""
        text = ("build.sh: connecting to 203.0.113.9 as /home/buildbot/.ssh/id_rsa "
                "with password=SuperSecretPW123 failed")
        redacted = bs.sanitize_error(text, own_paths=["/home/buildbot"])
        self.assertNotIn("203.0.113.9", redacted)
        self.assertNotIn("/home/buildbot", redacted)
        self.assertNotIn("SuperSecretPW123", redacted)

    def test_long_text_is_capped_with_an_ellipsis(self) -> None:
        text = "x" * 1000
        redacted = bs.sanitize_error(text)
        self.assertLessEqual(len(redacted), bs.MAX_ERROR_LEN + 1)
        self.assertTrue(redacted.endswith("…"))

    def test_empty_or_none_text_is_an_empty_string(self) -> None:
        self.assertEqual("", bs.sanitize_error(None))
        self.assertEqual("", bs.sanitize_error(""))
        self.assertEqual("", bs.sanitize_error("   \n  \n "))


class ErrorSummaryTests(unittest.TestCase):
    def test_combines_stage_and_first_log_line(self) -> None:
        result = failed_result("x", stage="engine", log_tail=["exit code 7", "more detail"])
        summary = bs.error_summary(result)
        self.assertIn("engine", summary)
        self.assertIn("exit code 7", summary)
        self.assertNotIn("more detail", summary)

    def test_no_stage_and_no_log_tail_is_none(self) -> None:
        self.assertIsNone(bs.error_summary({"stage": None, "log_tail": []}))

    def test_stage_only_is_used_when_log_tail_is_empty(self) -> None:
        summary = bs.error_summary({"stage": "launcher", "log_tail": []})
        self.assertEqual("launcher", summary)


class EntryRecordViaApplyResultTests(unittest.TestCase):
    def test_success_record_carries_iso_size_checksum_and_smoke(self) -> None:
        status, _ = bs.apply_result(bs.empty_status(), "web-server-nginx",
                                    success_result("web-server-nginx", size=42, checksum="deadbeef"))
        record = status["entries"]["web-server-nginx"]
        self.assertEqual("success", record["state"])
        self.assertEqual("2026-09-24T16:00:00+00:00", record["date"])
        self.assertEqual("0.2.0", record["engine"])
        self.assertEqual("ubuntu", record["base"])
        self.assertEqual("noble", record["suite"])
        self.assertEqual(42, record["size"])
        self.assertEqual("deadbeef", record["checksum"])
        self.assertIs(True, record["smoke_passed"])
        self.assertIsNone(record["error"])

    def test_failed_record_has_null_size_checksum_and_an_error(self) -> None:
        status, _ = bs.apply_result(bs.empty_status(), "x", failed_result("x"))
        record = status["entries"]["x"]
        self.assertEqual("failed", record["state"])
        self.assertIsNone(record["size"])
        self.assertIsNone(record["checksum"])
        self.assertIsNone(record["smoke_passed"])
        self.assertIsNotNone(record["error"])

    def test_smoke_failed_is_recorded_as_false_not_null(self) -> None:
        status, _ = bs.apply_result(bs.empty_status(), "x", success_result("x", smoke_status="failed"))
        self.assertIs(False, status["entries"]["x"]["smoke_passed"])

    def test_smoke_skipped_is_null_not_false(self) -> None:
        status, _ = bs.apply_result(bs.empty_status(), "x", success_result("x", smoke_status="skipped"))
        self.assertIsNone(status["entries"]["x"]["smoke_passed"])

    def test_malformed_result_never_raises(self) -> None:
        status, _ = bs.apply_result(bs.empty_status(), "x", {})
        self.assertEqual("failed", status["entries"]["x"]["state"])

    def test_own_paths_and_hostname_reach_the_recorded_error(self) -> None:
        result = failed_result("x", log_tail=["failed on host build-01 at /srv/synos/work/x"])
        status, _ = bs.apply_result(bs.empty_status(), "x", result, own_paths=["/srv/synos"], hostname="build-01")
        error = status["entries"]["x"]["error"]
        self.assertNotIn("/srv/synos", error)
        self.assertNotIn("build-01", error)


class LoadWriteStatusTests(unittest.TestCase):
    def test_missing_file_loads_as_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            status = bs.load_status(Path(tmp) / "build-status.json")
        self.assertEqual(bs.SCHEMA_VERSION, status["schema_version"])
        self.assertEqual({}, status["entries"])

    def test_corrupt_file_loads_as_empty_rather_than_raising(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "build-status.json"
            path.write_text("{not valid json", encoding="utf-8")
            status = bs.load_status(path)
        self.assertEqual({}, status["entries"])

    def test_wrong_schema_version_loads_as_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "build-status.json"
            path.write_text(json.dumps({"schema_version": 999, "entries": {}}), encoding="utf-8")
            status = bs.load_status(path)
        self.assertEqual(bs.SCHEMA_VERSION, status["schema_version"])

    def test_write_then_load_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sub" / "build-status.json"  # parent does not exist yet
            status, _ = bs.apply_result(bs.empty_status(), "web-server-nginx", success_result("web-server-nginx"))
            bs.write_status_atomic(path, status)
            reloaded = bs.load_status(path)
        self.assertEqual("success", reloaded["entries"]["web-server-nginx"]["state"])

    def test_write_leaves_no_temp_file_behind(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "build-status.json"
            bs.write_status_atomic(path, bs.empty_status())
            leftovers = [p for p in Path(tmp).iterdir() if p.name != "build-status.json"]
        self.assertEqual([], leftovers)


class StateMachineTests(unittest.TestCase):
    """Item 93: queued -> testing -> a real outcome, written live at each
    step, plus resolving a "testing" an interrupted previous run left
    behind."""

    def test_queue_creates_a_fresh_record_in_state_queued(self) -> None:
        status, changed = bs.mark_queued(bs.empty_status(), "x")
        self.assertTrue(changed)
        self.assertEqual("queued", status["entries"]["x"]["state"])
        self.assertEqual([], status["entries"]["x"]["history"])

    def test_start_moves_queued_to_testing(self) -> None:
        status, _ = bs.mark_queued(bs.empty_status(), "x")
        status, changed = bs.mark_testing(status, "x")
        self.assertTrue(changed)
        self.assertEqual("testing", status["entries"]["x"]["state"])

    def test_start_without_a_prior_queue_still_works(self) -> None:
        status, changed = bs.mark_testing(bs.empty_status(), "x")
        self.assertTrue(changed)
        self.assertEqual("testing", status["entries"]["x"]["state"])

    def test_queue_after_a_real_outcome_keeps_since_and_history(self) -> None:
        status, _ = bs.apply_result(bs.empty_status(), "x", success_result("x"))
        since_before = status["entries"]["x"]["since"]
        history_before = status["entries"]["x"]["history"]
        status, changed = bs.mark_queued(status, "x")
        self.assertTrue(changed)
        self.assertEqual("queued", status["entries"]["x"]["state"])
        self.assertEqual(since_before, status["entries"]["x"]["since"])
        self.assertEqual(history_before, status["entries"]["x"]["history"])

    def test_re_queuing_an_already_queued_entry_is_not_a_change(self) -> None:
        status, _ = bs.mark_queued(bs.empty_status(), "x")
        _, changed = bs.mark_queued(status, "x")
        self.assertFalse(changed)

    def test_full_lifecycle_queued_testing_success(self) -> None:
        status, c1 = bs.mark_queued(bs.empty_status(), "x")
        status, c2 = bs.mark_testing(status, "x")
        status, c3 = bs.apply_result(status, "x", success_result("x"))
        self.assertTrue(c1 and c2 and c3)
        self.assertEqual("success", status["entries"]["x"]["state"])
        self.assertEqual(1, len(status["entries"]["x"]["history"]))

    def test_resolve_stuck_testing_turns_it_into_failed_with_an_explanation(self) -> None:
        status, _ = bs.mark_testing(bs.empty_status(), "x")
        status, touched = bs.resolve_stuck_testing(status)
        self.assertEqual(["x"], touched)
        record = status["entries"]["x"]
        self.assertEqual("failed", record["state"])
        self.assertEqual(bs.INTERRUPTED_ERROR, record["error"])
        self.assertEqual(1, len(record["history"]))
        self.assertEqual("failed", record["history"][-1]["state"])

    def test_resolve_stuck_testing_leaves_non_testing_entries_untouched(self) -> None:
        status, _ = bs.apply_result(bs.empty_status(), "a", success_result("a"))
        status, _ = bs.mark_queued(status, "b")
        status, touched = bs.resolve_stuck_testing(status)
        self.assertEqual([], touched)
        self.assertEqual("success", status["entries"]["a"]["state"])
        self.assertEqual("queued", status["entries"]["b"]["state"])

    def test_resolve_stuck_testing_with_nothing_stuck_is_a_no_op_reporting_no_ids(self) -> None:
        status = bs.empty_status()
        new_status, touched = bs.resolve_stuck_testing(status)
        self.assertEqual([], touched)

    def test_a_run_interrupted_while_testing_is_resolved_by_the_next_run_before_it_proceeds(self) -> None:
        """End-to-end simulation: run 1 queues and starts "x" then dies
        (never calls apply_result). Run 2 starts by resolving it, then
        proceeds normally - "x" must never be left "testing" forever, and
        must never silently become "success"."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "build-status.json"
            status, _ = bs.mark_queued(bs.load_status(path), "x")
            status, _ = bs.mark_testing(status, "x")
            bs.write_status_atomic(path, status)  # run 1 "dies" here

            status = bs.load_status(path)
            status, touched = bs.resolve_stuck_testing(status)
            bs.write_status_atomic(path, status)
            reloaded = bs.load_status(path)
        self.assertEqual(["x"], touched)
        self.assertEqual("failed", reloaded["entries"]["x"]["state"])


class SinceStreakTests(unittest.TestCase):
    """Item 94: since carries the start of the *current* state's streak,
    not merely the most recent attempt's date."""

    def test_first_ever_result_since_equals_its_own_date(self) -> None:
        status, _ = bs.apply_result(bs.empty_status(), "x", success_result("x", end="2026-01-01T00:00:00+00:00"))
        self.assertEqual("2026-01-01T00:00:00+00:00", status["entries"]["x"]["since"])

    def test_repeated_success_keeps_the_original_since_date(self) -> None:
        status, _ = bs.apply_result(bs.empty_status(), "x", success_result("x", end="2026-01-01T00:00:00+00:00"))
        status, _ = bs.apply_result(status, "x", success_result("x", end="2026-01-05T00:00:00+00:00"))
        status, _ = bs.apply_result(status, "x", success_result("x", end="2026-01-10T00:00:00+00:00"))
        self.assertEqual("2026-01-01T00:00:00+00:00", status["entries"]["x"]["since"])
        self.assertEqual("2026-01-10T00:00:00+00:00", status["entries"]["x"]["date"])

    def test_a_failing_streak_records_when_it_began_not_the_latest_attempt(self) -> None:
        status, _ = bs.apply_result(bs.empty_status(), "x", success_result("x", end="2026-01-01T00:00:00+00:00"))
        status, _ = bs.apply_result(status, "x", failed_result("x", end="2026-01-05T00:00:00+00:00"))
        status, _ = bs.apply_result(status, "x", failed_result("x", end="2026-01-06T00:00:00+00:00"))
        status, _ = bs.apply_result(status, "x", failed_result("x", end="2026-01-07T00:00:00+00:00"))
        self.assertEqual("2026-01-05T00:00:00+00:00", status["entries"]["x"]["since"])
        self.assertEqual("2026-01-07T00:00:00+00:00", status["entries"]["x"]["date"])

    def test_recovering_from_failure_resets_since_to_the_recovery_date(self) -> None:
        status, _ = bs.apply_result(bs.empty_status(), "x", failed_result("x", end="2026-01-01T00:00:00+00:00"))
        status, _ = bs.apply_result(status, "x", success_result("x", end="2026-01-08T00:00:00+00:00"))
        self.assertEqual("2026-01-08T00:00:00+00:00", status["entries"]["x"]["since"])

    def test_a_queued_testing_round_trip_between_two_successes_does_not_disturb_since(self) -> None:
        status, _ = bs.apply_result(bs.empty_status(), "x", success_result("x", end="2026-01-01T00:00:00+00:00"))
        status, _ = bs.mark_queued(status, "x")
        status, _ = bs.mark_testing(status, "x")
        status, _ = bs.apply_result(status, "x", success_result("x", end="2026-01-15T00:00:00+00:00"))
        self.assertEqual("2026-01-01T00:00:00+00:00", status["entries"]["x"]["since"])


class HistoryBoundAndOrderTests(unittest.TestCase):
    """Item 95: bounded, ordered, and each entry carries date/state/
    engine/base/suite/duration and, for a failure, stage and error."""

    def test_history_grows_with_each_real_outcome(self) -> None:
        status = bs.empty_status()
        for i in range(3):
            status, _ = bs.apply_result(status, "x", success_result("x", end=f"2026-01-0{i+1}T00:00:00+00:00"))
        self.assertEqual(3, len(status["entries"]["x"]["history"]))

    def test_history_is_bounded_to_the_limit(self) -> None:
        status = bs.empty_status()
        for i in range(bs.HISTORY_LIMIT + 5):
            status, _ = bs.apply_result(status, "x", success_result("x", end=f"2026-01-01T00:{i:02d}:00+00:00"))
        self.assertEqual(bs.HISTORY_LIMIT, len(status["entries"]["x"]["history"]))

    def test_history_keeps_the_most_recent_entries_oldest_first(self) -> None:
        status = bs.empty_status()
        for i in range(bs.HISTORY_LIMIT + 3):
            status, _ = bs.apply_result(status, "x", success_result("x", end=f"2026-01-01T00:{i:02d}:00+00:00"))
        history = status["entries"]["x"]["history"]
        self.assertEqual("2026-01-01T00:03:00+00:00", history[0]["date"])
        self.assertEqual(f"2026-01-01T00:{bs.HISTORY_LIMIT + 2:02d}:00+00:00", history[-1]["date"])

    def test_queued_and_testing_transitions_never_add_a_history_entry(self) -> None:
        status, _ = bs.apply_result(bs.empty_status(), "x", success_result("x"))
        status, _ = bs.mark_queued(status, "x")
        status, _ = bs.mark_testing(status, "x")
        self.assertEqual(1, len(status["entries"]["x"]["history"]))

    def test_a_failure_history_entry_carries_stage_and_error(self) -> None:
        status, _ = bs.apply_result(bs.empty_status(), "x", failed_result("x", stage="launcher"))
        entry = status["entries"]["x"]["history"][-1]
        self.assertEqual("launcher", entry["stage"])
        self.assertIsNotNone(entry["error"])
        self.assertEqual("failed", entry["state"])
        self.assertIn("engine", entry)
        self.assertIn("base", entry)
        self.assertIn("suite", entry)
        self.assertIn("duration_s", entry)

    def test_a_success_history_entry_has_a_null_stage_and_error(self) -> None:
        status, _ = bs.apply_result(bs.empty_status(), "x", success_result("x"))
        entry = status["entries"]["x"]["history"][-1]
        self.assertIsNone(entry["stage"])
        self.assertIsNone(entry["error"])

    def test_fifty_entries_each_with_a_full_history_stays_a_valid_and_reasonably_small_file(self) -> None:
        status = bs.empty_status()
        for n in range(50):
            entry_id = f"entry-{n}"
            for i in range(bs.HISTORY_LIMIT + 2):
                status, _ = bs.apply_result(status, entry_id,
                                            success_result(entry_id, end=f"2026-01-01T00:{i:02d}:00+00:00"))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "build-status.json"
            bs.write_status_atomic(path, status)
            size = path.stat().st_size
            reloaded = bs.load_status(path)
        self.assertEqual(50, len(reloaded["entries"]))
        for record in reloaded["entries"].values():
            self.assertEqual(bs.HISTORY_LIMIT, len(record["history"]))
        # Not a strict contract, just a sanity check that history bounding
        # actually keeps this well under a size nobody would want to fetch
        # on every page load (a few KB per entry at most, not fifty
        # unbounded histories).
        self.assertLess(size, 200_000)


class ApplyResultAccumulationTests(unittest.TestCase):
    """Item 72: written after every entry, carrying forward everything it
    is not replacing, so an interrupted run leaves a valid file."""

    def test_second_entry_does_not_erase_the_first(self) -> None:
        status = bs.empty_status()
        status, _ = bs.apply_result(status, "a", success_result("a"))
        status, _ = bs.apply_result(status, "b", success_result("b"))
        self.assertEqual({"a", "b"}, set(status["entries"]))

    def test_re_applying_the_same_id_replaces_only_that_one(self) -> None:
        status = bs.empty_status()
        status, _ = bs.apply_result(status, "a", success_result("a"))
        status, _ = bs.apply_result(status, "b", success_result("b"))
        status, _ = bs.apply_result(status, "a", failed_result("a"))
        self.assertEqual("failed", status["entries"]["a"]["state"])
        self.assertEqual("success", status["entries"]["b"]["state"])

    def test_an_interrupted_run_leaves_a_valid_file_with_only_the_finished_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "build-status.json"
            status = bs.load_status(path)
            for entry_id in ("a", "b"):  # "c" never runs - the simulated interruption
                status, _ = bs.apply_result(status, entry_id, success_result(entry_id))
                bs.write_status_atomic(path, status)
            reloaded = bs.load_status(path)
        self.assertEqual({"a", "b"}, set(reloaded["entries"]))
        self.assertNotIn("c", reloaded["entries"])

    def test_a_later_run_carries_forward_entries_it_does_not_touch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "build-status.json"
            status = bs.empty_status()
            for entry_id in ("a", "b", "c"):
                status, _ = bs.apply_result(status, entry_id, success_result(entry_id))
            bs.write_status_atomic(path, status)

            status = bs.load_status(path)
            status, _ = bs.apply_result(status, "b", failed_result("b"))
            bs.write_status_atomic(path, status)

            reloaded = bs.load_status(path)
        self.assertEqual("success", reloaded["entries"]["a"]["state"])
        self.assertEqual("failed", reloaded["entries"]["b"]["state"])
        self.assertEqual("success", reloaded["entries"]["c"]["state"])


class StateChangeDetectionTests(unittest.TestCase):
    def test_a_brand_new_id_counts_as_changed(self) -> None:
        _, changed = bs.apply_result(bs.empty_status(), "a", success_result("a"))
        self.assertTrue(changed)

    def test_the_same_state_again_is_not_a_change(self) -> None:
        status, _ = bs.apply_result(bs.empty_status(), "a", success_result("a", end="2026-09-24T16:00:00+00:00"))
        _, changed = bs.apply_result(status, "a", success_result("a", end="2026-09-24T17:00:00+00:00"))
        self.assertFalse(changed, "a re-build that stays success is not a state change, even with a newer date")

    def test_success_to_failed_is_a_change(self) -> None:
        status, _ = bs.apply_result(bs.empty_status(), "a", success_result("a"))
        _, changed = bs.apply_result(status, "a", failed_result("a"))
        self.assertTrue(changed)

    def test_failed_to_skipped_is_a_change(self) -> None:
        status, _ = bs.apply_result(bs.empty_status(), "a", failed_result("a"))
        _, changed = bs.apply_result(status, "a", skipped_result("a"))
        self.assertTrue(changed)


class StatusUpdaterTests(unittest.TestCase):
    def test_record_writes_the_file_and_returns_whether_it_changed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "build-status.json"
            updater = bs.StatusUpdater(path)
            changed_first = updater.record("a", success_result("a"))
            changed_second = updater.record("a", success_result("a"))
            self.assertTrue(path.is_file())
        self.assertTrue(changed_first)
        self.assertFalse(changed_second)

    def test_queue_then_start_then_record_each_write_and_each_may_notify(self) -> None:
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "build-status.json"
            updater = bs.StatusUpdater(path, on_change=lambda p: calls.append(p))
            updater.queue("a")
            updater.start("a")
            updater.record("a", success_result("a"))
        self.assertEqual(3, len(calls))

    def test_on_change_is_called_only_when_the_state_changed(self) -> None:
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "build-status.json"
            updater = bs.StatusUpdater(path, on_change=lambda p: calls.append(p))
            updater.record("a", success_result("a"))       # new -> changed
            updater.record("a", success_result("a"))       # same state -> not changed
            updater.record("a", failed_result("a"))         # changed
        self.assertEqual(2, len(calls))
        self.assertTrue(all(c == path for c in calls))

    def test_resolve_interrupted_returns_touched_ids_and_notifies_on_change(self) -> None:
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "build-status.json"
            updater = bs.StatusUpdater(path, on_change=lambda p: calls.append(p))
            updater.start("stuck")
            calls.clear()
            touched = updater.resolve_interrupted()
        self.assertEqual(["stuck"], touched)
        self.assertEqual(1, len(calls))

    def test_resolve_interrupted_with_nothing_stuck_notifies_nobody(self) -> None:
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "build-status.json"
            updater = bs.StatusUpdater(path, on_change=lambda p: calls.append(p))
            touched = updater.resolve_interrupted()
        self.assertEqual([], touched)
        self.assertEqual([], calls)

    def test_own_paths_and_hostname_are_threaded_into_recorded_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "build-status.json"
            updater = bs.StatusUpdater(path, own_paths=["/opt/synos-conformance"], hostname="runner-9")
            result = failed_result("x", log_tail=["boom on runner-9 under /opt/synos-conformance/work"])
            updater.record("x", result)
            status = bs.load_status(path)
        error = status["entries"]["x"]["error"]
        self.assertNotIn("runner-9", error)
        self.assertNotIn("/opt/synos-conformance", error)

    def test_concurrent_record_calls_from_many_threads_never_lose_an_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "build-status.json"
            updater = bs.StatusUpdater(path)
            ids = [f"entry-{i}" for i in range(20)]

            def worker(entry_id: str) -> None:
                updater.record(entry_id, success_result(entry_id))

            threads = [threading.Thread(target=worker, args=(i,)) for i in ids]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            final = bs.load_status(path)
        self.assertEqual(set(ids), set(final["entries"]))


if __name__ == "__main__":
    unittest.main()
