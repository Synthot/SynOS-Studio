"""packaging/install-test-engine.sh: the pure decision functions exercised
directly — storage selection (the largest suitable filesystem, an explicit
path, a path too small, a filesystem that cannot back an overlay), the
configuration rendered from answers, distribution detection and its
refusal on an unsupported one, the idempotent podman storage.conf writer,
that no credential is ever written, and --dry-run.

Every call below sources the real script in a fresh bash subprocess and
runs one function or a short snippet after it — the same harness shape as
tests/unit/test_stack_install.py uses for mods/stack.sh: fake df/stat/
os-release/package-manager executables ahead of the real ones on PATH,
never a real install, a real distribution check, or a real container
runtime. Sourcing never runs main() itself (the script only calls it when
executed, not when sourced — see the guard at the bottom of the script),
so every function below is reachable without any of main()'s own
side effects.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "packaging" / "install-test-engine.sh"

# Resolved once, from this test process's own PATH, and used as an absolute
# path everywhere a test spawns bash itself — never the bare name "bash" —
# so that resolving the *interpreter* never depends on what a closed PATH
# built for the *script under test* does or doesn't contain (below).
BASH = shutil.which("bash") or "/bin/bash"

# Directories real system tools this script itself shells out to (sed, awk,
# mkdir, dirname, cat, mv, cp, tr, head, id, grep, mktemp, timeout, rm,
# tail, uname, ...) actually live in, on the distributions this project
# targets.
REAL_TOOL_DIRS = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")


def _write_executable(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/bash\n{body}", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def build_closed_path(bindir: Path, *, fakes: dict[str, str], omit: set[str] = frozenset()) -> str:
    """Builds `bindir` into a *closed* PATH entry — the returned string names
    `bindir` alone, never `bindir` prepended to the real PATH — so a name
    this script looks up is either one of the fakes given here, a real
    system tool symlinked in below, or genuinely absent. That last case is
    the one a "fakes ahead of the real PATH" setup cannot represent at all:
    prepending only ever adds an earlier match, it can never make a real
    binary already further down PATH stop existing (item 152 — this is
    exactly why detect_runtime found the machine's real podman even though
    the test only ever created a fake docker).

    `fakes`: name -> script body, written as executables directly in
    `bindir`. `omit`: names that must never appear in the closed PATH at
    all, real or fake — for a runtime test, always both "podman" and
    "docker" except whichever one `fakes` itself provides, so the other is
    guaranteed genuinely absent regardless of what is installed on the
    machine running the test. Every other real command actually needed
    (REAL_TOOL_DIRS) is symlinked in unless it's a fake or an omission, so
    the script still runs normally."""
    bindir.mkdir(parents=True, exist_ok=True)
    for name, script_body in fakes.items():
        _write_executable(bindir / name, script_body)
    claimed = set(fakes) | set(omit)
    for real_dir in REAL_TOOL_DIRS:
        directory = Path(real_dir)
        if not directory.is_dir():
            continue
        for entry in directory.iterdir():
            if entry.name in claimed:
                continue
            try:
                if entry.is_file() and os.access(entry, os.X_OK):
                    (bindir / entry.name).symlink_to(entry)
            except OSError:
                continue
            claimed.add(entry.name)  # first match wins, like real PATH order
    return str(bindir)


def run_snippet(body: str, env: dict | None = None) -> subprocess.CompletedProcess:
    """Sources the real script, then runs `body` — one function call or a
    short sequence of them — in a fresh bash subprocess."""
    script = f'source "{SCRIPT}"\n{body}\n'
    full_env = dict(os.environ)
    full_env.update(env or {})
    return subprocess.run(["bash", "-c", script], text=True, capture_output=True, env=full_env, cwd=str(ROOT))


def fake_df_stat(bindir: Path, filesystems: list[tuple[str, int, str]], argv_log: Path | None = None) -> None:
    """filesystems: (mountpoint, avail_kb, fstype) tuples, longest mountpoint
    first if a path could match more than one. Installs a fake `df` that
    mirrors the *real* GNU coreutils df this project actually runs against
    (verified directly against `df (GNU coreutils) 9.4` — see
    RealDfInvocationTests below), not a permissive stand-in:

    - `df ... --output=...` together with `-P` is refused exactly like the
      real one ("options -P and --output are mutually exclusive", exit
      nonzero, nothing on stdout) — item 132: a fake that accepts what the
      real tool rejects is worse than no fake.
    - `df -k --output=target,avail,fstype` (no `-P`) lists every configured
      filesystem — the real "list every real filesystem" invocation this
      script actually makes.
    - anything else is read as "df ... <path>" (`-Pk PATH`, or a bare path):
      the configured filesystem whose mountpoint is the longest prefix of
      PATH, POSIX-style single-line output.

    When `argv_log` is given, every invocation's exact argument list is
    appended to it (one line each), so a test can assert precisely what
    this script sent df — not just that *something* worked."""
    log_line = f'printf "%s\\n" "$*" >> "{argv_log}"' if argv_log else ""
    lines = [log_line,
             'args="$*"',
             'case "$args" in',
             '    *"--output="*"-P"*|*"-P"*"--output="*)',
             '        echo "df: options -P and --output are mutually exclusive" >&2',
             '        echo "Try '"'"'df --help'"'"' for more information." >&2',
             '        exit 1 ;;',
             'esac']
    listing = "\\n".join(f"{m}\\t{a}\\t{t}" for m, a, t in filesystems)
    lines += [
        'if [[ "$args" == *"--output=target,avail,fstype"* ]]; then',
        f'    printf "Filesystem\\tAvail\\tType\\n{listing}\\n"',
        "    exit 0",
        "fi",
        'path="${@: -1}"',
        "best=''; best_avail=0; best_fstype=ext4",
    ]
    for mount, avail, fstype in sorted(filesystems, key=lambda f: len(f[0])):
        lines.append(f'case "$path" in "{mount}"*) best="{mount}"; best_avail={avail}; best_fstype="{fstype}" ;; esac')
    lines += [
        'echo "Filesystem     1024-blocks       Used  Available Capacity Mounted on"',
        'printf "fake %10d %10d %10d %3s%% %s\\n" 0 0 "$best_avail" "1" "${best:-$path}"',
    ]
    _write_executable(bindir / "df", "\n".join(lines) + "\n")

    stat_lines = ['path="${@: -1}"', "case \"$path\" in"]
    for mount, avail, fstype in sorted(filesystems, key=lambda f: -len(f[0])):
        stat_lines.append(f'    "{mount}"*) echo "{fstype}" ;;')
    stat_lines += ["    *) echo ext4 ;;", "esac"]
    _write_executable(bindir / "stat", "\n".join(stat_lines) + "\n")


class ScriptShapeTests(unittest.TestCase):
    def test_script_is_well_formed_bash(self) -> None:
        result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True, check=False)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_declares_bash_and_uses_bash_only_syntax_consistently(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("#!/usr/bin/env bash"))

    def test_sourcing_the_script_never_runs_main(self) -> None:
        result = run_snippet("echo sourced-ok")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("sourced-ok", result.stdout)
        self.assertNotIn("engine checkout:", result.stdout)


class DistroDetectionTests(unittest.TestCase):
    def _osr(self, tmp: str, content: str) -> Path:
        path = Path(tmp) / "os-release"
        path.write_text(content, encoding="utf-8")
        return path

    def test_debian_is_detected_by_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            osr = self._osr(tmp, "ID=debian\n")
            result = run_snippet("detect_distro_family", env={"SYNOS_OS_RELEASE_FILE": str(osr)})
            self.assertEqual(0, result.returncode)
            self.assertEqual("debian", result.stdout.strip())

    def test_ubuntu_is_debian_family_via_id_like(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            osr = self._osr(tmp, 'ID=ubuntu\nID_LIKE=debian\n')
            result = run_snippet("detect_distro_family", env={"SYNOS_OS_RELEASE_FILE": str(osr)})
            self.assertEqual("debian", result.stdout.strip())

    def test_fedora_family(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            osr = self._osr(tmp, 'ID=fedora\n')
            result = run_snippet("detect_distro_family", env={"SYNOS_OS_RELEASE_FILE": str(osr)})
            self.assertEqual("fedora", result.stdout.strip())

    def test_opensuse_family(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            osr = self._osr(tmp, 'ID=opensuse-leap\nID_LIKE=suse\n')
            result = run_snippet("detect_distro_family", env={"SYNOS_OS_RELEASE_FILE": str(osr)})
            self.assertEqual("suse", result.stdout.strip())

    def test_arch_family(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            osr = self._osr(tmp, 'ID=arch\n')
            result = run_snippet("detect_distro_family", env={"SYNOS_OS_RELEASE_FILE": str(osr)})
            self.assertEqual("arch", result.stdout.strip())

    def test_alpine_family(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            osr = self._osr(tmp, 'ID=alpine\n')
            result = run_snippet("detect_distro_family", env={"SYNOS_OS_RELEASE_FILE": str(osr)})
            self.assertEqual("alpine", result.stdout.strip())

    def test_unsupported_distribution_is_refused_politely_not_guessed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            osr = self._osr(tmp, 'ID=solaris\n')
            result = run_snippet("require_family", env={"SYNOS_OS_RELEASE_FILE": str(osr)})
            self.assertEqual(3, result.returncode)
            self.assertIn("not recognized", result.stderr)
            self.assertIn("Debian/Ubuntu", result.stderr)
            self.assertIn("--skip-packages", result.stderr)
            self.assertNotIn("apt-get", result.stdout)
            self.assertNotIn("dnf", result.stdout)

    def test_missing_os_release_file_is_also_refused_not_crashed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "does-not-exist"
            result = run_snippet("require_family", env={"SYNOS_OS_RELEASE_FILE": str(missing)})
            self.assertEqual(3, result.returncode)
            self.assertIn("Supported families", result.stderr)


class StorageSelectionTests(unittest.TestCase):
    def test_the_largest_suitable_filesystem_is_chosen_automatically(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bindir = Path(tmp) / "bin"
            bindir.mkdir()
            fake_df_stat(bindir, [
                ("/", 10 * 1048576, "ext4"),
                ("/mnt/big", 5000 * 1048576, "ext4"),
                ("/boot/efi", 1 * 1048576, "vfat"),
                ("/dev", 8000 * 1048576, "tmpfs"),
            ])
            env = {"PATH": f"{bindir}:{os.environ['PATH']}"}
            result = run_snippet("select_storage_path 40", env=env)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual("/mnt/big", result.stdout.split()[0])
            self.assertIn("chosen: /mnt/big", result.stderr)
            self.assertIn("5000 GB free", result.stderr)
            # tmpfs is excluded before it is even shown as a candidate
            self.assertNotIn("/dev", result.stderr)
            # the numbers it looked at are shown, not just the winner
            self.assertIn("considered: /", result.stderr)
            self.assertIn("considered: /boot/efi", result.stderr)

    def test_an_explicit_path_is_used_and_created(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "store"
            bindir = Path(tmp) / "bin"
            bindir.mkdir()
            fake_df_stat(bindir, [(str(target), 6000 * 1048576, "ext4")])
            env = {"PATH": f"{bindir}:{os.environ['PATH']}"}
            result = run_snippet(f'select_storage_path 40 "{target}"', env=env)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertTrue(target.is_dir())
            self.assertEqual(str(target), result.stdout.split()[0])

    def test_an_explicit_path_too_small_is_reported_plainly_not_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "store"
            bindir = Path(tmp) / "bin"
            bindir.mkdir()
            fake_df_stat(bindir, [(str(target), 5 * 1048576, "ext4")])
            env = {"PATH": f"{bindir}:{os.environ['PATH']}"}
            result = run_snippet(f'select_storage_path 40 "{target}"', env=env)
            self.assertEqual(0, result.returncode, result.stderr)  # not refused
            self.assertIn("only 5 GB free", result.stderr)
            self.assertIn("needs about 40 GB", result.stderr)
            self.assertEqual(str(target), result.stdout.split()[0])

    def test_a_filesystem_that_cannot_back_an_overlay_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "store"
            target.mkdir()
            bindir = Path(tmp) / "bin"
            bindir.mkdir()
            fake_df_stat(bindir, [(str(target), 6000 * 1048576, "vfat")])
            env = {"PATH": f"{bindir}:{os.environ['PATH']}"}
            result = run_snippet(f'select_storage_path 40 "{target}"', env=env)
            self.assertNotEqual(0, result.returncode)
            self.assertIn("cannot back a container overlay filesystem", result.stderr)
            self.assertEqual("", result.stdout.strip())

    def test_auto_pick_refuses_when_nothing_qualifies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bindir = Path(tmp) / "bin"
            bindir.mkdir()
            fake_df_stat(bindir, [("/boot/efi", 500 * 1048576, "vfat")])
            env = {"PATH": f"{bindir}:{os.environ['PATH']}"}
            result = run_snippet("select_storage_path 40", env=env)
            self.assertNotEqual(0, result.returncode)
            self.assertIn("no filesystem capable of backing a container overlay", result.stderr)


class StorageConfIdempotenceTests(unittest.TestCase):
    def test_writing_the_same_graphroot_twice_leaves_the_file_byte_identical(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conf = Path(tmp) / "storage.conf"
            first = run_snippet(f'write_storage_conf "{conf}" "/mnt/big/containers"')
            self.assertEqual(0, first.returncode, first.stderr)
            content_after_first = conf.read_text(encoding="utf-8")

            second = run_snippet(f'write_storage_conf "{conf}" "/mnt/big/containers"')
            self.assertEqual(0, second.returncode, second.stderr)
            self.assertEqual(content_after_first, conf.read_text(encoding="utf-8"))
            self.assertEqual(1, content_after_first.count("[storage]"))
            self.assertEqual(1, content_after_first.count("graphroot"))

    def test_changing_the_graphroot_updates_in_place_without_duplicating_the_section(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conf = Path(tmp) / "storage.conf"
            run_snippet(f'write_storage_conf "{conf}" "/mnt/old"')
            result = run_snippet(f'write_storage_conf "{conf}" "/mnt/new"')
            self.assertEqual(0, result.returncode, result.stderr)
            content = conf.read_text(encoding="utf-8")
            self.assertEqual(1, content.count("[storage]"))
            self.assertEqual(1, content.count("graphroot"))
            self.assertIn('graphroot = "/mnt/new"', content)
            self.assertNotIn("/mnt/old", content)

    def test_an_existing_storage_conf_with_other_keys_keeps_them(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conf = Path(tmp) / "storage.conf"
            conf.write_text('[storage]\ndriver = "overlay"\n\n[storage.options]\nmount_program = "/usr/bin/fuse-overlayfs"\n',
                            encoding="utf-8")
            result = run_snippet(f'write_storage_conf "{conf}" "/mnt/big"')
            self.assertEqual(0, result.returncode, result.stderr)
            content = conf.read_text(encoding="utf-8")
            self.assertIn("mount_program", content)
            self.assertIn('graphroot = "/mnt/big"', content)


class ConfigWritingTests(unittest.TestCase):
    ANSWERS = (
        '"https://studio.example/data/catalog.json" "https://studio.example" '
        '"/mnt/big/synos-conformance" "/mnt/big/containers" 5 "1" "synos-conformance"'
    )

    def test_config_is_written_from_answers_and_matches_the_real_schema(self) -> None:
        result = run_snippet(f"write_config {self.ANSWERS}")
        self.assertEqual(0, result.returncode, result.stderr)
        data = yaml.safe_load(result.stdout)
        self.assertEqual("https://studio.example/data/catalog.json", data["catalog_url"])
        self.assertEqual("https://studio.example", data["site_url"])
        self.assertEqual("/mnt/big/synos-conformance", data["workdir"])
        self.assertEqual("/mnt/big/containers", data["container_root"])
        self.assertEqual(5, data["max_builds_per_run"])
        self.assertEqual("1", data["jobs"])  # a quoted string, not an int: "1" or "auto"
        self.assertIs(True, data["cleanup"])
        self.assertIsNone(data["image"])
        # every key this installer writes is one tools/catalog_conformance.py's
        # own Config dataclass actually knows about (Config.load rejects
        # anything else outright)
        known_config_fields = {
            "catalog_url", "workdir", "site_url", "min_free_gb", "min_store_gb",
            "max_builds_per_run", "build_timeout_minutes", "jobs", "smoke", "image",
            "engine_source", "container_root", "container_runroot", "report_url",
            "upload", "cleanup",
        }
        self.assertTrue(set(data) <= known_config_fields, set(data) - known_config_fields)

    def test_missing_site_url_is_left_commented_not_written_blank(self) -> None:
        result = run_snippet(
            'write_config "https://studio.example/data/catalog.json" "" "/wd" "" 5 "1" "synos-conformance"'
        )
        self.assertEqual(0, result.returncode, result.stderr)
        data = yaml.safe_load(result.stdout)
        self.assertNotIn("site_url", data)
        self.assertIn("# site_url:", result.stdout)

    def test_no_container_root_is_written_as_null_not_omitted(self) -> None:
        result = run_snippet(
            'write_config "https://studio.example/data/catalog.json" "https://studio.example" "/wd" "" 5 "auto" "synos-conformance"'
        )
        data = yaml.safe_load(result.stdout)
        self.assertIsNone(data["container_root"])

    def test_running_write_config_twice_with_the_same_answers_is_byte_identical(self) -> None:
        first = run_snippet(f"write_config {self.ANSWERS}")
        second = run_snippet(f"write_config {self.ANSWERS}")
        self.assertEqual(first.stdout, second.stdout)


class NoCredentialIsEverWrittenTests(unittest.TestCase):
    def test_no_uncommented_credential_line_anywhere_in_the_config(self) -> None:
        result = run_snippet(
            'write_config "https://studio.example/data/catalog.json" '
            '"https://studio.example" "/wd" "/store" 5 "1" "synos-conformance"'
        )
        self.assertEqual(0, result.returncode, result.stderr)
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            lowered = stripped.lower()
            for credential_word in ("password", "secret", "key_path", "token"):
                self.assertNotIn(credential_word, lowered, stripped)

    def test_upload_credential_keys_only_ever_appear_inside_comments(self) -> None:
        result = run_snippet(
            'write_config "https://studio.example/data/catalog.json" '
            '"https://studio.example" "/wd" "/store" 5 "1" "synos-conformance"'
        )
        self.assertIn("#   key_path:", result.stdout)
        self.assertIn("#   username:", result.stdout)
        # never an actual secret value, only a placeholder and where to put one
        self.assertNotIn("password:", result.stdout)

    def test_a_planted_environment_secret_never_reaches_the_written_config(self) -> None:
        # write_config takes no upload/credential arguments at all — there is
        # nothing for an ambient secret to leak through — proven here by
        # planting one in the environment and confirming it never surfaces.
        result = run_snippet(
            'write_config "https://studio.example/data/catalog.json" '
            '"https://studio.example" "/wd" "/store" 5 "1" "synos-conformance"',
            env={"SYNOS_UPLOAD_PASSWORD": "swordfish-do-not-print"},
        )
        self.assertNotIn("swordfish", result.stdout)


class DryRunTests(unittest.TestCase):
    """Every test in this class runs the whole script (main(), which calls
    detect_runtime() and requirement_present() against the real PATH it is
    given) — so every one of them is exactly the kind item 152 is about.
    Each gets a *closed* PATH (build_closed_path): whichever of podman/
    docker the test is not naming is guaranteed genuinely absent, not
    merely "not the first match" — proven both ways by RuntimeIsolationTests
    below, which runs this same harness with each runtime actually
    installed and actually absent in turn."""

    RUNTIME_FAKES = {
        "qemu-system-x86_64": "exit 0\n",
        "xorriso": "exit 0\n",
        "tesseract": "exit 0\n",
        "chromium": "exit 0\n",
        "sudo": 'exec "$@"\n',
        "systemctl": "exit 0\n",
        "useradd": "exit 0\n",
        "usermod": "exit 0\n",
        "getent": "exit 0\n",
        "nproc": "echo 4\n",
    }

    def _fake_environment(self, tmp: str, runtime: str = "podman") -> tuple[str, Path]:
        """Returns (closed PATH string, os-release path). `runtime` names
        exactly which of podman/docker is present; the other is placed in
        `omit`, so build_closed_path leaves it out of the returned PATH
        entirely — never found, no matter what this machine actually has
        installed (item 152)."""
        bindir = Path(tmp) / "bin"
        other = "docker" if runtime == "podman" else "podman"
        fakes = dict(self.RUNTIME_FAKES)
        fakes[runtime] = "exit 0\n"
        path = build_closed_path(bindir, fakes=fakes, omit={other, "df", "stat"})
        fake_df_stat(bindir, [("/mnt/big", 5000 * 1048576, "ext4")])
        osr = Path(tmp) / "os-release"
        osr.write_text("ID=debian\n", encoding="utf-8")
        return path, osr

    def _run(self, tmp: str, extra_args: list[str] | None = None, runtime: str = "podman") -> tuple[subprocess.CompletedProcess, Path]:
        path, osr = self._fake_environment(tmp, runtime=runtime)
        config_path = Path(tmp) / "conformance.yml"
        env = dict(os.environ)
        env["PATH"] = path  # replaced, never prepended: see build_closed_path
        env["SYNOS_OS_RELEASE_FILE"] = str(osr)
        args = [BASH, str(SCRIPT), "--dry-run", "--yes",
                f"--engine-root={ROOT}", "--site-url=https://studio.example",
                f"--config={config_path}", f"--unit-dir={tmp}/units"] + (extra_args or [])
        result = subprocess.run(args, capture_output=True, text=True, env=env, cwd=str(ROOT), check=False)
        return result, config_path

    def test_dry_run_prints_every_action_and_performs_none_of_them(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result, config_path = self._run(tmp)
            self.assertEqual(0, result.returncode, result.stderr + result.stdout)
            self.assertRegex(result.stdout, r"container runtime: .*/podman\b")  # item 154
            self.assertIn("[dry-run]", result.stdout)
            self.assertFalse(config_path.exists())
            self.assertFalse((Path(tmp) / "units").exists())
            self.assertFalse(Path("/var/lib/synos-conformance").exists())

    def test_dry_run_still_shows_the_config_it_would_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result, _ = self._run(tmp)
            self.assertIn("catalog_url:", result.stdout)
            self.assertIn("workdir:", result.stdout)

    def test_dry_run_twice_in_a_row_is_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first, config_path = self._run(tmp)
            second, _ = self._run(tmp)
            self.assertEqual(0, first.returncode, first.stderr)
            self.assertEqual(0, second.returncode, second.stderr)
            self.assertFalse(config_path.exists())

    def test_dry_run_on_an_unsupported_distribution_refuses_without_a_package_manager_guess(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path, osr = self._fake_environment(tmp)
            osr.write_text("ID=solaris\n", encoding="utf-8")
            env = dict(os.environ)
            env["PATH"] = path
            env["SYNOS_OS_RELEASE_FILE"] = str(osr)
            config_path = Path(tmp) / "conformance.yml"
            result = subprocess.run(
                [BASH, str(SCRIPT), "--dry-run", "--yes", f"--engine-root={ROOT}",
                 "--site-url=https://studio.example", f"--config={config_path}", f"--unit-dir={tmp}/units"],
                capture_output=True, text=True, env=env, cwd=str(ROOT), check=False,
            )
            self.assertEqual(3, result.returncode)
            self.assertIn("not recognized", result.stderr)
            self.assertFalse(config_path.exists())

    def test_docker_still_gets_a_working_configuration_with_storage_advice_printed_once(self) -> None:
        # item 135: on a machine whose only runtime is docker, not podman —
        # docker_storage_note must print exactly once, and the run must
        # still complete and write a usable config, not refuse. Item 152:
        # this is true regardless of whether *this* machine (running the
        # test) happens to also have podman installed, because podman is
        # genuinely absent from the closed PATH this test builds, not just
        # second in line.
        with tempfile.TemporaryDirectory() as tmp:
            result, _ = self._run(tmp, runtime="docker")
            self.assertEqual(0, result.returncode, result.stderr + result.stdout)
            self.assertRegex(result.stdout, r"container runtime: .*/docker\b")  # item 154
            self.assertEqual(1, result.stdout.count("is one setting for the whole"), result.stdout)
            self.assertIn("container_root: null", result.stdout)  # never set for docker
            self.assertIn("jobs:", result.stdout)

    def test_docker_with_an_explicit_storage_path_that_does_not_exist_yet_still_works(self) -> None:
        # the bug this installer shipped with: the docker branch never even
        # looked at an explicit --storage-path, so free space for it was
        # read from a directory that was never created, silently degrading
        # to "cannot read free space" and jobs "1".
        with tempfile.TemporaryDirectory() as tmp:
            target = f"{tmp}/not-created-yet/storage"
            result, _ = self._run(tmp, extra_args=[f"--storage-path={target}"], runtime="docker")
            self.assertEqual(0, result.returncode, result.stderr + result.stdout)
            self.assertRegex(result.stdout, r"container runtime: .*/docker\b")
            self.assertFalse(Path(target).exists())  # --dry-run still creates nothing
            self.assertNotIn("could not read free space", result.stdout + result.stderr)
            self.assertIn(f'workdir: "{target}/synos-conformance"', result.stdout)

    def test_docker_falls_back_to_the_service_home_when_no_disk_qualifies_at_all(self) -> None:
        # item 135: a failed storage pick must not be fatal under docker —
        # only podman's own storage depends on it being real.
        with tempfile.TemporaryDirectory() as tmp:
            path, osr = self._fake_environment(tmp, runtime="docker")
            bindir = Path(tmp) / "bin"
            fake_df_stat(bindir, [("/boot/efi", 500 * 1048576, "vfat")])  # nothing overlay-capable at all
            config_path = Path(tmp) / "conformance.yml"
            env = dict(os.environ)
            env["PATH"] = path
            env["SYNOS_OS_RELEASE_FILE"] = str(osr)
            result = subprocess.run(
                [BASH, str(SCRIPT), "--dry-run", "--yes", f"--engine-root={ROOT}",
                 "--site-url=https://studio.example", f"--config={config_path}", f"--unit-dir={tmp}/units"],
                capture_output=True, text=True, env=env, cwd=str(ROOT), check=False,
            )
            self.assertEqual(0, result.returncode, result.stderr + result.stdout)  # not a refusal
            self.assertRegex(result.stdout, r"container runtime: .*/docker\b")
            self.assertIn("using /var/lib/synos-conformance instead", result.stdout + result.stderr)


class RuntimeIsolationTests(unittest.TestCase):
    """item 153: proves the isolation both ways — the docker scenario and
    the podman scenario each run against a closed PATH (build_closed_path)
    in which the *other* runtime is placed in `omit` and so is genuinely
    absent, never merely second in line. Run this file's whole suite on a
    machine with podman installed (as-is) and again with PATH edited to
    remove every real podman/docker directory beforehand
    (`PATH=$(printf '%s\\n' "$PATH" | tr ':' '\\n' | grep -v ... )`, or
    simplest, a container with neither installed): both of these two tests
    pass exactly the same way either time, because neither one's outcome
    depends on the host at all — see this class's own module docstring
    entry point (build_closed_path) for how."""

    def _closed_path_with_fakes(self, tmp: str, *, runtime: str) -> Path:
        bindir = Path(tmp) / "bin"
        fakes = dict(DryRunTests.RUNTIME_FAKES)
        fakes[runtime] = "exit 0\n"
        other = "docker" if runtime == "podman" else "podman"
        build_closed_path(bindir, fakes=fakes, omit={other, "df", "stat"})
        fake_df_stat(bindir, [("/mnt/big", 5000 * 1048576, "ext4")])
        return bindir

    def _run(self, tmp: str, bindir: Path, runtime_expected: str) -> subprocess.CompletedProcess:
        osr = Path(tmp) / "os-release"
        osr.write_text("ID=debian\n", encoding="utf-8")
        config_path = Path(tmp) / "conformance.yml"
        env = dict(os.environ)
        env["PATH"] = str(bindir)
        env["SYNOS_OS_RELEASE_FILE"] = str(osr)
        result = subprocess.run(
            [BASH, str(SCRIPT), "--dry-run", "--yes", f"--engine-root={ROOT}",
             "--site-url=https://studio.example", f"--config={config_path}", f"--unit-dir={tmp}/units"],
            capture_output=True, text=True, env=env, cwd=str(ROOT), check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr + result.stdout)
        self.assertRegex(result.stdout, rf"container runtime: .*/{runtime_expected}\b",
                         f"expected the {runtime_expected} path; got:\n{result.stdout}")
        return result

    def test_docker_scenario_is_green_with_podman_genuinely_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bindir = self._closed_path_with_fakes(tmp, runtime="docker")
            self._run(tmp, bindir, "docker")

    def test_podman_scenario_is_green_with_docker_genuinely_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bindir = self._closed_path_with_fakes(tmp, runtime="podman")
            self._run(tmp, bindir, "podman")


class RealDfInvocationTests(unittest.TestCase):
    """The bug a permissive fake hid (item 131/132): GNU coreutils df
    refuses `-P` together with `--output` ("options -P and --output are
    mutually exclusive"). Confirmed directly against the real binary this
    project runs against:

        $ df --version | head -1
        df (GNU coreutils) 9.4
        $ df -Pk --output=target,avail,fstype
        df: options -P and --output are mutually exclusive

    fake_df_stat's df reproduces exactly this refusal (and nothing more
    permissive), so these tests fail if list_candidate_filesystems or
    select_storage_path ever again sends `-P` alongside `--output` — the
    same way the real df would refuse them on the machine this was written
    for."""

    def test_the_filesystem_listing_never_combines_dash_P_with_dash_dash_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bindir = Path(tmp) / "bin"
            bindir.mkdir()
            argv_log = Path(tmp) / "df-argv.log"
            fake_df_stat(bindir, [("/mnt/big", 5000 * 1048576, "ext4")], argv_log=argv_log)
            env = {"PATH": f"{bindir}:{os.environ['PATH']}"}
            result = run_snippet("select_storage_path 40", env=env)
            self.assertEqual(0, result.returncode, result.stderr)
            for line in argv_log.read_text(encoding="utf-8").splitlines():
                self.assertFalse("-P" in line.split() and "--output=target,avail,fstype" in line,
                                 f"df was invoked with both -P and --output: {line!r}")
            # and the exact invocation used for the listing call is spelled
            # out, not merely "doesn't contain -P"
            self.assertIn("-k --output=target,avail,fstype", argv_log.read_text(encoding="utf-8"))

    def test_a_fake_that_matches_real_df_would_have_caught_the_original_bug(self) -> None:
        # Regression guard, run against the *original* invocation
        # (`df -Pk --output=...`) to prove fake_df_stat would have failed
        # this suite before the fix, not just that it passes after.
        with tempfile.TemporaryDirectory() as tmp:
            bindir = Path(tmp) / "bin"
            bindir.mkdir()
            fake_df_stat(bindir, [("/mnt/big", 5000 * 1048576, "ext4")])
            env = {"PATH": f"{bindir}:{os.environ['PATH']}"}
            broken = run_snippet('df -Pk --output=target,avail,fstype', env=env)
            self.assertNotEqual(0, broken.returncode)
            self.assertIn("mutually exclusive", broken.stderr)
            fixed = run_snippet('df -k --output=target,avail,fstype', env=env)
            self.assertEqual(0, fixed.returncode, fixed.stderr)


class NumericGuardTests(unittest.TestCase):
    """item 133: an empty or non-numeric free-space field must be skipped
    with a printed reason, never reach a `[ ... -lt ... ]` comparison."""

    def test_zero_real_filesystems_never_produces_a_phantom_candidate(self) -> None:
        # The original crash: a heredoc built around a command substitution
        # still feeds `while read` exactly one blank line when the command
        # produces no output at all, so "0 candidates" silently became a
        # single iteration with every field empty, which then hit
        # `[ "" -gt 0 ]` — "integer expression expected". Fixed by reading
        # from process substitution instead (which yields zero iterations
        # for zero lines), proven here directly: an entirely empty listing.
        with tempfile.TemporaryDirectory() as tmp:
            bindir = Path(tmp) / "bin"
            bindir.mkdir()
            fake_df_stat(bindir, [])  # no filesystems at all
            env = {"PATH": f"{bindir}:{os.environ['PATH']}"}
            result = run_snippet("select_storage_path 40", env=env)
            self.assertNotEqual(0, result.returncode)
            self.assertNotIn("integer expression expected", result.stderr)
            self.assertNotIn("considered:  (", result.stderr)  # no blank/phantom row
            self.assertIn("among 0 candidate(s)", result.stderr)

    def test_a_row_with_an_unreadable_free_space_field_is_skipped_with_a_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bindir = Path(tmp) / "bin"
            bindir.mkdir()
            # df -k --output=... emitting a malformed/blank Avail field for
            # one mount (real df has been seen to do this for some
            # network/special filesystems) alongside one good one.
            _write_executable(bindir / "df", textwrap.dedent('''
                if [[ "$*" == *"--output=target,avail,fstype"* ]]; then
                    printf "Filesystem\\tAvail\\tType\\n/weird\\t\\text4\\n/mnt/big\\t5242880000\\text4\\n"
                    exit 0
                fi
                echo "Filesystem 1024-blocks Used Available Capacity Mounted"
                echo "fake 0 0 5242880000 1% /mnt/big"
            '''))
            _write_executable(bindir / "stat", 'echo ext4\n')
            env = {"PATH": f"{bindir}:{os.environ['PATH']}"}
            result = run_snippet("select_storage_path 40", env=env)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual("/mnt/big", result.stdout.split()[0])  # the good candidate still wins
            self.assertIn("skipping /weird: could not read its free space", result.stderr)
            self.assertNotIn("integer expression expected", result.stderr)


class ServiceUserInvocationTests(unittest.TestCase):
    """item 134: useradd's actual long option is `--home-dir` (`-d`); this
    script called `--home`, which GNU shadow-utils useradd does not
    recognize at all (confirmed directly: `useradd --home /tmp/x ...`
    prints its usage/option list and exits 2 on this machine). Verified
    here by asserting the exact argument list a fake useradd receives."""

    def _harness(self, tmp: str) -> tuple[Path, Path]:
        bindir = Path(tmp) / "bin"
        bindir.mkdir()
        argv_log = Path(tmp) / "useradd-argv.log"
        _write_executable(bindir / "sudo", 'exec "$@"\n')  # drop "sudo" itself, run the real argv
        _write_executable(bindir / "useradd", f'printf "%s\\n" "$*" >> "{argv_log}"\nexit 0\n')
        _write_executable(bindir / "id", 'exit 1\n')  # "no such user" -> creation path is taken
        _write_executable(bindir / "getent", 'exit 1\n')  # no matching group -> usermod is skipped
        return bindir, argv_log

    def test_useradd_is_called_with_home_dir_not_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bindir, argv_log = self._harness(tmp)
            env = {"PATH": f"{bindir}:{os.environ['PATH']}", "ASSUME_YES": "1"}
            result = run_snippet('create_service_user "/var/lib/synos-conformance" "/usr/bin/podman"', env=env)
            self.assertEqual(0, result.returncode, result.stderr)
            argv = argv_log.read_text(encoding="utf-8").strip()
            self.assertIn("--home-dir /var/lib/synos-conformance", argv)
            self.assertNotIn("--home /var/lib/synos-conformance", argv)
            # and not the bare long option either, which useradd also rejects
            self.assertNotRegex(argv, r"--home(?!-dir)\b")


class ExplicitStorageDryRunTests(unittest.TestCase):
    """select_storage_path's create=0 mode (this installer's own
    --dry-run): an explicit path that does not exist yet must be described,
    never created."""

    def test_a_nonexistent_explicit_path_is_never_created_and_its_existing_ancestor_is_measured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bindir = Path(tmp) / "bin"
            bindir.mkdir()
            ancestor = Path(tmp) / "mnt-big"
            ancestor.mkdir()
            fake_df_stat(bindir, [(str(ancestor), 5000 * 1048576, "ext4")])
            target = ancestor / "not-created-yet" / "storage"
            env = {"PATH": f"{bindir}:{os.environ['PATH']}"}
            result = run_snippet(f'select_storage_path 40 "{target}" 0', env=env)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertFalse(target.exists())
            self.assertIn("does not exist yet; would be created", result.stderr)
            self.assertIn(str(ancestor), result.stderr)
            self.assertEqual(str(target), result.stdout.split()[0])

    def test_create_1_the_default_still_creates_it_for_a_real_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bindir = Path(tmp) / "bin"
            bindir.mkdir()
            target = Path(tmp) / "really-create-me"
            fake_df_stat(bindir, [(str(target), 5000 * 1048576, "ext4")])
            env = {"PATH": f"{bindir}:{os.environ['PATH']}"}
            result = run_snippet(f'select_storage_path 40 "{target}"', env=env)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertTrue(target.is_dir())


class BrowserVerificationTests(unittest.TestCase):
    """verify_browser distinguishes "nothing on PATH at all" (exit 2) from "a
    binary exists but will not launch headless" (exit 1), so
    verify_installation can name the first case precisely instead of a
    generic FAIL — the class of gap a plain Ubuntu chromium/chromium-browser
    (a transitional package onto the Chromium snap) would otherwise hit
    silently."""

    def _empty_bindir(self, tmp: str) -> Path:
        """A PATH directory carrying only bash itself — this sandbox host
        happens to have a real chromium (via snap) and google-chrome already
        installed, so an isolated PATH (not the real one) is what actually
        exercises "no browser found"."""
        bindir = Path(tmp) / "bin"
        bindir.mkdir()
        real_bash = shutil.which("bash")
        assert real_bash, "bash must be on PATH to run these tests at all"
        (bindir / "bash").symlink_to(real_bash)
        return bindir

    def test_returns_2_when_no_candidate_binary_is_on_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bindir = self._empty_bindir(tmp)
            env = {"PATH": str(bindir)}  # deliberately excludes the real PATH
            result = run_snippet("verify_browser; echo $?", env=env)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual("2", result.stdout.strip())

    def test_returns_1_when_a_binary_exists_but_will_not_launch_headless(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bindir = self._empty_bindir(tmp)
            _write_executable(bindir / "chromium", "exit 1\n")
            _write_executable(bindir / "timeout", 'shift\n"$@"\n')
            env = {"PATH": f"{bindir}:{os.environ['PATH']}"}
            result = run_snippet("verify_browser; echo $?", env=env)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual("1", result.stdout.strip())

    def _stub_other_checks(self) -> str:
        """Every verify_* function verify_installation calls besides
        verify_browser, redefined as a trivial pass — isolates the browser
        message this test actually cares about from the rest of
        verify_installation's real, environment-dependent checks."""
        return "\n".join(
            f"{name}() {{ return 0; }}"
            for name in ("verify_runtime", "verify_storage_location", "verify_qemu",
                         "verify_ocr", "verify_free_space", "verify_service_dry_run")
        )

    def test_missing_browser_gets_specific_actionable_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bindir = self._empty_bindir(tmp)
            env = {"PATH": str(bindir)}
            body = self._stub_other_checks() + '\nverify_installation "podman" "/tmp" "/tmp" "/tmp/c.yml" "python3" 1'
            result = run_snippet(body, env=env)
            self.assertNotEqual(0, result.returncode)
            self.assertIn("FAIL  headless browser starts", result.stdout)
            self.assertIn("chromium, chromium-browser, google-chrome or google-chrome-stable", result.stdout)
            self.assertIn("test-engine", result.stdout)
            self.assertIn("snap", result.stdout)

    def test_a_present_but_broken_browser_gets_the_plain_fail_not_the_missing_guidance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bindir = self._empty_bindir(tmp)
            _write_executable(bindir / "chromium", "exit 1\n")
            _write_executable(bindir / "timeout", 'shift\n"$@"\n')
            env = {"PATH": f"{bindir}:{os.environ['PATH']}"}
            body = self._stub_other_checks() + '\nverify_installation "podman" "/tmp" "/tmp" "/tmp/c.yml" "python3" 1'
            result = run_snippet(body, env=env)
            self.assertNotEqual(0, result.returncode)
            self.assertIn("FAIL  headless browser starts", result.stdout)
            self.assertNotIn("chromium, chromium-browser, google-chrome or google-chrome-stable", result.stdout)


if __name__ == "__main__":
    unittest.main()
