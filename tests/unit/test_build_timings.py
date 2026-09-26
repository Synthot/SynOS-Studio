"""Phase-timing instrumentation: shared.sh's record_phase_timing, the per-mod
timing in mods/install_all_mods.sh, the handful of new pieces in build.sh
(collect_mod_timings, clean()'s ledger reset, report_build_timings), and
tools/build_timings.py, which turns the ledger into dist/<name>.timings.json
and the "longest first" summary printed at the end of a build.

No real build runs here (that needs an hour and root); instead, fake phases
drive the real shell functions and the real python module, the same way
test_checksum.py's extract_function runs the real write_iso_checksum body
and test_stack_install.py's harness sources the real shared.sh."""
from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SHARED_SH = ROOT / "shared.sh"
BUILD_SH = ROOT / "build.sh"
INSTALL_ALL_MODS_SH = ROOT / "mods" / "install_all_mods.sh"

_SPEC = importlib.util.spec_from_file_location("build_timings", ROOT / "tools" / "build_timings.py")
build_timings = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(build_timings)


def extract_function(source: str, name: str) -> str:
    """Pulls a `function <name>() { ... }` block out of a shell script by
    matching braces, so a test runs the real build.sh code, not a copy
    (same helper as test_checksum.py's, kept local as that one is)."""
    start = source.index(f"function {name}()")
    body_start = source.index("{", start)
    depth = 0
    for index in range(body_start, len(source)):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError(f"unterminated function {name}() in build.sh")


def _write_executable(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/bash\n{body}", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class BuildTimingsModuleTests(unittest.TestCase):
    """tools/build_timings.py's own functions, exercised directly."""

    def test_reset_then_append_writes_one_json_object_per_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sub" / "timings.jsonl"
            build_timings.reset_timings(path)
            self.assertEqual("", path.read_text(encoding="utf-8"))
            build_timings.append_phase(path, "Download base system", 12.4)
            build_timings.append_phase(path, "Compress rootfs", 300)
            entries = _read_jsonl(path)
            self.assertEqual([
                {"phase": "Download base system", "seconds": 12},
                {"phase": "Compress rootfs", "seconds": 300},
            ], entries)

    def test_reset_discards_whatever_a_previous_run_left(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "timings.jsonl"
            build_timings.append_phase(path, "stale from a crashed run", 999)
            build_timings.reset_timings(path)
            self.assertEqual([], build_timings.read_phases(path))

    def test_read_phases_skips_malformed_or_partial_lines(self) -> None:
        """A build that dies mid-write of one line must not lose every
        phase recorded before it -- only the broken line is dropped."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "timings.jsonl"
            path.write_text(
                '{"phase": "Download base system", "seconds": 40}\n'
                '{"phase": "Compress rootfs", "sec\n'  # truncated mid-write
                '{"not_a_phase_line": true}\n'
                '\n'
                '{"phase": "Create iso image", "seconds": 20}\n',
                encoding="utf-8",
            )
            self.assertEqual(
                [("Download base system", 40), ("Create iso image", 20)],
                build_timings.read_phases(path),
            )

    def test_read_phases_of_a_missing_file_is_empty_not_an_error(self) -> None:
        self.assertEqual([], build_timings.read_phases(Path("/no/such/file.jsonl")))

    def test_ranked_summary_is_longest_first_with_shares_of_the_total(self) -> None:
        phases = [("a", 10), ("b", 70), ("c", 20)]
        ranked, total = build_timings.ranked_summary(phases)
        self.assertEqual(100, total)
        self.assertEqual(["b", "c", "a"], [entry["phase"] for entry in ranked])
        self.assertEqual([0.7, 0.2, 0.1], [entry["share"] for entry in ranked])

    def test_ranked_summary_of_nothing_recorded_does_not_divide_by_zero(self) -> None:
        ranked, total = build_timings.ranked_summary([])
        self.assertEqual(0, total)
        self.assertEqual([], ranked)

    def test_summary_lines_lead_with_the_slowest_phase(self) -> None:
        ranked, total = build_timings.ranked_summary([("fast", 1), ("slow", 99)])
        lines = build_timings.format_summary_lines(ranked, total)
        self.assertIn("slow", lines[1])
        self.assertIn("fast", lines[2])


class BuildTimingsCliTests(unittest.TestCase):
    """The CLI build.sh actually shells out to."""

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            (sys.executable, str(ROOT / "tools" / "build_timings.py"), *args),
            text=True, capture_output=True, check=False,
        )

    def test_record_then_summarize_writes_the_evidence_file_next_to_the_iso(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "timings.jsonl"
            self.assertEqual(0, self._run("reset", "--file", str(ledger)).returncode)
            self.assertEqual(0, self._run("record", "--file", str(ledger), "--phase", "Compress rootfs", "--seconds", "50").returncode)
            self.assertEqual(0, self._run("record", "--file", str(ledger), "--phase", "Create iso image", "--seconds", "25").returncode)
            stem = Path(tmp) / "dist" / "SynOS-1.0.0-ubuntu-noble-2609251200-amd64"
            result = self._run("summarize", "--file", str(ledger), "--stem", str(stem))
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("SynOS-1.0.0-ubuntu-noble-2609251200-amd64.timings.json", result.stdout)
            # longest phase printed first, sharing wording with the log's own voice
            lines = result.stdout.splitlines()
            self.assertIn("Compress rootfs", lines[1])
            evidence = json.loads((stem.parent / (stem.name + ".timings.json")).read_text(encoding="utf-8"))
            self.assertEqual(75, evidence["total_seconds"])
            self.assertEqual("Compress rootfs", evidence["phases"][0]["phase"])
            self.assertAlmostEqual(50 / 75, evidence["phases"][0]["share"], places=3)

    def test_summarize_of_an_empty_ledger_still_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "timings.jsonl"  # never created -- e.g. packages step never ran
            stem = Path(tmp) / "dist" / "out"
            result = self._run("summarize", "--file", str(ledger), "--stem", str(stem))
            self.assertEqual(0, result.returncode, result.stderr)
            evidence = json.loads((stem.parent / "out.timings.json").read_text(encoding="utf-8"))
            self.assertEqual(0, evidence["total_seconds"])
            self.assertEqual([], evidence["phases"])


class RecordPhaseTimingShellTests(unittest.TestCase):
    """shared.sh's record_phase_timing, sourced and called for real."""

    def _run(self, body: str) -> subprocess.CompletedProcess[str]:
        script = f'source "{SHARED_SH}"\n{body}\n'
        return subprocess.run(["bash", "-c", script], text=True, capture_output=True, check=False)

    def test_appends_one_compact_json_line_and_creates_missing_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "does" / "not" / "exist" / "timings.jsonl"
            result = self._run(f'record_phase_timing "{ledger}" "Download base system" 42')
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual('{"phase": "Download base system", "seconds": 42}\n', ledger.read_text(encoding="utf-8"))

    def test_two_calls_append_rather_than_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "timings.jsonl"
            result = self._run(
                f'record_phase_timing "{ledger}" "Download base system" 1\n'
                f'record_phase_timing "{ledger}" "Compress rootfs" 2\n'
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(
                ['{"phase": "Download base system", "seconds": 1}',
                 '{"phase": "Compress rootfs", "seconds": 2}'],
                ledger.read_text(encoding="utf-8").splitlines(),
            )

    def test_is_exported_so_a_chrooted_mod_process_inherits_it(self) -> None:
        # install_all_mods.sh never sources shared.sh itself inside a mod --
        # it relies on the exported function surviving into `bash install.sh`.
        result = self._run('bash -c "record_phase_timing /dev/null x 1" ')
        self.assertEqual(0, result.returncode, result.stderr)


class FakePhaseEndToEndTests(unittest.TestCase):
    """Fake phases, using the real judge() and record_phase_timing() from
    shared.sh, feeding the real tools/build_timings.py -- the same two
    calls every real phase in build.sh makes, exercised end to end."""

    def _run(self, body: str) -> subprocess.CompletedProcess[str]:
        script = f'set -e\nsource "{SHARED_SH}"\n{body}\n'
        return subprocess.run(["bash", "-c", script], text=True, capture_output=True, check=False)

    def test_a_run_of_fake_phases_summarizes_longest_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "timings.jsonl"
            body = textwrap.dedent(f"""\
                function fake_phase_a() {{
                    local t0=$(date +%s)
                    sleep 0.2
                    judge "Fake phase A"
                    record_phase_timing "{ledger}" "Fake phase A" "$(( $(date +%s) - t0 ))"
                }}
                function fake_phase_b() {{
                    local t0=$(date +%s)
                    true
                    judge "Fake phase B"
                    record_phase_timing "{ledger}" "Fake phase B" "$(( $(date +%s) - t0 ))"
                }}
                fake_phase_a
                fake_phase_b
            """)
            result = self._run(body)
            self.assertEqual(0, result.returncode, result.stderr)
            entries = _read_jsonl(ledger)
            self.assertEqual(["Fake phase A", "Fake phase B"], [e["phase"] for e in entries])
            self.assertGreaterEqual(entries[0]["seconds"], 0)

            summary = subprocess.run(
                (sys.executable, str(ROOT / "tools" / "build_timings.py"), "summarize",
                 "--file", str(ledger), "--stem", str(Path(tmp) / "dist" / "out")),
                text=True, capture_output=True, check=False,
            )
            self.assertEqual(0, summary.returncode, summary.stderr)
            self.assertIn("Fake phase A", summary.stdout)
            self.assertIn("Fake phase B", summary.stdout)

    def test_a_fake_phase_that_fails_is_not_recorded_but_earlier_ones_are(self) -> None:
        """judge() calls exit 1 on failure -- the failing phase's own
        record_phase_timing line never runs, but a completed phase before it
        is already on disk. This is why a build that dies mid-way still
        reports what the phases before it cost."""
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "timings.jsonl"
            body = textwrap.dedent(f"""\
                function fake_ok() {{
                    local t0=$(date +%s)
                    true
                    judge "Fake ok phase"
                    record_phase_timing "{ledger}" "Fake ok phase" "$(( $(date +%s) - t0 ))"
                }}
                function fake_failing() {{
                    local t0=$(date +%s)
                    false
                    judge "Fake failing phase"
                    record_phase_timing "{ledger}" "Fake failing phase" "$(( $(date +%s) - t0 ))"
                }}
                fake_ok
                fake_failing
                echo "unreachable"
            """)
            result = self._run(body)
            self.assertNotEqual(0, result.returncode)
            self.assertNotIn("unreachable", result.stdout)
            entries = _read_jsonl(ledger)
            self.assertEqual(["Fake ok phase"], [e["phase"] for e in entries])


class ModTimingRealExecutionTests(unittest.TestCase):
    """mods/install_all_mods.sh's own per-mod timing, run for real (real
    shared.sh, real judge/print_ok/record_phase_timing -- nothing stubbed
    except the mods themselves, which are throwaway fixtures)."""

    def _make_mods(self, mods_dir: Path) -> None:
        for name, body in (
            ("01-alpha", 'sleep 0.3\necho "alpha ran"\n'),
            ("02-beta", 'echo "beta ran"\n'),
            ("03-gamma-fails", 'echo "gamma ran"\nexit 1\n'),
            ("04-delta", 'echo "delta ran"\n'),
        ):
            mod = mods_dir / name
            mod.mkdir()
            _write_executable(mod / "install.sh", body)

    def test_successful_mods_are_each_timed_and_the_failure_stops_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mods_dir = Path(tmp)
            self._make_mods(mods_dir)
            env = dict(os.environ)
            env["SCRIPT_DIR"] = str(mods_dir)
            script = (
                f'set -e -u -o pipefail\n'
                f'source "{SHARED_SH}"\n'
                f'CLEANUP_MOD="85-cleanup-mod"\n'
                f'PHASE="early"\n'
                + INSTALL_ALL_MODS_SH.read_text(encoding="utf-8").split('PHASE="${1:-all}"', 1)[1]
            )
            result = subprocess.run(["bash", "-c", script], text=True, capture_output=True, env=env, check=False)
            self.assertNotEqual(0, result.returncode)
            # 01 and 02 ran and were timed; 03 failed (set -e stopped there); 04 never ran.
            self.assertIn("alpha ran", result.stdout)
            self.assertIn("beta ran", result.stdout)
            self.assertIn("gamma ran", result.stdout)
            self.assertNotIn("delta ran", result.stdout)
            self.assertIn("Mod 01-alpha finished in", result.stdout)
            self.assertIn("Mod 02-beta finished in", result.stdout)
            self.assertNotIn("03-gamma-fails finished", result.stdout)

            entries = _read_jsonl(mods_dir / "timings.jsonl")
            self.assertEqual(["01-alpha", "02-beta"], [e["phase"] for e in entries])
            self.assertGreaterEqual(entries[0]["seconds"], 0)

    def test_the_cleanup_phase_times_only_the_cleanup_mod(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mods_dir = Path(tmp)
            self._make_mods(mods_dir)
            (mods_dir / "85-cleanup-mod").mkdir()
            _write_executable(mods_dir / "85-cleanup-mod" / "install.sh", 'echo "cleanup ran"\n')
            env = dict(os.environ)
            env["SCRIPT_DIR"] = str(mods_dir)
            script = (
                f'set -e -u -o pipefail\n'
                f'source "{SHARED_SH}"\n'
                f'CLEANUP_MOD="85-cleanup-mod"\n'
                f'PHASE="cleanup"\n'
                + INSTALL_ALL_MODS_SH.read_text(encoding="utf-8").split('PHASE="${1:-all}"', 1)[1]
            )
            result = subprocess.run(["bash", "-c", script], text=True, capture_output=True, env=env, check=False)
            self.assertEqual(0, result.returncode, result.stderr)
            entries = _read_jsonl(mods_dir / "timings.jsonl")
            self.assertEqual(["85-cleanup-mod"], [e["phase"] for e in entries])


class CollectModTimingsTests(unittest.TestCase):
    """build.sh's collect_mod_timings: merges the chroot-local ledger into
    the host-side one and clears the source, so the next chroot invocation
    (cleanup, after early) does not get the same mods merged in twice."""

    def _harness(self, tmp: Path) -> Path:
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        _write_executable(bin_dir / "sudo", 'exec "$@"\n')
        return bin_dir

    def _run(self, tmp: Path, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        build = BUILD_SH.read_text(encoding="utf-8")
        function_body = extract_function(build, "collect_mod_timings")
        bin_dir = self._harness(tmp)
        env = dict(os.environ)
        env["PATH"] = f"{bin_dir}:{env['PATH']}"
        env["TIMINGS_FILE"] = str(tmp / ".build" / "timings.jsonl")
        env.update(extra_env or {})
        script = f'source "{SHARED_SH}"\n{function_body}\ncollect_mod_timings\n'
        return subprocess.run(["bash", "-c", script], text=True, capture_output=True, cwd=tmp, env=env, check=False)

    def test_merges_the_chroot_ledger_and_removes_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            mod_dir = tmp / "new_building_os" / "root" / "mods"
            mod_dir.mkdir(parents=True)
            (mod_dir / "timings.jsonl").write_text(
                '{"phase": "05-live-kernel-apps-installer", "seconds": 7}\n', encoding="utf-8")
            result = self._run(tmp)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(
                [{"phase": "05-live-kernel-apps-installer", "seconds": 7}],
                _read_jsonl(tmp / ".build" / "timings.jsonl"),
            )
            self.assertFalse((mod_dir / "timings.jsonl").exists(), "must clear the source so cleanup's mods are not merged twice")

    def test_does_nothing_and_does_not_fail_when_nothing_was_written(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            (tmp / "new_building_os" / "root" / "mods").mkdir(parents=True)
            result = self._run(tmp)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertFalse((tmp / ".build" / "timings.jsonl").exists())


class CleanResetsTimingsLedgerTests(unittest.TestCase):
    """clean()'s ledger reset: keeps a "Build packages" entry from this run
    (tools/build_packages.py always runs first) and drops everything else,
    so a direct re-run of ./build.sh does not mix in a previous run's mods,
    Ansible, squashfs or ISO-assembly timings."""

    def _run(self, tmp: Path, ledger_contents: str | None) -> subprocess.CompletedProcess[str]:
        build = BUILD_SH.read_text(encoding="utf-8")
        function_body = extract_function(build, "clean")
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        _write_executable(bin_dir / "sudo", 'exec "$@"\n')
        ledger = tmp / ".build" / "timings.jsonl"
        if ledger_contents is not None:
            ledger.parent.mkdir(parents=True)
            ledger.write_text(ledger_contents, encoding="utf-8")
        env = dict(os.environ)
        env["PATH"] = f"{bin_dir}:{env['PATH']}"
        env["TIMINGS_FILE"] = str(ledger)
        script = f'set -u\nsource "{SHARED_SH}"\n{function_body}\nclean\n'
        return subprocess.run(["bash", "-c", script], text=True, capture_output=True, cwd=tmp, env=env, check=False)

    def test_keeps_only_the_build_packages_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            result = self._run(
                tmp,
                '{"phase": "Build packages", "seconds": 30}\n'
                '{"phase": "06-profile-software", "seconds": 400}\n'
                '{"phase": "Compress rootfs", "seconds": 600}\n',
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(
                [{"phase": "Build packages", "seconds": 30}],
                _read_jsonl(tmp / ".build" / "timings.jsonl"),
            )

    def test_leaves_an_empty_ledger_when_packages_never_ran_before(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            result = self._run(tmp, '{"phase": "06-profile-software", "seconds": 400}\n')
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual([], _read_jsonl(tmp / ".build" / "timings.jsonl"))

    def test_a_missing_ledger_is_not_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            result = self._run(Path(tmp_str), ledger_contents=None)
            self.assertEqual(0, result.returncode, result.stderr)


class DownloadBaseSystemTimingTests(unittest.TestCase):
    """download_base_system (the chroot bootstrap): times the whole
    function -- the debootstrap call dominates, "Create build directory"
    is instant -- with a fake debootstrap standing in for the real,
    network-and-root-needing one."""

    def test_records_one_entry_for_the_whole_function(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            bin_dir = tmp / "bin"
            bin_dir.mkdir()
            _write_executable(bin_dir / "sudo", 'exec "$@"\n')
            _write_executable(bin_dir / "debootstrap", 'sleep 0.2\nexit 0\n')
            build = BUILD_SH.read_text(encoding="utf-8")
            function_body = extract_function(build, "download_base_system")
            ledger = tmp / ".build" / "timings.jsonl"
            env = dict(os.environ)
            env["PATH"] = f"{bin_dir}:{env['PATH']}"
            env.update({
                "SCRIPT_DIR": str(tmp), "TIMINGS_FILE": str(ledger),
                "BASE_ID": "ubuntu", "TARGET_SUITE": "noble", "TARGET_ARCH": "amd64",
                "APT_SOURCE": "http://archive.example/ubuntu",
            })
            script = f'set -e -u\nsource "{SHARED_SH}"\n{function_body}\ndownload_base_system\n'
            result = subprocess.run(["bash", "-c", script], text=True, capture_output=True, cwd=tmp, env=env, check=False)
            self.assertEqual(0, result.returncode, result.stderr)
            entries = _read_jsonl(ledger)
            self.assertEqual(["Download base system"], [e["phase"] for e in entries])
            self.assertGreaterEqual(entries[0]["seconds"], 0)


class RunAnsibleChrootTimingTests(unittest.TestCase):
    """run_ansible_chroot (the Ansible run) has two exit points -- skipped
    (ansible-playbook missing, nothing required it) and the real run --
    and both must record the phase."""

    def _harness_env(self, tmp: Path, bin_dir: Path) -> dict[str, str]:
        env = dict(os.environ)
        env["PATH"] = f"{bin_dir}:{env['PATH']}"
        env.update({
            "SCRIPT_DIR": str(tmp), "TIMINGS_FILE": str(tmp / ".build" / "timings.jsonl"),
            "ANSIBLE_PLAYBOOKS": "", "ANSIBLE_VARS_FILE": ".build/ansible-vars.json",
        })
        return env

    def test_skipped_when_ansible_playbook_is_missing_and_not_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            bin_dir = tmp / "bin"  # deliberately empty: no ansible-playbook on PATH
            bin_dir.mkdir()
            build = BUILD_SH.read_text(encoding="utf-8")
            function_body = extract_function(build, "run_ansible_chroot")
            env = self._harness_env(tmp, bin_dir)
            env["ANSIBLE_CHROOT_REQUIRED"] = "false"
            script = f'set -e -u\nsource "{SHARED_SH}"\n{function_body}\nrun_ansible_chroot\n'
            result = subprocess.run(["bash", "-c", script], text=True, capture_output=True, cwd=tmp, env=env, check=False)
            self.assertEqual(0, result.returncode, result.stderr)
            entries = _read_jsonl(tmp / ".build" / "timings.jsonl")
            self.assertEqual(["Apply profile with Ansible in chroot"], [e["phase"] for e in entries])

    def test_records_the_phase_when_ansible_actually_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            bin_dir = tmp / "bin"
            bin_dir.mkdir()
            _write_executable(bin_dir / "sudo", 'exec "$@"\n')
            _write_executable(bin_dir / "ansible-playbook", 'sleep 0.1\nexit 0\n')
            (tmp / ".build").mkdir()
            (tmp / ".build" / "ansible-vars.json").write_text("{}", encoding="utf-8")
            (tmp / "ansible" / "collections" / "ansible_collections" / "synos" / "workstation" / "playbooks").mkdir(parents=True)
            (tmp / "ansible" / "collections" / "ansible_collections" / "synos" / "workstation" / "playbooks" / "customize_chroot.yml").write_text("- hosts: all\n", encoding="utf-8")
            build = BUILD_SH.read_text(encoding="utf-8")
            function_body = extract_function(build, "run_ansible_chroot")
            env = self._harness_env(tmp, bin_dir)
            script = f'set -e -u\nsource "{SHARED_SH}"\n{function_body}\nrun_ansible_chroot\n'
            result = subprocess.run(["bash", "-c", script], text=True, capture_output=True, cwd=tmp, env=env, check=False)
            self.assertEqual(0, result.returncode, result.stderr)
            entries = _read_jsonl(tmp / ".build" / "timings.jsonl")
            self.assertEqual(["Apply profile with Ansible in chroot"], [e["phase"] for e in entries])
            self.assertGreaterEqual(entries[0]["seconds"], 0)


class ReportBuildTimingsTests(unittest.TestCase):
    """build.sh's report_build_timings: the final, successful-build-only
    summary, written beside the ISO with the same stem build_iso used."""

    def test_writes_the_stem_named_evidence_file_and_prints_the_ranking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            (tmp / "tools").mkdir()
            os.symlink(ROOT / "tools" / "build_timings.py", tmp / "tools" / "build_timings.py")
            ledger = tmp / ".build" / "timings.jsonl"
            ledger.parent.mkdir(parents=True)
            ledger.write_text(
                '{"phase": "06-profile-software", "seconds": 40}\n'
                '{"phase": "Compress rootfs", "seconds": 60}\n',
                encoding="utf-8",
            )
            build = BUILD_SH.read_text(encoding="utf-8")
            function_body = extract_function(build, "report_build_timings")
            env = dict(os.environ)
            env.update({
                "SCRIPT_DIR": str(tmp), "TIMINGS_FILE": str(ledger),
                "TARGET_FILE_NAME": "SynOS", "TARGET_BUILD_VERSION": "1.0.0",
                "BASE_ID": "ubuntu", "TARGET_SUITE": "noble", "DATE": "2609251200",
                "TARGET_ARCH": "amd64",
            })
            script = f'source "{SHARED_SH}"\n{function_body}\nreport_build_timings\n'
            result = subprocess.run(["bash", "-c", script], text=True, capture_output=True, cwd=tmp, env=env, check=False)
            self.assertEqual(0, result.returncode, result.stderr)
            evidence_path = tmp / "dist" / "SynOS-1.0.0-ubuntu-noble-2609251200-amd64.timings.json"
            self.assertTrue(evidence_path.is_file())
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            self.assertEqual("Compress rootfs", evidence["phases"][0]["phase"])
            self.assertIn("Compress rootfs", result.stdout)


if __name__ == "__main__":
    unittest.main()
