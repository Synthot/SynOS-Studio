"""Container services (and every other role that installs packages from the
archive) failed near the end of a real build:

    TASK [synos.workstation.container_services : podman for the container
    services] fatal: [...]: FAILED! => {"changed": false, "msg": "No package
    matching 'netavark' is available"}

mods/85-cleanup-mod deletes /var/lib/apt/lists to keep the squashfs small,
and it used to run as just another numbered mod inside run_chroot -- before
run_ansible_chroot applied the profile. The fix makes the order a stated
contract: build.sh runs the cleanup mod as its own phase, after the profile
is applied (see docs/ARCHITECTURE.md, step 7), and every package-installing
role includes apt_cache_ready first as a second, independent line of
defence. No real apt or ansible-playbook is needed for any of this: the
loop and the script are plain POSIX shell, and the failure message is a
Jinja2 template we can render directly, the same way
test_profile_files.py's RoleTests render a role's raw task file."""
from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

import jinja2
import yaml

ROOT = Path(__file__).resolve().parents[2]
COLLECTION = ROOT / "ansible/collections/ansible_collections/synos/workstation"
APT_CACHE_READY = COLLECTION / "roles/apt_cache_ready"
CONTAINER_SERVICES = COLLECTION / "roles/container_services"


class BuildOrderTests(unittest.TestCase):
    """The order is a stated contract in build.sh, not mod-number luck."""

    def test_main_runs_cleanup_after_the_profile_is_applied(self) -> None:
        build = (ROOT / "build.sh").read_text(encoding="utf-8")
        main = build.split("# =============   main  ================")[1]
        calls = [line.strip() for line in main.splitlines() if line.strip() and not line.strip().startswith("#")]
        # setup_apt, run_chroot, run_ansible_chroot, run_cleanup_mod, umount_folders, ...
        self.assertIn("run_chroot", calls)
        self.assertIn("run_ansible_chroot", calls)
        self.assertIn("run_cleanup_mod", calls)
        self.assertIn("umount_folders", calls)
        self.assertLess(calls.index("run_ansible_chroot"), calls.index("run_cleanup_mod"),
                         "the profile must be applied before the cleanup mod deletes the apt cache")
        self.assertLess(calls.index("run_cleanup_mod"), calls.index("umount_folders"),
                         "cleanup must be the last thing that touches the mounted chroot")

    def test_run_chroot_only_runs_the_early_mods(self) -> None:
        build = (ROOT / "build.sh").read_text(encoding="utf-8")
        run_chroot = build.split("function run_chroot()")[1].split("function ")[0]
        self.assertIn("install_all_mods.sh early", run_chroot)
        self.assertNotIn("install_all_mods.sh -", run_chroot)

    def test_run_cleanup_mod_runs_only_the_cleanup_phase(self) -> None:
        build = (ROOT / "build.sh").read_text(encoding="utf-8")
        run_cleanup = build.split("function run_cleanup_mod()")[1].split("function ")[0]
        self.assertIn("install_all_mods.sh cleanup", run_cleanup)

    def test_nothing_after_cleanup_touches_apt(self) -> None:
        """Everything from umount_folders onward: only dpkg-query (reads the
        installed database, not the apt cache) and unmount calls may
        reference apt or the chroot."""
        build = (ROOT / "build.sh").read_text(encoding="utf-8")
        # Function bodies only: stop before the "main" call list at the
        # bottom, which legitimately calls a function named setup_apt.
        tail = build.split("function umount_folders()")[1].split("# =============   main  ================")[0]
        for line in tail.splitlines():
            if "apt" not in line:
                continue
            allowed = (
                "var/cache/apt/archives" in line  # unmounting the bind-mounted download cache
                or "dpkg-query" in line
                or line.strip().startswith("#")
            )
            self.assertTrue(allowed, f"unexpected apt touch after cleanup: {line!r}")


class InstallAllModsPhaseTests(unittest.TestCase):
    """The mods loop itself (mods/install_all_mods.sh): 'early' skips
    85-cleanup-mod, 'cleanup' runs only it, and either way the file-order
    scan for mods is unchanged. Exercised for real (not just read as text)
    against a throwaway mods/ directory, so this is a behavioural test of
    the actual loop code, not a restatement of it."""

    def _run_phase(self, phase: str) -> list[str]:
        with tempfile.TemporaryDirectory() as d:
            mods_dir = Path(d)
            for name in ("01-alpha", "85-cleanup-mod", "90-omega"):
                mod = mods_dir / name
                mod.mkdir()
                (mod / "install.sh").write_text("#!/bin/bash\necho \"ran:$(basename \"$PWD\")\"\n", encoding="utf-8")

            script = (ROOT / "mods/install_all_mods.sh").read_text(encoding="utf-8")
            loop = script.split("CLEANUP_MOD=", 1)[1]
            harness = textwrap.dedent(f"""\
                #!/bin/bash
                set -e -u -o pipefail
                print_info() {{ :; }}
                SCRIPT_DIR="{mods_dir}"
                CLEANUP_MOD={loop}
            """)
            harness_path = mods_dir.parent / "harness.sh"
            harness_path.write_text(harness, encoding="utf-8")
            result = subprocess.run(
                ["bash", str(harness_path), phase],
                text=True, capture_output=True, check=True)
            return re.findall(r"ran:(\S+)", result.stdout)

    def test_early_phase_skips_the_cleanup_mod(self) -> None:
        ran = self._run_phase("early")
        self.assertEqual(["01-alpha", "90-omega"], ran)

    def test_cleanup_phase_runs_only_the_cleanup_mod(self) -> None:
        ran = self._run_phase("cleanup")
        self.assertEqual(["85-cleanup-mod"], ran)

    def test_no_phase_argument_runs_every_mod_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            mods_dir = Path(d)
            for name in ("01-alpha", "85-cleanup-mod", "90-omega"):
                mod = mods_dir / name
                mod.mkdir()
                (mod / "install.sh").write_text("#!/bin/bash\necho \"ran:$(basename \"$PWD\")\"\n", encoding="utf-8")
            script = (ROOT / "mods/install_all_mods.sh").read_text(encoding="utf-8")
            loop = script.split("CLEANUP_MOD=", 1)[1]
            harness = textwrap.dedent(f"""\
                #!/bin/bash
                set -e -u -o pipefail
                print_info() {{ :; }}
                SCRIPT_DIR="{mods_dir}"
                CLEANUP_MOD={loop}
            """)
            harness_path = mods_dir.parent / "harness.sh"
            harness_path.write_text(harness, encoding="utf-8")
            result = subprocess.run(["bash", str(harness_path)], text=True, capture_output=True, check=True)
            self.assertEqual(["01-alpha", "85-cleanup-mod", "90-omega"], re.findall(r"ran:(\S+)", result.stdout))


class AptCacheReadyScriptTests(unittest.TestCase):
    """files/ensure_apt_cache.sh: the actual shipped script, run for real
    against a throwaway lists directory with a fake apt-get on PATH -- no
    real apt or network needed."""

    SCRIPT = APT_CACHE_READY / "files/ensure_apt_cache.sh"

    def _fake_apt_get(self, tmp: Path, log: Path) -> Path:
        fake = tmp / "bin"
        fake.mkdir(exist_ok=True)
        apt_get = fake / "apt-get"
        apt_get.write_text(
            f'#!/bin/sh\necho "called:$*" >> "{log}"\nexit 0\n', encoding="utf-8")
        apt_get.chmod(apt_get.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        return fake

    def _run(self, lists_dir: Path, fake_bin: Path) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["SYNOS_APT_LISTS_DIR"] = str(lists_dir)
        env["PATH"] = f"{fake_bin}:{env['PATH']}"
        return subprocess.run(["sh", str(self.SCRIPT)], text=True, capture_output=True, env=env, check=True)

    def test_refreshes_when_the_index_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            lists = tmp / "lists"
            (lists / "lock").parent.mkdir(parents=True)
            (lists / "lock").touch()
            (lists / "partial").mkdir()
            log = tmp / "apt-get.log"
            fake_bin = self._fake_apt_get(tmp, log)
            result = self._run(lists, fake_bin)
            self.assertIn("running apt-get update", result.stdout)
            self.assertEqual("called:update\n", log.read_text(encoding="utf-8"))

    def test_does_not_refresh_when_the_index_is_present(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            lists = tmp / "lists"
            lists.mkdir()
            (lists / "lock").touch()
            (lists / "partial").mkdir()
            (lists / "archive.ubuntu.com_ubuntu_dists_noble_universe_binary-amd64_Packages").touch()
            log = tmp / "apt-get.log"
            fake_bin = self._fake_apt_get(tmp, log)
            result = self._run(lists, fake_bin)
            self.assertIn("already present; not refreshing", result.stdout)
            self.assertFalse(log.exists(), "apt-get must not run when the cache is already warm")

    def test_an_empty_directory_counts_as_missing(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            lists = tmp / "lists"
            lists.mkdir()
            log = tmp / "apt-get.log"
            fake_bin = self._fake_apt_get(tmp, log)
            self._run(lists, fake_bin)
            self.assertEqual("called:update\n", log.read_text(encoding="utf-8"))


class ContainerServicesRoleTests(unittest.TestCase):
    """The role at the centre of the real failure: podman and netavark."""

    def _tasks(self) -> list[dict]:
        return yaml.safe_load((CONTAINER_SERVICES / "tasks/main.yml").read_text(encoding="utf-8"))

    def test_apt_cache_ready_runs_before_the_package_install(self) -> None:
        tasks = self._tasks()
        names = [t["name"] for t in tasks]
        cache_idx = next(i for i, t in enumerate(tasks) if "apt_cache_ready" in str(t.get("ansible.builtin.include_role", "")))
        install_idx = next(i for i, t in enumerate(tasks) if "block" in t and any("podman" in b["name"] for b in t["block"]))
        self.assertLess(cache_idx, install_idx, names)

    def test_the_install_is_rescued_with_a_fail_task(self) -> None:
        tasks = self._tasks()
        install_task = next(t for t in tasks if "block" in t and any("podman" in b["name"] for b in t["block"]))
        self.assertIn("rescue", install_task)
        fail_tasks = [t for t in install_task["rescue"] if "ansible.builtin.fail" in t]
        self.assertEqual(1, len(fail_tasks))

    def test_the_failure_message_names_the_package_and_the_component(self) -> None:
        tasks = self._tasks()
        install_task = next(t for t in tasks if "block" in t and any("podman" in b["name"] for b in t["block"]))
        fail_task = next(t for t in install_task["rescue"] if "ansible.builtin.fail" in t)
        msg_template = fail_task["ansible.builtin.fail"]["msg"]

        env = jinja2.Environment()
        env.filters["regex_replace"] = lambda s, pattern, repl: re.sub(pattern, repl, s)
        template = env.from_string(msg_template)

        # The real failure from the bug report.
        rendered = template.render(
            ansible_failed_result={"msg": "No package matching 'netavark' is available"},
            synos_container_services_apt_components={"stdout_lines": ["Components: main restricted universe multiverse"]},
        )
        self.assertIn("netavark", rendered)
        self.assertIn("podman", rendered)
        self.assertIn("universe", rendered)
        self.assertIn("main restricted universe multiverse", rendered)

        # Also render cleanly when the sources grep finds nothing (no crash,
        # still a usable message).
        rendered_empty = template.render(
            ansible_failed_result={"msg": "No package matching 'netavark' is available"},
            synos_container_services_apt_components={"stdout_lines": []},
        )
        self.assertIn("netavark", rendered_empty)
        self.assertIn("none found", rendered_empty)


class OtherPackageInstallingRolesTests(unittest.TestCase):
    """Requirement 3: everything downstream of cleanup that installs
    packages needs the same safety net, not only container_services."""

    ROLES_AND_FIRST_INSTALL_TASK = {
        "base_hardening": "Hardening packages",
        "workplace_apps": "Install packages",
        "directory_join": "Directory client packages",
        "fleet_enrollment": "ansible-pull is available on the machine",
        "desktop_branding": "Install the brand kit",
    }

    def test_each_role_includes_apt_cache_ready_before_its_first_apt_install(self) -> None:
        for role, first_install_name in self.ROLES_AND_FIRST_INSTALL_TASK.items():
            with self.subTest(role=role):
                tasks = yaml.safe_load((COLLECTION / "roles" / role / "tasks/main.yml").read_text(encoding="utf-8"))
                names = [t["name"] for t in tasks]
                cache_idx = next(
                    i for i, t in enumerate(tasks)
                    if "apt_cache_ready" in str(t.get("ansible.builtin.include_role", "")))
                install_idx = names.index(first_install_name)
                self.assertLess(cache_idx, install_idx, names)


class AptCacheReadyRoleWellFormedTests(unittest.TestCase):
    def test_role_files_exist_and_parse(self) -> None:
        tasks = yaml.safe_load((APT_CACHE_READY / "tasks/main.yml").read_text(encoding="utf-8"))
        self.assertIsInstance(tasks, list)
        self.assertTrue(all("name" in t for t in tasks))
        yaml.safe_load((APT_CACHE_READY / "defaults/main.yml").read_text(encoding="utf-8"))

    def test_script_is_executable(self) -> None:
        mode = (APT_CACHE_READY / "files/ensure_apt_cache.sh").stat().st_mode
        self.assertTrue(mode & stat.S_IXUSR)


if __name__ == "__main__":
    unittest.main()
