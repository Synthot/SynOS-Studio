"""tools/build_matrix.py: the planner, target selection, the report shape,
resumability and the disk guard. Every build in here is a fake process (a
small script standing in for tools/synos); nothing shells out to a real
build."""
from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load_module(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


build_matrix = load_module("build_matrix_under_test", "tools/build_matrix.py")


FAKE_SYNOS = textwrap.dedent("""\
    #!/usr/bin/env python3
    import json, os, pathlib, sys, time

    args = sys.argv[1:]

    def get(flag):
        return args[args.index(flag) + 1] if flag in args else None

    out, log = get("--output"), get("--log")
    mode = os.environ.get("FAKE_SYNOS_MODE", "success")
    if log:
        pathlib.Path(log).parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8") as handle:
            handle.write("+ fake build\\n")
            if mode == "fail":
                handle.write("simulated failure, line 1\\n")
                handle.write("simulated failure, line 2\\n")

    if mode == "hang":
        time.sleep(30)
        sys.exit(0)
    if mode == "fail":
        print(json.dumps({"ok": False, "exit_code": 3, "error": "simulated failure"}))
        sys.exit(3)

    iso = pathlib.Path(out) / "fake.iso"
    iso.parent.mkdir(parents=True, exist_ok=True)
    iso.write_bytes(b"FAKE-ISO-BYTES" * 4)
    print(json.dumps({"ok": True, "exit_code": 0, "iso": str(iso), "image": None, "engine": "local"}))
    sys.exit(0)
""")


class FakeSynos:
    """A stand-in for tools/synos: build_matrix.SYNOS is pointed at this
    script's path for the duration of the test, and restored after."""

    def __init__(self, mode: str = "success"):
        self.mode = mode
        self._tmpdir = tempfile.TemporaryDirectory(prefix="fake-synos-")
        self.path = Path(self._tmpdir.name) / "fake_synos.py"
        self.path.write_text(FAKE_SYNOS, encoding="utf-8")
        self.path.chmod(self.path.stat().st_mode | stat.S_IEXEC)

    def __enter__(self):
        self._old_synos = build_matrix.SYNOS
        self._old_env = os.environ.get("FAKE_SYNOS_MODE")
        build_matrix.SYNOS = self.path
        os.environ["FAKE_SYNOS_MODE"] = self.mode
        return self

    def __exit__(self, *exc):
        build_matrix.SYNOS = self._old_synos
        if self._old_env is None:
            os.environ.pop("FAKE_SYNOS_MODE", None)
        else:
            os.environ["FAKE_SYNOS_MODE"] = self._old_env
        self._tmpdir.cleanup()


def make_target(id="t1", kind="catalog", base="ubuntu", suite="noble", checksum="abc123") -> "build_matrix.Target":
    return build_matrix.Target(id=id, kind=kind, base=base, suite=suite, source=ROOT / "manifest.yml",
                               checksum=checksum, profile_id="workstation")


class PlannerTests(unittest.TestCase):
    def test_catalog_targets_match_the_index(self) -> None:
        import yaml
        index = yaml.safe_load((ROOT / "bundle-catalog" / "index.yml").read_text(encoding="utf-8"))
        entries = index["bundle_catalog"]
        targets = build_matrix.plan_catalog_targets(ROOT)
        self.assertEqual(sorted(e["id"] for e in entries), sorted(t.id for t in targets))
        self.assertTrue(all(t.kind == "catalog" for t in targets))
        self.assertTrue(all(t.checksum for t in targets))

    def test_core_targets_cover_every_base_and_supported_suite(self) -> None:
        targets = build_matrix.plan_core_targets(ROOT)
        ids = {t.id for t in targets}
        # Known from bases/*/base.env at the time this suite was written: debian
        # ships one suite, ubuntu three; a base.env edit changes this set, which
        # is the point (the planner reads it, this test does not hardcode it).
        expected = set()
        for base, suites in build_matrix._bases(ROOT):
            for suite in suites:
                expected.add(f"core-{base}-{suite}")
        self.assertEqual(expected, ids)
        self.assertTrue(all(t.kind == "core" for t in targets))
        self.assertTrue(all(t.source.is_file() for t in targets))

    def test_full_plan_is_catalog_plus_core_with_unique_ids(self) -> None:
        targets = build_matrix.plan_targets(ROOT)
        ids = [t.id for t in targets]
        self.assertEqual(len(ids), len(set(ids)), "target ids must be unique across catalog and core")
        catalog = [t for t in targets if t.kind == "catalog"]
        core = [t for t in targets if t.kind == "core"]
        self.assertEqual(len(targets), len(catalog) + len(core))

    def test_core_manifest_resolves_the_engines_default_profile(self) -> None:
        default_profile = build_matrix._default_profile(ROOT)
        targets = build_matrix.plan_core_targets(ROOT)
        for target in targets:
            data = build_matrix.render_manifest.load_yaml(target.source)
            self.assertEqual(default_profile, data["profile"])
            self.assertEqual(target.base, data["base"])
            self.assertEqual(target.suite, data["suite"])


class SelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.targets = [
            make_target("web-server-nginx", "catalog", "ubuntu", "noble"),
            make_target("git-server", "catalog", "ubuntu", "noble"),
            make_target("core-debian-trixie", "core", "debian", "trixie"),
            make_target("core-ubuntu-jammy", "core", "ubuntu", "jammy"),
        ]

    def test_only_narrows_to_named_ids(self) -> None:
        selected = build_matrix.select_targets(self.targets, only=["git-server"], base=None, suite=None, kind=None)
        self.assertEqual(["git-server"], [t.id for t in selected])

    def test_only_with_unknown_id_raises(self) -> None:
        with self.assertRaises(SystemExit):
            build_matrix.select_targets(self.targets, only=["does-not-exist"], base=None, suite=None, kind=None)

    def test_kind_filter(self) -> None:
        selected = build_matrix.select_targets(self.targets, only=None, base=None, suite=None, kind="core")
        self.assertEqual({"core-debian-trixie", "core-ubuntu-jammy"}, {t.id for t in selected})

    def test_base_and_suite_filters_combine(self) -> None:
        selected = build_matrix.select_targets(self.targets, only=None, base=["ubuntu"], suite=["jammy"], kind=None)
        self.assertEqual(["core-ubuntu-jammy"], [t.id for t in selected])


class ReportShapeTests(unittest.TestCase):
    def test_successful_build_report_shape(self) -> None:
        target = make_target("t1")
        with tempfile.TemporaryDirectory() as out_dir, FakeSynos("success"):
            record = build_matrix.run_one(target, output_dir=Path(out_dir), timeout_seconds=30,
                                          image=None, pull=False, run_smoke=False)
        expected_keys = {"id", "kind", "base", "suite", "source", "checksum", "profile", "engine", "start", "end",
                         "duration_s", "exit_code", "status", "iso", "log_path", "log_tail", "smoke"}
        self.assertEqual(expected_keys, set(record))
        self.assertEqual("success", record["status"])
        self.assertEqual(0, record["exit_code"])
        self.assertIsNotNone(record["iso"])
        self.assertEqual({"path", "size", "sha256"}, set(record["iso"]))
        self.assertEqual(64, len(record["iso"]["sha256"]))
        self.assertIsNone(record["smoke"])  # run_smoke=False
        self.assertGreaterEqual(record["duration_s"], 0)
        # The report must be JSON-serializable as-is (this is what report.json holds).
        json.dumps(record)

    def test_failing_build_is_recorded_with_log_tail_and_does_not_raise(self) -> None:
        target = make_target("t2")
        with tempfile.TemporaryDirectory() as out_dir, FakeSynos("fail"):
            record = build_matrix.run_one(target, output_dir=Path(out_dir), timeout_seconds=30,
                                          image=None, pull=False, run_smoke=False)
        self.assertEqual("build_failed", record["status"])
        self.assertEqual(3, record["exit_code"])
        self.assertIsNone(record["iso"])
        self.assertTrue(record["log_tail"], "a failed build must keep the tail of its log")
        self.assertTrue(any("simulated failure" in line for line in record["log_tail"]))

    def test_timeout_is_recorded_not_raised(self) -> None:
        target = make_target("t3")
        with tempfile.TemporaryDirectory() as out_dir, FakeSynos("hang"):
            record = build_matrix.run_one(target, output_dir=Path(out_dir), timeout_seconds=1,
                                          image=None, pull=False, run_smoke=False)
        self.assertEqual("timeout", record["status"])
        self.assertIsNone(record["iso"])


class ResumeTests(unittest.TestCase):
    def test_resume_skips_success_and_retries_everything_else(self) -> None:
        targets = [
            make_target("ok", checksum="same"),
            make_target("stale-checksum", checksum="new"),
            make_target("previously-failed", checksum="x"),
            make_target("never-run", checksum="y"),
        ]
        report = {"targets": {
            "ok": {"status": "success", "checksum": "same"},
            "stale-checksum": {"status": "success", "checksum": "old"},
            "previously-failed": {"status": "build_failed", "checksum": "x"},
        }}
        to_run = build_matrix.targets_to_run(targets, report, resume=True)
        self.assertEqual({"stale-checksum", "previously-failed", "never-run"}, {t.id for t in to_run})

    def test_without_resume_every_target_runs(self) -> None:
        targets = [make_target("a"), make_target("b")]
        report = {"targets": {"a": {"status": "success", "checksum": "abc123"}}}
        to_run = build_matrix.targets_to_run(targets, report, resume=False)
        self.assertEqual({"a", "b"}, {t.id for t in to_run})

    def test_a_full_run_writes_a_report_a_second_resumed_run_can_read(self) -> None:
        target = make_target("resumable")
        with tempfile.TemporaryDirectory() as out_dir:
            out_dir = Path(out_dir)
            report_path = out_dir / "report.json"
            with FakeSynos("success"):
                record = build_matrix.run_one(target, output_dir=out_dir, timeout_seconds=30,
                                              image=None, pull=False, run_smoke=False)
            report = {"targets": {"resumable": record}}
            build_matrix._write_report(report_path, report)
            reloaded = build_matrix._load_report(report_path)
            self.assertEqual("success", reloaded["targets"]["resumable"]["status"])
            to_run = build_matrix.targets_to_run([target], reloaded, resume=True)
            self.assertEqual([], to_run, "an unchanged, successful target must not be rebuilt on --resume")


class DiskGuardTests(unittest.TestCase):
    def test_refuses_when_free_space_is_short(self) -> None:
        class TinyDisk:
            free = 1 * 1024**3
            total = 2 * 1024**3
            used = 1 * 1024**3

        original = build_matrix.shutil.disk_usage
        build_matrix.shutil.disk_usage = lambda path: TinyDisk()
        try:
            problems = build_matrix.guard_host(1)
        finally:
            build_matrix.shutil.disk_usage = original
        self.assertTrue(any("GB free" in p or "GB is needed" in p for p in problems))

    def test_refuses_a_jobs_count_this_machine_has_no_cpus_for(self) -> None:
        problems = build_matrix.guard_host(10_000)
        self.assertTrue(any("--jobs" in p for p in problems))

    def test_refuses_zero_or_negative_jobs(self) -> None:
        problems = build_matrix.guard_host(0)
        self.assertTrue(any("--jobs" in p for p in problems))


class HumanSummaryTests(unittest.TestCase):
    def test_summary_table_has_one_row_per_target(self) -> None:
        report = {"targets": {
            "a": {"kind": "catalog", "status": "success", "duration_s": 12.3, "iso": {"size": 2 * 1024**3}},
            "b": {"kind": "core", "status": "build_failed", "duration_s": 4.0, "iso": None},
        }}
        text = build_matrix.human_summary(report)
        self.assertIn("a", text)
        self.assertIn("b", text)
        self.assertIn("success", text)
        self.assertIn("build_failed", text)


if __name__ == "__main__":
    unittest.main()
