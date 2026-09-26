"""tools/build_matrix.py: the planner, target selection, the report shape,
resumability and the disk guard. Every build in here is a fake process (a
small script standing in for tools/synos); nothing shells out to a real
build."""
from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
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
    """A stand-in for tools/synos: build_matrix._synos_for (called with the
    disposable scratch checkout run_one just created) is pointed at this
    script's path for the duration of the test, and restored after. A real
    scratch checkout is still created and removed around it (run_one does
    that regardless of which synos path it is handed), so these tests also
    exercise the isolation machinery itself, just not a real build inside it."""

    def __init__(self, mode: str = "success"):
        self.mode = mode
        self._tmpdir = tempfile.TemporaryDirectory(prefix="fake-synos-")
        self.path = Path(self._tmpdir.name) / "fake_synos.py"
        self.path.write_text(FAKE_SYNOS, encoding="utf-8")
        self.path.chmod(self.path.stat().st_mode | stat.S_IEXEC)

    def __enter__(self):
        self._old_synos_for = build_matrix._synos_for
        self._old_env = os.environ.get("FAKE_SYNOS_MODE")
        build_matrix._synos_for = lambda scratch_path: self.path
        os.environ["FAKE_SYNOS_MODE"] = self.mode
        return self

    def __exit__(self, *exc):
        build_matrix._synos_for = self._old_synos_for
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

    def test_core_targets_never_include_a_suite_that_cannot_back_a_live_image(self) -> None:
        """"core" builds the engine's own default profile on every suite a
        base supports, once each — the exact shape that hit the jammy bug: a
        suite in SUPPORTED_SUITES this engine cannot actually build. _bases()
        must already exclude it (bases/ubuntu/live.map), never build_matrix
        deciding this on its own."""
        ubuntu_suites = dict(build_matrix._bases(ROOT))["ubuntu"]
        self.assertNotIn("jammy", ubuntu_suites)
        self.assertIn("noble", ubuntu_suites)
        self.assertIn("resolute", ubuntu_suites)
        targets = build_matrix.plan_core_targets(ROOT)
        self.assertNotIn("core-ubuntu-jammy", {t.id for t in targets})

    def test_full_plan_is_catalog_plus_core_plus_saturation_with_unique_ids(self) -> None:
        targets = build_matrix.plan_targets(ROOT)
        ids = [t.id for t in targets]
        self.assertEqual(len(ids), len(set(ids)), "target ids must be unique across catalog, core and saturation")
        catalog = [t for t in targets if t.kind == "catalog"]
        core = [t for t in targets if t.kind == "core"]
        saturation = [t for t in targets if t.kind == "saturation"]
        self.assertEqual(len(targets), len(catalog) + len(core) + len(saturation))

    def test_core_manifest_resolves_the_engines_default_profile(self) -> None:
        default_profile = build_matrix._default_profile(ROOT)
        targets = build_matrix.plan_core_targets(ROOT)
        for target in targets:
            data = build_matrix.render_manifest.load_yaml(target.source)
            self.assertEqual(default_profile, data["profile"])
            self.assertEqual(target.base, data["base"])
            self.assertEqual(target.suite, data["suite"])

    def test_saturation_targets_cover_every_real_base_and_carry_their_own_generated_bundle(self) -> None:
        targets = build_matrix.plan_saturation_targets(ROOT)
        ids = {t.id for t in targets}
        self.assertEqual({f"saturation-{b}" for b in build_matrix.build_saturation.real_bases(ROOT)}, ids)
        for target in targets:
            self.assertEqual("saturation", target.kind)
            self.assertTrue((target.source / "bundle.json").is_file())
            self.assertTrue((target.source / "profiles" / f"{target.profile_id}.yml").is_file())
            self.assertTrue(target.checksum)

    def test_saturation_targets_are_never_under_bundle_catalog(self) -> None:
        bundle_catalog_dir = (ROOT / "bundle-catalog").resolve()
        for target in build_matrix.plan_saturation_targets(ROOT):
            resolved = target.source.resolve()
            self.assertNotEqual(bundle_catalog_dir, resolved)
            self.assertNotIn(bundle_catalog_dir, resolved.parents)


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


def _git_status(cwd: Path) -> str:
    return subprocess.run(["git", "status", "--porcelain"], cwd=cwd, capture_output=True, text=True, check=True).stdout


@contextlib.contextmanager
def _fake_container_store():
    """Skips host_resources.container_store's real `<engine> info` subprocess
    call (slow, and would need a genuinely runnable engine, not just one on
    PATH) for tests that only need resolve_jobs to succeed with *some*
    engine present — container_engine() (a cheap shutil.which) still runs
    for real, so this only helps on a machine that actually has podman or
    docker on PATH, same as the rest of this project already assumes."""
    original = build_matrix.host_resources.container_store
    build_matrix.host_resources.container_store = lambda engine, container_root=None: None
    try:
        yield
    finally:
        build_matrix.host_resources.container_store = original


def _git_worktree_list(cwd: Path) -> str:
    return subprocess.run(["git", "worktree", "list", "--porcelain"], cwd=cwd, capture_output=True, text=True, check=True).stdout


class IsolationTests(unittest.TestCase):
    """Pins the regression this class is named for: `tools/synos build
    <bundle>` applies the bundle's files into whatever checkout its own
    tools/synos script lives in. Before tools/scratch_checkout.py, run_one
    invoked *this* checkout's own tools/synos, so a real run wrote one
    profiles/<id>.yml per catalog entry straight into this checkout — dirty
    git status, and tests/unit/test_bundle_catalog.py's own guard against a
    catalog entry shadowing an engine profile then failing for everyone,
    not just whoever ran the matrix."""

    def test_the_planner_and_a_faked_build_over_a_real_catalog_entry_leave_this_checkout_unchanged(self) -> None:
        targets_by_id = {t.id: t for t in build_matrix.plan_catalog_targets(ROOT)}
        target = targets_by_id["web-server-nginx"]
        before_status = _git_status(ROOT)
        before_worktrees = _git_worktree_list(ROOT)

        with tempfile.TemporaryDirectory() as tmp, FakeSynos("success"):
            record = build_matrix.run_one(target, output_dir=Path(tmp), timeout_seconds=30,
                                          image=None, pull=False, run_smoke=False, root=ROOT)

        self.assertEqual("success", record["status"])
        self.assertEqual(before_status, _git_status(ROOT), "a matrix run must leave this checkout's git status exactly as it found it")
        self.assertEqual(before_worktrees, _git_worktree_list(ROOT), "the scratch worktree must be removed, not left registered")
        self.assertFalse((ROOT / "profiles" / "web-server-nginx.yml").exists(),
                         "web-server-nginx's profile must never land in this checkout's own profiles/")

    def test_a_real_bundle_apply_over_a_real_catalog_entry_lands_in_the_scratch_checkout_only(self) -> None:
        """The same isolation, without faking tools/synos at all: a real
        `bundle apply` (the part of `build` this regression is actually
        about) really writes profiles/web-server-nginx.yml — inside the
        scratch checkout it was run in, never here."""
        targets_by_id = {t.id: t for t in build_matrix.plan_catalog_targets(ROOT)}
        target = targets_by_id["web-server-nginx"]
        before_status = _git_status(ROOT)

        with tempfile.TemporaryDirectory() as tmp:
            scratch_path = build_matrix.scratch_checkout.create(ROOT, Path(tmp) / "scratch", label="isolation-test")
            try:
                build_source = build_matrix._materialize_source(target, ROOT, scratch_path)
                synos_path = build_matrix._synos_for(scratch_path)
                result = subprocess.run([sys.executable, str(synos_path), "--json", "bundle", "apply", str(build_source)],
                                        cwd=scratch_path, capture_output=True, text=True, check=False)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertTrue((scratch_path / "profiles" / "web-server-nginx.yml").is_file(),
                               "the real apply must have written the profile inside the scratch checkout")
                self.assertFalse((ROOT / "profiles" / "web-server-nginx.yml").exists(),
                                 "and must never have written it into this checkout")
            finally:
                build_matrix.scratch_checkout.remove(scratch_path)

        self.assertEqual(before_status, _git_status(ROOT))

    def test_a_real_bundle_apply_over_a_saturation_target_lands_in_the_scratch_checkout_only(self) -> None:
        """A saturation target's whole generated bundle folder lives under this
        checkout's own .build/ (git-ignored), which a fresh worktree of HEAD
        never carries — unlike a catalog entry's tracked folder. _materialize_source
        must copy the tree into the scratch checkout before `bundle apply` can
        find it there at all, and never write anything back here."""
        targets_by_id = {t.id: t for t in build_matrix.plan_saturation_targets(ROOT)}
        target = targets_by_id["saturation-ubuntu"]
        before_status = _git_status(ROOT)

        with tempfile.TemporaryDirectory() as tmp:
            scratch_path = build_matrix.scratch_checkout.create(ROOT, Path(tmp) / "scratch", label="saturation-isolation-test")
            try:
                build_source = build_matrix._materialize_source(target, ROOT, scratch_path)
                self.assertTrue((build_source / "bundle.json").is_file(),
                               "the generated bundle must have been copied into the scratch checkout")
                synos_path = build_matrix._synos_for(scratch_path)
                result = subprocess.run([sys.executable, str(synos_path), "--json", "bundle", "apply", str(build_source)],
                                        cwd=scratch_path, capture_output=True, text=True, check=False)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertTrue((scratch_path / "profiles" / "saturation-ubuntu.yml").is_file(),
                               "the real apply must have written the profile inside the scratch checkout")
                self.assertFalse((ROOT / "profiles" / "saturation-ubuntu.yml").exists(),
                                 "and must never have written it into this checkout's own tracked profiles/")
            finally:
                build_matrix.scratch_checkout.remove(scratch_path)

        self.assertEqual(before_status, _git_status(ROOT))

    def test_scratch_checkout_is_cleaned_up_even_when_the_build_raises(self) -> None:
        targets_by_id = {t.id: t for t in build_matrix.plan_catalog_targets(ROOT)}
        target = targets_by_id["web-server-nginx"]
        before_worktrees = _git_worktree_list(ROOT)

        def _boom(scratch_path):
            raise RuntimeError("simulated crash before the build even starts")

        old_synos_for = build_matrix._synos_for
        build_matrix._synos_for = _boom
        try:
            with tempfile.TemporaryDirectory() as tmp:
                record = build_matrix.run_one(target, output_dir=Path(tmp), timeout_seconds=30,
                                              image=None, pull=False, run_smoke=False, root=ROOT)
        finally:
            build_matrix._synos_for = old_synos_for

        self.assertEqual("error", record["status"])
        self.assertEqual(before_worktrees, _git_worktree_list(ROOT), "a crash mid-target must still remove its scratch worktree")

    def test_a_full_run_through_main_never_touches_the_tracked_build_status_file(self) -> None:
        """The regression that slipped past the tests above: run_one/run_targets_in_parallel
        never wrote bundle-catalog/build-status.yml — only main()'s own
        _on_result did, after every target — so an isolation test that only
        calls run_one directly never exercised that write at all. This one
        drives the real CLI entry point."""
        status_path = ROOT / "bundle-catalog" / "build-status.yml"
        before_status_bytes = status_path.read_bytes()
        before_git_status = _git_status(ROOT)

        with tempfile.TemporaryDirectory() as tmp, FakeSynos("success"), _fake_container_store():
            code = build_matrix.main(["--only", "web-server-nginx", "--jobs", "1", "--no-smoke", "--output", tmp])
            self.assertTrue((Path(tmp) / "build-status.yml").is_file(), "the run's own status copy must exist, beside report.json")

        self.assertEqual(0, code)
        self.assertEqual(before_status_bytes, status_path.read_bytes(),
                         "a run without --update-catalog-status-file must not modify the tracked status file")
        self.assertEqual(before_git_status, _git_status(ROOT), "nothing tracked may change after a default run")

    def test_update_catalog_status_file_writes_the_tracked_file_deliberately(self) -> None:
        import yaml
        status_path = ROOT / "bundle-catalog" / "build-status.yml"
        original_bytes = status_path.read_bytes()
        self.addCleanup(lambda: status_path.write_bytes(original_bytes))

        with tempfile.TemporaryDirectory() as tmp, FakeSynos("success"), _fake_container_store():
            code = build_matrix.main(["--only", "web-server-nginx", "--jobs", "1", "--no-smoke",
                                     "--output", tmp, "--update-catalog-status-file"])

        self.assertEqual(0, code)
        self.assertNotEqual(original_bytes, status_path.read_bytes(), "--update-catalog-status-file must actually write the tracked file")
        data = yaml.safe_load(status_path.read_text(encoding="utf-8"))
        self.assertIn("web-server-nginx", data["build_status"])
        self.assertEqual("success", data["build_status"]["web-server-nginx"]["result"])


class BuildStatusDataTests(unittest.TestCase):
    """build_status_data/write_build_status directly: pure functions, no
    real build, no file this repository tracks touched by any test here."""

    def test_only_catalog_targets_contribute(self) -> None:
        targets_by_id = {
            "web-server-nginx": make_target("web-server-nginx", kind="catalog"),
            "core-ubuntu-noble": make_target("core-ubuntu-noble", kind="core"),
        }
        report = {"targets": {
            "web-server-nginx": {"status": "success", "engine": "0.2.0", "start": "t0", "end": "t1", "duration_s": 1.0, "iso": None},
            "core-ubuntu-noble": {"status": "success", "engine": "0.2.0", "start": "t0", "end": "t1", "duration_s": 1.0, "iso": None},
        }}
        data = build_matrix.build_status_data(targets_by_id, report)
        self.assertEqual(["web-server-nginx"], list(data))

    def test_a_target_with_no_record_yet_contributes_nothing(self) -> None:
        targets_by_id = {"web-server-nginx": make_target("web-server-nginx", kind="catalog")}
        data = build_matrix.build_status_data(targets_by_id, {"targets": {}})
        self.assertEqual({}, data)

    def test_a_still_running_target_contributes_nothing(self) -> None:
        targets_by_id = {"web-server-nginx": make_target("web-server-nginx", kind="catalog")}
        report = {"targets": {"web-server-nginx": {"status": "running", "engine": "0.2.0", "start": "t0", "end": None, "duration_s": None, "iso": None}}}
        data = build_matrix.build_status_data(targets_by_id, report)
        self.assertEqual({}, data)

    def test_iso_sha256_and_smoke_are_included_when_present(self) -> None:
        targets_by_id = {"web-server-nginx": make_target("web-server-nginx", kind="catalog")}
        report = {"targets": {"web-server-nginx": {
            "status": "success", "engine": "0.2.0", "start": "t0", "end": "t1", "duration_s": 5.0,
            "iso": {"sha256": "abc123"}, "smoke": {"status": "passed"},
        }}}
        data = build_matrix.build_status_data(targets_by_id, report)
        self.assertEqual("abc123", data["web-server-nginx"]["iso_sha256"])
        self.assertEqual("passed", data["web-server-nginx"]["smoke"])

    def test_write_build_status_without_merge_replaces_the_file_exactly(self) -> None:
        import yaml
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "build-status.yml"
            build_matrix.write_build_status(path, {"a": {"result": "success"}}, merge_existing=False)
            build_matrix.write_build_status(path, {"b": {"result": "success"}}, merge_existing=False)
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        self.assertEqual({"b": {"result": "success"}}, data["build_status"], "without merge_existing, only the latest call's data survives")

    def test_write_build_status_with_merge_preserves_untouched_ids(self) -> None:
        import yaml
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "build-status.yml"
            build_matrix.write_build_status(path, {"a": {"result": "success"}}, merge_existing=True)
            build_matrix.write_build_status(path, {"b": {"result": "success"}}, merge_existing=True)
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        self.assertEqual({"a": {"result": "success"}, "b": {"result": "success"}}, data["build_status"],
                         "merge_existing must keep an id an --only run did not touch")

    def test_write_build_status_with_merge_overwrites_a_changed_id(self) -> None:
        import yaml
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "build-status.yml"
            build_matrix.write_build_status(path, {"a": {"result": "build_failed"}}, merge_existing=True)
            build_matrix.write_build_status(path, {"a": {"result": "success"}}, merge_existing=True)
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        self.assertEqual("success", data["build_status"]["a"]["result"])


class ParallelismTests(unittest.TestCase):
    """tools/job_queue.py drives the real scheduling (tests/unit/test_job_queue.py
    covers that in isolation); these prove the wiring in run_targets_in_parallel
    itself: each worker gets its own scratch checkout, reuses it across every
    target that worker is handed, and the checkout this runs from stays
    exactly as it was found — no test here shells out to a real build."""

    def _catalog_targets(self, ids: list[str]) -> list["build_matrix.Target"]:
        by_id = {t.id: t for t in build_matrix.plan_catalog_targets(ROOT)}
        return [by_id[i] for i in ids]

    def test_each_worker_gets_its_own_scratch_checkout(self) -> None:
        targets = self._catalog_targets(["web-server-nginx", "web-server-apache", "web-server-caddy", "git-server"])
        seen: list[str] = []
        seen_lock = threading.Lock()

        with FakeSynos("success") as fake:
            def recording_synos_for(scratch_path, _fake_path=fake.path):
                with seen_lock:
                    seen.append(str(scratch_path))
                return _fake_path

            old = build_matrix._synos_for
            build_matrix._synos_for = recording_synos_for
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    results = build_matrix.run_targets_in_parallel(
                        targets, output_dir=Path(tmp), timeout_seconds=30, image=None, pull=False,
                        run_smoke=False, jobs=4, root=ROOT)
            finally:
                build_matrix._synos_for = old

        self.assertEqual(4, len(results))
        self.assertTrue(all(r["status"] == "success" for r in results.values()))
        scratch_paths_used = set(seen)
        self.assertGreaterEqual(len(scratch_paths_used), 2,
                                "four targets across four workers must use more than one scratch checkout")
        self.assertLessEqual(len(scratch_paths_used), 4, "no more scratch checkouts than workers")

    def test_a_single_worker_reuses_its_scratch_checkout_across_targets(self) -> None:
        targets = self._catalog_targets(["web-server-nginx", "web-server-apache"])
        scratch_paths_used: list[str] = []

        with FakeSynos("success") as fake:
            def recording_synos_for(scratch_path, _fake_path=fake.path):
                scratch_paths_used.append(str(scratch_path))
                return _fake_path

            old = build_matrix._synos_for
            build_matrix._synos_for = recording_synos_for
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    results = build_matrix.run_targets_in_parallel(
                        targets, output_dir=Path(tmp), timeout_seconds=30, image=None, pull=False,
                        run_smoke=False, jobs=1, root=ROOT)
            finally:
                build_matrix._synos_for = old

        self.assertEqual(2, len(results))
        self.assertEqual(1, len(set(scratch_paths_used)), "one worker must reuse the same scratch checkout for both targets")

    def test_a_parallel_run_leaves_this_checkout_unchanged_with_no_worktrees_left(self) -> None:
        targets = self._catalog_targets(["web-server-nginx", "web-server-apache", "web-server-caddy"])
        before_status = _git_status(ROOT)
        before_worktrees = _git_worktree_list(ROOT)

        with tempfile.TemporaryDirectory() as tmp, FakeSynos("success"):
            results = build_matrix.run_targets_in_parallel(
                targets, output_dir=Path(tmp), timeout_seconds=30, image=None, pull=False,
                run_smoke=False, jobs=3, root=ROOT)

        self.assertEqual(3, len(results))
        self.assertEqual(before_status, _git_status(ROOT))
        self.assertEqual(before_worktrees, _git_worktree_list(ROOT))

    def test_resolve_jobs_auto_is_wired_to_host_resources(self) -> None:
        jobs, problems = build_matrix.resolve_jobs("auto")
        self.assertEqual([], problems)
        self.assertGreaterEqual(jobs, 0)


class ContainerRootPassthroughTests(unittest.TestCase):
    """--container-root/--container-runroot (SYNOS_CONTAINER_ROOT/SYNOS_CONTAINER_RUNROOT):
    every target's `tools/synos build` receives the same setting, and
    resolve_jobs measures free disk there. subprocess.run is faked here, so
    no scratch checkout or real synos process is ever started."""

    class _FakeCompleted:
        returncode = 0
        stdout = "{}"
        stderr = ""

    def _capture_argv(self, target, **extra) -> list[str]:
        calls: list[list[str]] = []
        original = build_matrix.subprocess.run

        def fake_run(argv, **kwargs):
            calls.append(list(argv))
            return self._FakeCompleted()

        build_matrix.subprocess.run = fake_run
        try:
            with tempfile.TemporaryDirectory() as tmp:
                tmp = Path(tmp)
                build_matrix.run_one(target, output_dir=tmp / "out", timeout_seconds=60, image=None, pull=False,
                                     run_smoke=False, root=ROOT, scratch_path=tmp / "scratch", **extra)
        finally:
            build_matrix.subprocess.run = original
        self.assertEqual(1, len(calls))
        return calls[0]

    def test_both_flags_reach_the_synos_build_invocation(self) -> None:
        argv = self._capture_argv(make_target(), container_root="/mnt/big/synos-storage",
                                  container_runroot="/mnt/big/synos-runroot")
        self.assertIn("--container-root", argv)
        self.assertEqual("/mnt/big/synos-storage", argv[argv.index("--container-root") + 1])
        self.assertIn("--container-runroot", argv)
        self.assertEqual("/mnt/big/synos-runroot", argv[argv.index("--container-runroot") + 1])

    def test_an_unset_container_root_omits_both_flags(self) -> None:
        argv = self._capture_argv(make_target())
        self.assertNotIn("--container-root", argv)
        self.assertNotIn("--container-runroot", argv)

    def test_resolve_jobs_passes_container_root_to_host_resources(self) -> None:
        original = build_matrix.host_resources.resolve_jobs
        captured: dict = {}

        def fake_resolve_jobs(requested, root, **kwargs):
            captured.update(kwargs)
            return 2, []

        build_matrix.host_resources.resolve_jobs = fake_resolve_jobs
        try:
            build_matrix.resolve_jobs("auto", "/mnt/big/synos-storage")
        finally:
            build_matrix.host_resources.resolve_jobs = original
        self.assertEqual("/mnt/big/synos-storage", captured.get("container_root"))

    def test_resolve_jobs_with_no_container_root_passes_none(self) -> None:
        original = build_matrix.host_resources.resolve_jobs
        captured: dict = {}

        def fake_resolve_jobs(requested, root, **kwargs):
            captured.update(kwargs)
            return 2, []

        build_matrix.host_resources.resolve_jobs = fake_resolve_jobs
        try:
            build_matrix.resolve_jobs("auto")
        finally:
            build_matrix.host_resources.resolve_jobs = original
        self.assertIsNone(captured.get("container_root"))


class DiskGuardTests(unittest.TestCase):
    """build_matrix.resolve_jobs itself; the numbers behind it (disk, memory,
    CPU derivation) are tested directly in tests/unit/test_host_resources.py."""

    def test_refuses_when_free_space_is_short(self) -> None:
        class TinyDisk:
            free = 1 * 1024**3
            total = 2 * 1024**3
            used = 1 * 1024**3

        original = build_matrix.host_resources.shutil.disk_usage
        build_matrix.host_resources.shutil.disk_usage = lambda path: TinyDisk()
        try:
            jobs, problems = build_matrix.resolve_jobs("1")
        finally:
            build_matrix.host_resources.shutil.disk_usage = original
        self.assertTrue(problems, "1 GB free must not be enough for even one build")

    def test_refuses_an_explicit_count_above_the_derived_safe_maximum(self) -> None:
        jobs, problems = build_matrix.resolve_jobs("10000")
        self.assertTrue(any("--jobs" in p for p in problems))

    def test_refuses_zero_or_negative_jobs(self) -> None:
        jobs, problems = build_matrix.resolve_jobs("0")
        self.assertTrue(any("--jobs" in p for p in problems))

    def test_auto_never_needs_a_reason(self) -> None:
        jobs, problems = build_matrix.resolve_jobs("auto")
        self.assertEqual([], problems)
        self.assertGreaterEqual(jobs, 1)


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
