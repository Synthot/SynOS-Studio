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
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "packaging" / "install-test-engine.sh"


def _write_executable(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/bash\n{body}", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def run_snippet(body: str, env: dict | None = None) -> subprocess.CompletedProcess:
    """Sources the real script, then runs `body` — one function call or a
    short sequence of them — in a fresh bash subprocess."""
    script = f'source "{SCRIPT}"\n{body}\n'
    full_env = dict(os.environ)
    full_env.update(env or {})
    return subprocess.run(["bash", "-c", script], text=True, capture_output=True, env=full_env, cwd=str(ROOT))


def fake_df_stat(bindir: Path, filesystems: list[tuple[str, int, str]]) -> None:
    """filesystems: (mountpoint, avail_kb, fstype) tuples. Installs a fake
    `df` answering both `df -Pk --output=target,avail,fstype` (the "list
    every real filesystem" call) and `df -Pk <path>` (the "how much room
    at this one path" call, matched by prefix so a path under a mountpoint
    still resolves to it), and a fake `stat -f -c %T <path>` answering the
    same way."""
    listing = "\\n".join(f"{m}\\t{a}\\t{t}" for m, a, t in filesystems)
    df_lines = ["if [[ \"$*\" == *\"--output=target,avail,fstype\"* ]]; then",
                f"    printf 'Filesystem\\tAvail\\tType\\n{listing}\\n'", "    exit 0", "fi",
                'path="${@: -1}"', "case \"$path\" in"]
    stat_lines = ['path="${@: -1}"', "case \"$path\" in"]
    for mount, avail, fstype in filesystems:
        df_lines.append(f'    "{mount}"*) echo "Filesystem 1024-blocks Used Available Capacity Mounted"; '
                         f'echo "fake 0 0 {avail} 1% {mount}" ;;')
        stat_lines.append(f'    "{mount}"*) echo "{fstype}" ;;')
    df_lines += ["    *) echo \"Filesystem 1024-blocks Used Available Capacity Mounted\"; echo \"fake 0 0 0 1% ?\" ;;", "esac"]
    stat_lines += ["    *) echo ext4 ;;", "esac"]
    _write_executable(bindir / "df", "\n".join(df_lines) + "\n")
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
    def _fake_environment(self, tmp: str) -> tuple[Path, Path]:
        bindir = Path(tmp) / "bin"
        bindir.mkdir(exist_ok=True)
        for name in ("podman", "qemu-system-x86_64", "xorriso", "tesseract", "chromium",
                     "sudo", "systemctl", "useradd", "usermod", "getent"):
            _write_executable(bindir / name, "exit 0\n")
        _write_executable(bindir / "nproc", "echo 4\n")
        fake_df_stat(bindir, [("/mnt/big", 5000 * 1048576, "ext4")])
        osr = Path(tmp) / "os-release"
        osr.write_text("ID=debian\n", encoding="utf-8")
        return bindir, osr

    def _run(self, tmp: str, extra_args: list[str] | None = None) -> tuple[subprocess.CompletedProcess, Path]:
        bindir, osr = self._fake_environment(tmp)
        config_path = Path(tmp) / "conformance.yml"
        env = dict(os.environ)
        env["PATH"] = f"{bindir}:{env['PATH']}"
        env["SYNOS_OS_RELEASE_FILE"] = str(osr)
        args = ["bash", str(SCRIPT), "--dry-run", "--yes",
                f"--engine-root={ROOT}", "--site-url=https://studio.example",
                f"--config={config_path}", f"--unit-dir={tmp}/units"] + (extra_args or [])
        result = subprocess.run(args, capture_output=True, text=True, env=env, cwd=str(ROOT), check=False)
        return result, config_path

    def test_dry_run_prints_every_action_and_performs_none_of_them(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result, config_path = self._run(tmp)
            self.assertEqual(0, result.returncode, result.stderr + result.stdout)
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
            bindir, osr = self._fake_environment(tmp)
            osr.write_text("ID=solaris\n", encoding="utf-8")
            env = dict(os.environ)
            env["PATH"] = f"{bindir}:{env['PATH']}"
            env["SYNOS_OS_RELEASE_FILE"] = str(osr)
            config_path = Path(tmp) / "conformance.yml"
            result = subprocess.run(
                ["bash", str(SCRIPT), "--dry-run", "--yes", f"--engine-root={ROOT}",
                 "--site-url=https://studio.example", f"--config={config_path}", f"--unit-dir={tmp}/units"],
                capture_output=True, text=True, env=env, cwd=str(ROOT), check=False,
            )
            self.assertEqual(3, result.returncode)
            self.assertIn("not recognized", result.stderr)
            self.assertFalse(config_path.exists())


if __name__ == "__main__":
    unittest.main()
