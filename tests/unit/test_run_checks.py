"""tools/run_checks.py: stage selection, ordering, the skip/fail distinction,
--fail-fast, log naming and the --json shape — all against fake stages or a
temporary log directory, never the real unit suite or a real bundle-catalog
loop (those are exercised, once, by hand: `python3 tools/run_checks.py
--level fast`)."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]


def load_module(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module  # dataclasses (Stage/Outcome/Context) need this to resolve annotations
    spec.loader.exec_module(module)
    return module


rc = load_module("run_checks_under_test", "tools/run_checks.py")


def make_stage(name, level, status, *, note=None, precheck_status=None, calls=None):
    """A fake Stage whose run() (and, when precheck_status is given, whose
    precheck()) never touches a subprocess or the real checkout — just
    records that it ran and returns a canned Outcome."""
    calls = calls if calls is not None else []

    def run(ctx):
        calls.append(name)
        return rc.Outcome(status=status, stdout=f"stdout of {name}", stderr=f"stderr of {name}" if status == "failed" else "",
                          note=note, command=f"<fake {name}>", cwd=ctx.root)

    precheck = None
    if precheck_status is not None:
        def precheck(ctx):
            return rc.Outcome(status=precheck_status, note=note or f"precheck skipped {name}")

    return rc.Stage(name=name, description=f"fake stage {name}", level=level, run=run, precheck=precheck)


def make_context(log_dir: Path) -> "rc.Context":
    return rc.Context(root=ROOT, studio_repo=None, log_dir=log_dir)


class StageSelectionTests(unittest.TestCase):
    def setUp(self):
        self.a = make_stage("a", "fast", "passed")
        self.b = make_stage("b", "unit", "passed")
        self.c = make_stage("c", "all", "passed")
        self.d = make_stage("d", None, "passed")  # only reachable via --only
        self.fakes = [self.a, self.b, self.c, self.d]
        self.patches = [mock.patch.object(rc, "STAGES", self.fakes),
                        mock.patch.object(rc, "STAGES_BY_NAME", {s.name: s for s in self.fakes})]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def test_fast_level_selects_only_fast(self):
        self.assertEqual(["a"], [s.name for s in rc.stages_for_level("fast")])

    def test_unit_level_selects_fast_and_unit(self):
        self.assertEqual(["a", "b"], [s.name for s in rc.stages_for_level("unit")])

    def test_all_level_selects_fast_unit_and_all_but_never_the_only_only_stage(self):
        self.assertEqual(["a", "b", "c"], [s.name for s in rc.stages_for_level("all")])

    def test_only_only_stage_is_never_selected_by_any_level(self):
        for level in ("fast", "unit", "all"):
            names = [s.name for s in rc.stages_for_level(level)]
            self.assertNotIn("d", names, f"'d' (level=None) must never be selected by --level {level}")

    def test_only_unknown_name_raises(self):
        with self.assertRaises(SystemExit) as cm:
            rc.select_stages(level="unit", only=["bogus"])
        self.assertIn("bogus", str(cm.exception))

    def test_only_reaches_the_only_only_stage(self):
        selected = rc.select_stages(level="fast", only=["d"])
        self.assertEqual(["d"], [s.name for s in selected])

    def test_only_preserves_declared_order_regardless_of_input_order(self):
        selected = rc.select_stages(level="fast", only=["c", "a", "b"])
        self.assertEqual(["a", "b", "c"], [s.name for s in selected])

    def test_only_with_one_unknown_among_known_still_raises(self):
        with self.assertRaises(SystemExit):
            rc.select_stages(level="fast", only=["a", "nope"])


class RunStagesTests(unittest.TestCase):
    def test_stages_run_sequentially_in_order(self):
        calls: list[str] = []
        stages = [make_stage(n, "fast", "passed", calls=calls) for n in ("x", "y", "z")]
        with tempfile.TemporaryDirectory() as tmp:
            ctx = make_context(Path(tmp))
            records = rc.run_stages(ctx, stages, fail_fast=False)
        self.assertEqual(["x", "y", "z"], calls)
        self.assertEqual(["x", "y", "z"], [r["name"] for r in records])

    def test_a_failing_stage_makes_the_run_fail(self):
        calls: list[str] = []
        stages = [make_stage("ok", "fast", "passed", calls=calls),
                 make_stage("broken", "fast", "failed", calls=calls)]
        with tempfile.TemporaryDirectory() as tmp:
            ctx = make_context(Path(tmp))
            records = rc.run_stages(ctx, stages, fail_fast=False)
        self.assertFalse(rc.overall_ok(records))

    def test_a_skipped_stage_does_not_make_the_run_fail(self):
        calls: list[str] = []
        stages = [make_stage("ok", "fast", "passed", calls=calls),
                 make_stage("absent-tool", "fast", "skipped", note="no such tool", calls=calls)]
        with tempfile.TemporaryDirectory() as tmp:
            ctx = make_context(Path(tmp))
            records = rc.run_stages(ctx, stages, fail_fast=False)
        self.assertTrue(rc.overall_ok(records))
        self.assertEqual("skipped", records[1]["status"])

    def test_fail_fast_stops_before_the_next_stage_even_runs(self):
        calls: list[str] = []
        stages = [make_stage("first", "fast", "failed", calls=calls),
                 make_stage("second", "fast", "passed", calls=calls),
                 make_stage("third", "fast", "passed", calls=calls)]
        with tempfile.TemporaryDirectory() as tmp:
            ctx = make_context(Path(tmp))
            records = rc.run_stages(ctx, stages, fail_fast=True)
        self.assertEqual(["first"], calls, "a later stage must not even be invoked once --fail-fast has tripped")
        self.assertEqual(["first"], [r["name"] for r in records])

    def test_without_fail_fast_every_stage_still_runs_after_a_failure(self):
        calls: list[str] = []
        stages = [make_stage("first", "fast", "failed", calls=calls),
                 make_stage("second", "fast", "passed", calls=calls)]
        with tempfile.TemporaryDirectory() as tmp:
            ctx = make_context(Path(tmp))
            records = rc.run_stages(ctx, stages, fail_fast=False)
        self.assertEqual(["first", "second"], calls)
        self.assertEqual(["first", "second"], [r["name"] for r in records])

    def test_precheck_skip_short_circuits_run(self):
        calls: list[str] = []
        stage = make_stage("skippable", "fast", "passed", precheck_status="skipped", calls=calls)
        with tempfile.TemporaryDirectory() as tmp:
            ctx = make_context(Path(tmp))
            record = rc.run_stage(ctx, stage)
        self.assertEqual([], calls, "run() must not execute once precheck already decided the outcome")
        self.assertEqual("skipped", record["status"])

    def test_precheck_fail_short_circuits_run_and_fails_the_stage(self):
        """A broken checkout (e.g. a --studio-repo that does not look like
        one) must FAIL, not skip — the distinction the brief insists on."""
        calls: list[str] = []
        stage = make_stage("broken-checkout", "fast", "passed", precheck_status="failed",
                           note="does not look like a checkout", calls=calls)
        with tempfile.TemporaryDirectory() as tmp:
            ctx = make_context(Path(tmp))
            record = rc.run_stage(ctx, stage)
        self.assertEqual([], calls)
        self.assertEqual("failed", record["status"])
        self.assertFalse(rc.overall_ok([record]))

    def test_no_precheck_runs_normally(self):
        calls: list[str] = []
        stage = make_stage("plain", "fast", "passed", calls=calls)
        with tempfile.TemporaryDirectory() as tmp:
            ctx = make_context(Path(tmp))
            rc.run_stage(ctx, stage)
        self.assertEqual(["plain"], calls)


class LogFileTests(unittest.TestCase):
    def test_log_file_is_written_and_named_after_the_stage(self):
        stage = make_stage("logged-one", "fast", "passed")
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp)
            ctx = make_context(log_dir)
            record = rc.run_stage(ctx, stage)
            expected = log_dir / "logged-one.log"
            self.assertEqual(str(expected), record["log_path"])
            self.assertTrue(expected.is_file())
            text = expected.read_text()
        self.assertIn("stdout of logged-one", text)
        self.assertIn("status: passed", text)

    def test_failed_stage_log_includes_stderr(self):
        stage = make_stage("failing-one", "fast", "failed")
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp)
            ctx = make_context(log_dir)
            rc.run_stage(ctx, stage)
            text = (log_dir / "failing-one.log").read_text()
        self.assertIn("stderr of failing-one", text)
        self.assertIn("status: failed", text)

    def test_each_stage_gets_its_own_log_file(self):
        stages = [make_stage(n, "fast", "passed") for n in ("one", "two", "three")]
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp)
            ctx = make_context(log_dir)
            rc.run_stages(ctx, stages, fail_fast=False)
            names = sorted(p.name for p in log_dir.iterdir())
        self.assertEqual(["one.log", "three.log", "two.log"], names)

    def test_failed_stage_record_carries_a_tail_and_the_log_path(self):
        stage = make_stage("tail-test", "fast", "failed")
        with tempfile.TemporaryDirectory() as tmp:
            ctx = make_context(Path(tmp))
            record = rc.run_stage(ctx, stage)
            self.assertTrue(Path(record["log_path"]).is_file())
        self.assertTrue(record["tail"], "a failed stage's record must carry something to show in the short summary")


class JsonShapeTests(unittest.TestCase):
    def test_only_and_json_together_via_main(self):
        stages = [make_stage("j-ok", "fast", "passed"), make_stage("j-skip", "fast", "skipped", note="no tool")]
        fakes_by_name = {s.name: s for s in stages}
        with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(rc, "STAGES", stages), \
            mock.patch.object(rc, "STAGES_BY_NAME", fakes_by_name):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                exit_code = rc.main(["--only", "j-ok,j-skip", "--json", "--log-dir", tmp])
        payload = json.loads(buf.getvalue())
        self.assertEqual(0, exit_code)
        self.assertTrue(payload["ok"])
        self.assertEqual(["j-ok", "j-skip"], [s["name"] for s in payload["stages"]])
        for key in ("name", "level", "status", "duration_s", "log_path", "note", "tail"):
            self.assertIn(key, payload["stages"][0])

    def test_json_reports_failure_and_nonzero_exit(self):
        stages = [make_stage("j-bad", "fast", "failed")]
        with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(rc, "STAGES", stages), \
            mock.patch.object(rc, "STAGES_BY_NAME", {"j-bad": stages[0]}):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                exit_code = rc.main(["--only", "j-bad", "--json", "--log-dir", tmp])
        payload = json.loads(buf.getvalue())
        self.assertEqual(rc.EXIT_FAILURES, exit_code)
        self.assertFalse(payload["ok"])
        self.assertEqual("failed", payload["stages"][0]["status"])


class ListTests(unittest.TestCase):
    def test_list_prints_every_real_stage_without_running_any(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            exit_code = rc.main(["--list"])
        out = buf.getvalue()
        self.assertEqual(rc.EXIT_OK, exit_code)
        for stage in rc.STAGES:
            self.assertIn(stage.name, out)

    def test_only_with_a_real_unknown_name_via_main_raises(self):
        with self.assertRaises(SystemExit):
            rc.main(["--only", "not-a-real-stage-name"])


if __name__ == "__main__":
    unittest.main()
