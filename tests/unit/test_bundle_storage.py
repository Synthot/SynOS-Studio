"""bundle_launcher.sh/.ps1's container storage: where a podman build keeps
its images, layers and chroot. Split out of test_bundle.py to keep that
file under the reviewable line-count cap (tests/unit/test_architecture.py);
shares its fixtures (MANIFEST, write_bundle, write_executable, run_under_pty)
rather than duplicating them."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from test_bundle import MANIFEST, ROOT, run_under_pty, write_bundle, write_executable


class _StorageHelpers:
    """Fixtures shared by ContainerStorageTests and ResetStorageTests - a
    plain mixin, not a TestCase, so its methods are never collected and run
    twice under both class names."""

    TOOLS = ("sh", "sed", "head", "awk", "df", "id", "uname", "grep", "ls", "cat", "tr",
             "dirname", "printf", "stat", "mkdir")
    # A full (non-"check") build walks further than the storage checks: the
    # self-refresh probe, dist/ bookkeeping, the run itself.
    BUILD_TOOLS = TOOLS + ("cp", "chmod", "tar", "date", "tee", "rm", "mv")

    def make_fake_bin(self, tmp: Path, tools: tuple[str, ...] | None = None) -> Path:
        fake = tmp / "bin"
        fake.mkdir()
        for tool in (tools or self.TOOLS):
            found = shutil.which(tool)
            if found:
                (fake / tool).symlink_to(found)
        return fake

    def write_root_id(self, fake: Path) -> None:
        """A fake `id` reporting uid/gid 0, so the podman branch (which runs
        through sudo for a real, non-root user) needs neither real root nor
        an interactive sudo prompt to exercise here."""
        real_id = shutil.which("id")
        (fake / "id").unlink()
        write_executable(fake / "id", "#!/bin/sh\ncase \"$1\" in\n  -u) echo 0 ;;\n  -g) echo 0 ;;\n"
                                       f"  *) exec '{real_id}' \"$@\" ;;\nesac\n")

    def write_vfat_stat(self, fake: Path) -> None:
        """A fake `stat -f -c %T` reporting vfat for every path - the bundle's
        own disk cannot back a container's overlay, the one case a real
        choice remains once the bundle's disk is the default."""
        (fake / "stat").unlink()
        write_executable(fake / "stat", "#!/bin/sh\necho vfat\n")

    def write_logging_runtime(self, fake: Path, name: str, log: Path) -> None:
        """A fake podman/docker that records every invocation's argv, one
        line per call, and always succeeds."""
        write_executable(fake / name, f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> '{log}'\nexit 0\n")

    def write_docker_reporting_store(self, fake: Path, log: Path, store: Path) -> None:
        write_executable(fake / "docker", (
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$*\" >> '{log}'\n"
            "if [ \"$1\" = info ] && [ \"$2\" = --format ]; then\n"
            "    case \"$3\" in\n"
            f"        *DockerRootDir*) printf '%s\\n' '{store}' ;;\n"
            "        *) printf '\\n' ;;\n"
            "    esac\n"
            "fi\n"
            "exit 0\n"
        ))

    def write_podman_reporting_store(self, fake: Path, log: Path, store: Path) -> None:
        """A fake podman answering `info --format {{.Store.GraphRoot}}` with
        `store` (and recording every call); used for the storage question,
        which asks this before deciding whether there is a real question."""
        write_executable(fake / "podman", (
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$*\" >> '{log}'\n"
            "if [ \"$1\" = info ] && [ \"$2\" = --format ]; then\n"
            "    case \"$3\" in\n"
            f"        *GraphRoot*) printf '%s\\n' '{store}' ;;\n"
            "        *) printf '\\n' ;;\n"
            "    esac\n"
            "fi\n"
            "exit 0\n"
        ))

    def write_df(self, fake: Path, marker_path: str, marker_avail_kb: int, default_avail_kb: int = 100 * 1024 * 1024) -> None:
        """A fake `df -Pk PATH` that reports marker_avail_kb for marker_path
        and a large default for everything else (in particular ".", the
        bundle's own 40 GB check), so a test controls exactly one number
        without depending on the real disk this runs on."""
        (fake / "df").unlink()
        write_executable(fake / "df", (
            "#!/bin/sh\n"
            "path=$2\n"
            f"if [ \"$path\" = '{marker_path}' ]; then avail={marker_avail_kb}; else avail={default_avail_kb}; fi\n"
            "printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\\n'\n"
            "printf '/dev/fake 1 1 %s 1%% %s\\n' \"$avail\" \"$path\"\n"
        ))

    def make_bundle(self, tmp: Path) -> Path:
        bundle = write_bundle(tmp / "bundle", {"format": 1, "manifest": "manifests/x.yml", "engine": {"min": "0.1.0"}},
                              {"manifests/x.yml": MANIFEST})
        shutil.copy(ROOT / "tools" / "bundle_launcher.sh", bundle / "build.sh")
        return bundle


class ContainerStorageTests(_StorageHelpers, unittest.TestCase):
    """SYNOS_CONTAINER_ROOT/SYNOS_CONTAINER_RUNROOT (and --container-root=/--container-runroot=):
    podman takes a per-build storage location with no root; docker's is
    daemon-wide and is refused plainly instead of silently ignored or built
    into a failure. Every runtime here is a fake that logs its argv and exits
    0; no real build is ever started."""

    def test_a_bundle_cannot_name_an_engine_minimum_that_is_a_path(self) -> None:
        """bundle.json is data, and its engine.min is read straight out of it into a
        download URL and - when GitHub's release list cannot be reached, which is the
        documented fallback - into the name of the directory the engine source is
        extracted to and later removed from, with sudo when the files inside are
        root-owned. A value that is not a version is refused while it is still just a
        string, so nothing a bundle says can ever reach outside .build/engine-src/."""
        for bad in ("../../etc", "0.1.0/../..", "..", "0.1.0;rm -rf /", "0.1."):
            with self.subTest(min=bad), tempfile.TemporaryDirectory() as tmp:
                tmp = Path(tmp)
                bundle = write_bundle(tmp / "bundle",
                                      {"format": 1, "manifest": "manifests/x.yml", "engine": {"min": bad}},
                                      {"manifests/x.yml": MANIFEST})
                shutil.copy(ROOT / "tools" / "bundle_launcher.sh", bundle / "build.sh")
                fake = self.make_fake_bin(tmp)
                env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_NO_UPDATE_CHECK": "1", "SYNOS_YES": "1"}
                result = subprocess.run(["sh", "build.sh", "check"], cwd=bundle, env=env,
                                        capture_output=True, text=True, stdin=subprocess.DEVNULL)
                self.assertEqual(1, result.returncode, result.stdout + result.stderr)
                self.assertIn("not a version", result.stderr)
                self.assertIn(bad, result.stderr, "the refusal quotes what the bundle actually said")

    def test_the_tree_removal_helper_refuses_anything_outside_the_extraction_directory(self) -> None:
        """clean_dir() removes a whole tree, through sudo when a previous build left
        root-owned files in it, so it does not trust its caller: it is lifted out of the
        real launcher here and exercised directly, since the only paths that reach it in
        the launcher itself are the ones it is meant to accept."""
        text = (ROOT / "tools" / "bundle_launcher.sh").read_text(encoding="utf-8")
        start = text.index("clean_dir() {")
        end = text.index("\n}\n", start) + 3
        harness = ("fail() { printf 'error: %s\\n' \"$1\" >&2; exit \"${2:-1}\"; }\n"
                   + text[start:end] + '\nclean_dir "$1"\necho removed\n')
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            script = tmp / "harness.sh"
            script.write_text(harness, encoding="utf-8")
            keep = tmp / "keep-me"
            keep.mkdir()
            (keep / "file").write_text("not yours to delete", encoding="utf-8")
            for bad in (str(keep), "/", "..", ".build/engine-src", ".build/engine-src/../../keep-me",
                        "dist", ".build/engine-src//x"):
                with self.subTest(path=bad):
                    result = subprocess.run(["sh", str(script), bad], cwd=tmp, capture_output=True, text=True)
                    self.assertEqual(2, result.returncode, result.stdout + result.stderr)
                    self.assertIn("refusing to remove", result.stderr)
            self.assertTrue((keep / "file").is_file(), "a refused path is never touched")
            target = tmp / ".build" / "engine-src" / "v0.3.0"
            target.mkdir(parents=True)
            (target / "inside").write_text("x", encoding="utf-8")
            result = subprocess.run(["sh", str(script), ".build/engine-src/v0.3.0"], cwd=tmp,
                                    capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertFalse(target.exists(), "the one shape it accepts is actually removed")

    def test_podman_receives_root_and_runroot_docker_never_would(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_bundle(tmp)
            fake = self.make_fake_bin(tmp)
            self.write_root_id(fake)
            container_root = tmp / "storage"
            container_runroot = tmp / "runroot"
            log = tmp / "runtime.log"
            self.write_logging_runtime(fake, "podman", log)
            self.write_df(fake, str(container_root), 100 * 1024 * 1024)
            env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_NO_UPDATE_CHECK": "1", "SYNOS_YES": "1",
                   "SYNOS_CONTAINER_ROOT": str(container_root), "SYNOS_CONTAINER_RUNROOT": str(container_runroot)}
            result = subprocess.run(["sh", "build.sh", "check"], cwd=bundle, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            calls = log.read_text(encoding="utf-8").splitlines()
            self.assertTrue(calls, "podman must have been invoked")
            for call in calls:
                self.assertIn(f"--root {container_root} --runroot {container_runroot}", call, call)
            self.assertTrue(container_root.is_dir())
            self.assertTrue(container_runroot.is_dir())
            self.assertIn(f"--root {container_root} --runroot {container_runroot}", result.stdout)

    def test_default_lands_beside_the_bundle_with_nothing_set(self) -> None:
        """The default needs nothing: no flag, no variable, no prior answer.
        On podman the bundle's own disk is where the storage goes, said once,
        plainly, on the run that creates the directory."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_bundle(tmp)
            fake = self.make_fake_bin(tmp)
            self.write_root_id(fake)
            log = tmp / "runtime.log"
            self.write_logging_runtime(fake, "podman", log)
            self.write_df(fake, str(tmp / "never-matched"), 1)
            env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_NO_UPDATE_CHECK": "1", "SYNOS_YES": "1"}
            bundle_store = bundle / ".build" / "container-storage"
            result = subprocess.run(["sh", "build.sh", "check"], cwd=bundle, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            calls = log.read_text(encoding="utf-8").splitlines()
            self.assertTrue(calls, "podman must have been invoked")
            for call in calls:
                self.assertIn(f"--root {bundle_store}", call, call)
            self.assertIn(f"this build keeps its storage under {bundle_store}", result.stdout)
            self.assertIn("use --storage=<path>", result.stdout)
            self.assertTrue(bundle_store.is_dir())
            # automatic, not a decision: nothing is written for the next run to read back
            self.assertFalse((bundle / ".build" / "container-root").exists())

            # a second run finds the directory already there and says nothing about it
            log.write_text("", encoding="utf-8")
            again = subprocess.run(["sh", "build.sh", "check"], cwd=bundle, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(0, again.returncode, again.stdout + again.stderr)
            self.assertNotIn("this build keeps its storage under", again.stdout)
            self.assertTrue(any(f"--root {bundle_store}" in c for c in log.read_text(encoding="utf-8").splitlines()))

    def test_docker_is_unaffected_by_the_new_default_even_on_a_vfat_bundle_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_bundle(tmp)
            fake = self.make_fake_bin(tmp)
            self.write_vfat_stat(fake)
            log = tmp / "runtime.log"
            self.write_logging_runtime(fake, "docker", log)
            self.write_df(fake, str(tmp / "never-matched"), 1)
            env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_NO_UPDATE_CHECK": "1", "SYNOS_YES": "1"}
            result = subprocess.run(["sh", "build.sh", "check"], cwd=bundle, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertNotIn("this build keeps its storage under", result.stdout)
            self.assertNotIn("cannot back a container's overlay", result.stdout + result.stderr)
            for call in log.read_text(encoding="utf-8").splitlines():
                self.assertNotIn("--root", call)
            self.assertFalse((bundle / ".build" / "container-root").exists())
            self.assertFalse((bundle / ".build" / "container-storage").exists())

    def test_docker_refuses_a_requested_location_before_any_command_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_bundle(tmp)
            fake = self.make_fake_bin(tmp)
            container_root = tmp / "storage"
            log = tmp / "runtime.log"
            self.write_logging_runtime(fake, "docker", log)
            env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_NO_UPDATE_CHECK": "1",
                   "SYNOS_CONTAINER_ROOT": str(container_root)}
            result = subprocess.run(["sh", "build.sh", "check"], cwd=bundle, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(2, result.returncode, result.stdout + result.stderr)
            self.assertFalse(log.exists() and log.read_text(encoding="utf-8").strip(), "docker must never be invoked")
            self.assertIn("daemon-wide location", result.stderr)
            self.assertIn("data-root", result.stderr)
            self.assertIn("/etc/docker/daemon.json", result.stderr)
            self.assertIn("/var/snap/docker/current/config/daemon.json", result.stderr)
            self.assertIn("snap connect docker:removable-media", result.stderr)
            self.assertIn("install podman", result.stderr)

    def test_podman_low_space_refusal_names_the_numbers_and_puts_the_rootless_option_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_bundle(tmp)
            fake = self.make_fake_bin(tmp)
            self.write_root_id(fake)
            container_root = tmp / "storage"
            log = tmp / "runtime.log"
            self.write_logging_runtime(fake, "podman", log)
            self.write_df(fake, str(container_root), 8 * 1024 * 1024)  # 8 GB, below the 30 GB threshold
            env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_NO_UPDATE_CHECK": "1", "SYNOS_YES": "1",
                   "SYNOS_CONTAINER_ROOT": str(container_root)}
            result = subprocess.run(["sh", "build.sh", "check"], cwd=bundle, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(2, result.returncode, result.stdout + result.stderr)
            message = result.stderr
            self.assertIn("needs 30 GB free", message)
            self.assertIn("only 8 GB is free", message)
            self.assertIn(str(container_root), message)
            self.assertIn("no root needed", message)
            self.assertLess(message.index("no root needed"), message.index("move podman's own default storage"),
                            "the option that needs no root must come first")
            self.assertFalse(log.exists() and log.read_text(encoding="utf-8").strip(), "no podman call before the refusal")

    def test_docker_low_space_refusal_names_the_numbers_and_both_daemon_json_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_bundle(tmp)
            fake = self.make_fake_bin(tmp)
            store_dir = tmp / "docker-storage"
            store_dir.mkdir()
            log = tmp / "runtime.log"
            self.write_docker_reporting_store(fake, log, store_dir)
            self.write_df(fake, str(store_dir), 5 * 1024 * 1024)  # 5 GB, below the 30 GB threshold
            env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_NO_UPDATE_CHECK": "1", "SYNOS_YES": "1"}
            result = subprocess.run(["sh", "build.sh", "check"], cwd=bundle, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(2, result.returncode, result.stdout + result.stderr)
            message = result.stderr
            self.assertIn("needs 30 GB free", message)
            self.assertIn("only 5 GB is free", message)
            self.assertIn(str(store_dir), message)
            self.assertIn("install podman", message)
            self.assertIn("plain install", message)
            self.assertIn("/etc/docker/daemon.json", message)
            self.assertIn("snap install", message)
            self.assertIn("/var/snap/docker/current/config/daemon.json", message)
            self.assertIn("snap connect docker:removable-media", message)

    def test_vfat_bundle_disk_prompt_wording_and_enter_switches_to_podmans_storage(self) -> None:
        """The prompt now has a real, two-sided question only once the
        bundle's own disk cannot back an overlay at all: asked once, with
        the facts, the fix (podman's own storage) offered as the default
        answer Enter accepts."""
        try:
            import pty  # noqa: F401
        except ImportError:
            self.skipTest("the pty module is not available on this platform")
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_bundle(tmp)
            fake = self.make_fake_bin(tmp, self.BUILD_TOOLS)
            self.write_root_id(fake)
            self.write_vfat_stat(fake)
            store_dir = tmp / "default-store"
            store_dir.mkdir()
            log = tmp / "runtime.log"
            self.write_podman_reporting_store(fake, log, store_dir)
            self.write_df(fake, "never-matched", 1)  # every path (bundle disk included) gets the 100 GB default
            env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_NO_UPDATE_CHECK": "1"}
            output = run_under_pty(bundle, env, ["sh", "build.sh"], send_when_seen="[Y/n]", send=b"\n")
            self.assertIn(f"this bundle's own disk is vfat, which cannot back a container's overlay filesystem; "
                          f"podman's own storage at {store_dir} (100 GB free) can.", output)
            self.assertIn("Use podman's own storage there instead? [Y/n]", output)
            calls = log.read_text(encoding="utf-8").splitlines()
            self.assertFalse(any("--root" in c for c in calls), calls)
            self.assertEqual("default", (bundle / ".build" / "container-root").read_text(encoding="utf-8").strip())

    def test_declining_the_vfat_prompt_keeps_the_bundle_disk_and_then_refuses(self) -> None:
        try:
            import pty  # noqa: F401
        except ImportError:
            self.skipTest("the pty module is not available on this platform")
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_bundle(tmp)
            fake = self.make_fake_bin(tmp, self.BUILD_TOOLS)
            self.write_root_id(fake)
            self.write_vfat_stat(fake)
            store_dir = tmp / "default-store"
            store_dir.mkdir()
            log = tmp / "runtime.log"
            self.write_podman_reporting_store(fake, log, store_dir)
            self.write_df(fake, "never-matched", 1)
            env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_NO_UPDATE_CHECK": "1"}
            bundle_store = bundle / ".build" / "container-storage"
            output = run_under_pty(bundle, env, ["sh", "build.sh"], send_when_seen="[Y/n]", send=b"n\n")
            self.assertIn("[Y/n]", output)
            self.assertIn(f"{bundle_store} is vfat, which cannot back a container's overlay filesystem", output)
            calls = log.read_text(encoding="utf-8").splitlines()
            self.assertFalse(any("--root" in c for c in calls), calls)
            self.assertEqual(str(bundle_store), (bundle / ".build" / "container-root").read_text(encoding="utf-8").strip())

    def test_no_prompt_without_a_terminal_even_on_a_vfat_bundle_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_bundle(tmp)
            fake = self.make_fake_bin(tmp)
            self.write_root_id(fake)
            self.write_vfat_stat(fake)
            store_dir = tmp / "default-store"
            store_dir.mkdir()
            log = tmp / "runtime.log"
            self.write_podman_reporting_store(fake, log, store_dir)
            self.write_df(fake, "never-matched", 1)
            env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_NO_UPDATE_CHECK": "1"}
            result = subprocess.run(["sh", "build.sh", "check"], cwd=bundle, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertNotIn("[Y/n]", result.stdout + result.stderr)
            self.assertNotIn("podman's own storage at", result.stdout + result.stderr)
            # unchanged: the existing hard refusal still applies, exactly as before
            self.assertEqual(2, result.returncode)
            self.assertIn("cannot back a container's overlay filesystem", result.stderr)
            self.assertFalse((bundle / ".build" / "container-root").exists())

    def test_no_prompt_with_yes_even_on_a_vfat_bundle_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_bundle(tmp)
            fake = self.make_fake_bin(tmp)
            self.write_root_id(fake)
            self.write_vfat_stat(fake)
            store_dir = tmp / "default-store"
            store_dir.mkdir()
            log = tmp / "runtime.log"
            self.write_podman_reporting_store(fake, log, store_dir)
            self.write_df(fake, "never-matched", 1)
            env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_NO_UPDATE_CHECK": "1", "SYNOS_YES": "1"}
            result = subprocess.run(["sh", "build.sh", "check"], cwd=bundle, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertNotIn("[Y/n]", result.stdout + result.stderr)
            self.assertNotIn("podman's own storage at", result.stdout + result.stderr)
            self.assertEqual(2, result.returncode)
            self.assertIn("cannot back a container's overlay filesystem", result.stderr)

    def test_no_prompt_in_check_mode_even_with_a_terminal(self) -> None:
        try:
            import pty  # noqa: F401
        except ImportError:
            self.skipTest("the pty module is not available on this platform")
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_bundle(tmp)
            fake = self.make_fake_bin(tmp)
            self.write_root_id(fake)
            self.write_vfat_stat(fake)
            store_dir = tmp / "default-store"
            store_dir.mkdir()
            log = tmp / "runtime.log"
            self.write_podman_reporting_store(fake, log, store_dir)
            self.write_df(fake, "never-matched", 1)
            env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_NO_UPDATE_CHECK": "1"}
            # nothing ever prompts here, so wait on the line "check" always prints instead
            output = run_under_pty(bundle, env, ["sh", "build.sh", "check"], send_when_seen="error:", send=b"\n", timeout=8)
            self.assertNotIn("[Y/n]", output)
            self.assertNotIn("podman's own storage at", output)
            self.assertIn("cannot back a container's overlay filesystem", output)

    def test_remembered_choice_is_reused_without_asking_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_bundle(tmp)
            fake = self.make_fake_bin(tmp)
            self.write_root_id(fake)
            remembered = tmp / "remembered-storage"
            (bundle / ".build").mkdir()
            (bundle / ".build" / "container-root").write_text(str(remembered) + "\n", encoding="utf-8")
            log = tmp / "runtime.log"
            self.write_logging_runtime(fake, "podman", log)
            self.write_df(fake, str(remembered), 100 * 1024 * 1024)
            env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_NO_UPDATE_CHECK": "1", "SYNOS_YES": "1"}
            result = subprocess.run(["sh", "build.sh", "check"], cwd=bundle, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertNotIn("[Y/n]", result.stdout)
            self.assertIn(f"using the remembered build storage: {remembered}", result.stdout)
            self.assertIn("--storage=<path>", result.stdout)
            calls = log.read_text(encoding="utf-8").splitlines()
            self.assertTrue(any(f"--root {remembered}" in c for c in calls), calls)

    def test_remembered_default_choice_is_reused_and_named(self) -> None:
        """The "keep podman's own storage" answer is remembered too, and
        named on later runs just like a custom path would be."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_bundle(tmp)
            fake = self.make_fake_bin(tmp)
            self.write_root_id(fake)
            (bundle / ".build").mkdir()
            (bundle / ".build" / "container-root").write_text("default\n", encoding="utf-8")
            log = tmp / "runtime.log"
            self.write_logging_runtime(fake, "podman", log)
            self.write_df(fake, "never-matched", 1)
            env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_NO_UPDATE_CHECK": "1", "SYNOS_YES": "1"}
            result = subprocess.run(["sh", "build.sh", "check"], cwd=bundle, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn("using podman's own storage for this build, not this bundle's disk", result.stdout)
            for call in log.read_text(encoding="utf-8").splitlines():
                self.assertNotIn("--root", call)
            self.assertFalse((bundle / ".build" / "container-storage").exists())

    def test_container_root_flag_overrides_the_remembered_value_and_updates_it(self) -> None:
        """The older --container-root spelling still works, and still wins."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_bundle(tmp)
            fake = self.make_fake_bin(tmp)
            self.write_root_id(fake)
            remembered = tmp / "old-storage"
            new_root = tmp / "new-storage"
            (bundle / ".build").mkdir()
            (bundle / ".build" / "container-root").write_text(str(remembered) + "\n", encoding="utf-8")
            log = tmp / "runtime.log"
            self.write_logging_runtime(fake, "podman", log)
            self.write_df(fake, str(new_root), 100 * 1024 * 1024)
            env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_NO_UPDATE_CHECK": "1", "SYNOS_YES": "1"}
            result = subprocess.run(["sh", "build.sh", "check", f"--container-root={new_root}"], cwd=bundle, env=env,
                                    capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            calls = log.read_text(encoding="utf-8").splitlines()
            self.assertTrue(any(f"--root {new_root}" in c for c in calls), calls)
            self.assertFalse(any(str(remembered) in c for c in calls), calls)
            self.assertEqual(str(new_root), (bundle / ".build" / "container-root").read_text(encoding="utf-8").strip())

    def test_storage_flag_both_spellings_override_a_remembered_value(self) -> None:
        """--storage=PATH and --storage PATH (the current spelling, item 149)
        both win over a remembered value, same as the older --container-root."""
        for form in ("equals", "space"):
            with self.subTest(form=form), tempfile.TemporaryDirectory() as tmp:
                tmp = Path(tmp)
                bundle = self.make_bundle(tmp)
                fake = self.make_fake_bin(tmp)
                self.write_root_id(fake)
                remembered = tmp / "old-storage"
                new_root = tmp / "new-storage"
                (bundle / ".build").mkdir()
                (bundle / ".build" / "container-root").write_text(str(remembered) + "\n", encoding="utf-8")
                log = tmp / "runtime.log"
                self.write_logging_runtime(fake, "podman", log)
                self.write_df(fake, str(new_root), 100 * 1024 * 1024)
                env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_NO_UPDATE_CHECK": "1", "SYNOS_YES": "1"}
                argv = ["sh", "build.sh", "check", f"--storage={new_root}"] if form == "equals" \
                    else ["sh", "build.sh", "check", "--storage", str(new_root)]
                result = subprocess.run(argv, cwd=bundle, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                calls = log.read_text(encoding="utf-8").splitlines()
                self.assertTrue(any(f"--root {new_root}" in c for c in calls), calls)
                self.assertFalse(any(str(remembered) in c for c in calls), calls)
                self.assertEqual(str(new_root), (bundle / ".build" / "container-root").read_text(encoding="utf-8").strip())

    def test_environment_variable_still_works_but_the_flag_wins_over_it(self) -> None:
        """SYNOS_CONTAINER_ROOT keeps working for unattended use (item 150),
        and a --storage flag on the same run still wins over it."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_bundle(tmp)
            fake = self.make_fake_bin(tmp)
            self.write_root_id(fake)
            env_root = tmp / "env-storage"
            flag_root = tmp / "flag-storage"
            log = tmp / "runtime.log"
            self.write_logging_runtime(fake, "podman", log)
            self.write_df(fake, str(flag_root), 100 * 1024 * 1024)
            env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_NO_UPDATE_CHECK": "1", "SYNOS_YES": "1",
                   "SYNOS_CONTAINER_ROOT": str(env_root)}
            result = subprocess.run(["sh", "build.sh", "check", f"--storage={flag_root}"], cwd=bundle, env=env,
                                    capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            calls = log.read_text(encoding="utf-8").splitlines()
            self.assertTrue(any(f"--root {flag_root}" in c for c in calls), calls)
            self.assertFalse(any(str(env_root) in c for c in calls), calls)

    def test_a_location_on_a_filesystem_that_cannot_back_an_overlay_is_refused_clearly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_bundle(tmp)
            fake = self.make_fake_bin(tmp)
            self.write_root_id(fake)
            log = tmp / "runtime.log"
            self.write_logging_runtime(fake, "podman", log)
            # A fake `stat -f -c %T` reporting a filesystem podman cannot use for an overlay.
            (fake / "stat").unlink()
            write_executable(fake / "stat", "#!/bin/sh\necho vfat\n")
            container_root = tmp / "storage"
            env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_NO_UPDATE_CHECK": "1", "SYNOS_YES": "1",
                   "SYNOS_CONTAINER_ROOT": str(container_root)}
            result = subprocess.run(["sh", "build.sh", "check"], cwd=bundle, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(2, result.returncode, result.stdout + result.stderr)
            self.assertIn("vfat", result.stderr)
            self.assertIn("cannot back a container's overlay filesystem", result.stderr)
            self.assertFalse(log.exists() and log.read_text(encoding="utf-8").strip(), "no podman call before the refusal")

    def test_launcher_script_documents_the_new_flags(self) -> None:
        text = (ROOT / "tools" / "bundle_launcher.sh").read_text(encoding="utf-8")
        for needle in ("SYNOS_CONTAINER_ROOT", "SYNOS_CONTAINER_RUNROOT", "--container-root=", "--container-runroot=",
                       "--storage", "older spelling"):
            self.assertIn(needle, text, needle)

    def test_help_text_names_the_storage_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_bundle(tmp)
            fake = self.make_fake_bin(tmp)
            env = {"PATH": str(fake), "HOME": str(tmp)}
            result = subprocess.run(["sh", "build.sh", "--help"], cwd=bundle, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn("--storage", result.stdout)

    def test_storage_flag_needs_a_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_bundle(tmp)
            fake = self.make_fake_bin(tmp)
            env = {"PATH": str(fake), "HOME": str(tmp)}
            result = subprocess.run(["sh", "build.sh", "check", "--storage"], cwd=bundle, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(1, result.returncode, result.stdout + result.stderr)
            self.assertIn("--storage needs a path", result.stdout + result.stderr)

    def test_a_relative_storage_path_becomes_absolute_in_argv_and_the_report(self) -> None:
        """A relative --storage must stop being relative the moment it is
        accepted (item 158): the runtime never sees it, and neither does the
        person reading the report back."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_bundle(tmp)
            fake = self.make_fake_bin(tmp)
            self.write_root_id(fake)
            log = tmp / "runtime.log"
            self.write_logging_runtime(fake, "podman", log)
            expected = bundle / "storage"
            self.write_df(fake, str(expected), 100 * 1024 * 1024)
            env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_NO_UPDATE_CHECK": "1", "SYNOS_YES": "1"}
            result = subprocess.run(["sh", "build.sh", "check", "--storage", "storage"], cwd=bundle, env=env,
                                    capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            calls = log.read_text(encoding="utf-8").splitlines()
            self.assertTrue(any(f"--root {expected}" in c for c in calls), calls)
            self.assertNotIn("--root storage", result.stdout)
            self.assertIn(str(expected), result.stdout)
            self.assertEqual(str(expected), (bundle / ".build" / "container-root").read_text(encoding="utf-8").strip())

    def test_an_already_absolute_storage_path_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_bundle(tmp)
            fake = self.make_fake_bin(tmp)
            self.write_root_id(fake)
            log = tmp / "runtime.log"
            self.write_logging_runtime(fake, "podman", log)
            absolute = tmp / "elsewhere" / "storage"
            self.write_df(fake, str(absolute), 100 * 1024 * 1024)
            env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_NO_UPDATE_CHECK": "1", "SYNOS_YES": "1"}
            result = subprocess.run(["sh", "build.sh", "check", f"--storage={absolute}"], cwd=bundle, env=env,
                                    capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            calls = log.read_text(encoding="utf-8").splitlines()
            self.assertTrue(any(f"--root {absolute}" in c for c in calls), calls)

    def test_storage_path_with_a_space_survives_every_hop(self) -> None:
        """--root/--runroot are real quoted arguments, not a string rebuilt
        and word-split (item 161): a space in the path must not split it
        into two arguments."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_bundle(tmp)
            fake = self.make_fake_bin(tmp)
            self.write_root_id(fake)
            log = tmp / "runtime.log"
            # Records argc and each argument on its own line, so a path with
            # a space arriving as two arguments (broken quoting) is visible.
            write_executable(fake / "podman", (
                "#!/bin/sh\n"
                f"{{ printf 'ARGC=%d\\n' \"$#\"; for a in \"$@\"; do printf 'ARG=[%s]\\n' \"$a\"; done; }} >> '{log}'\n"
                "exit 0\n"
            ))
            spaced = tmp / "my storage"
            self.write_df(fake, str(spaced), 100 * 1024 * 1024)
            env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_NO_UPDATE_CHECK": "1", "SYNOS_YES": "1"}
            result = subprocess.run(["sh", "build.sh", "check", f"--storage={spaced}"], cwd=bundle, env=env,
                                    capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn(f"ARG=[{spaced}]", log.read_text(encoding="utf-8"))

    def test_storage_inside_the_engine_source_is_refused_naming_both_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_bundle(tmp)
            fake = self.make_fake_bin(tmp)
            self.write_root_id(fake)
            log = tmp / "runtime.log"
            self.write_logging_runtime(fake, "podman", log)
            self.write_df(fake, "never-matched", 1)
            engine_checkout = tmp / "engine-checkout"
            engine_checkout.mkdir()
            inside = engine_checkout / "storage"
            env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_NO_UPDATE_CHECK": "1", "SYNOS_YES": "1",
                   "SYNOS_ENGINE_SOURCE": str(engine_checkout)}
            result = subprocess.run(["sh", "build.sh", "check", f"--storage={inside}"], cwd=bundle, env=env,
                                    capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(2, result.returncode, result.stdout + result.stderr)
            self.assertIn(str(inside), result.stderr)
            self.assertIn(str(engine_checkout), result.stderr)
            self.assertIn("copies, archives or hashes whole", result.stderr)
            self.assertFalse(log.exists() and log.read_text(encoding="utf-8").strip(), "no podman call before the refusal")

    def test_storage_inside_dist_is_refused_naming_both_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_bundle(tmp)
            fake = self.make_fake_bin(tmp)
            self.write_root_id(fake)
            log = tmp / "runtime.log"
            self.write_logging_runtime(fake, "podman", log)
            self.write_df(fake, "never-matched", 1)
            env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_NO_UPDATE_CHECK": "1", "SYNOS_YES": "1"}
            result = subprocess.run(["sh", "build.sh", "check", "--storage", "dist/storage"], cwd=bundle, env=env,
                                    capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(2, result.returncode, result.stdout + result.stderr)
            self.assertIn(str(bundle / "dist" / "storage"), result.stderr)
            self.assertIn(str(bundle / "dist"), result.stderr)
            self.assertIn("copies, archives or hashes whole", result.stderr)

    def test_storage_inside_the_downloaded_engine_source_is_refused_before_anything_is_created(self) -> None:
        """The collision that actually destroyed a real build: `--storage storage`
        run from a bundle, resolving inside .build/engine-src/<checkout>/, which
        the builder image then COPYs whole -- the build copied its own live
        storage into itself and died with "broken pipe" and "operation not
        permitted" deep inside the image build. .build/engine-src/ is a fixed
        path, so it is refused up front: before the directory is created, and
        before the location is remembered for the next run."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_bundle(tmp)
            fake = self.make_fake_bin(tmp)
            self.write_root_id(fake)
            log = tmp / "runtime.log"
            self.write_logging_runtime(fake, "podman", log)
            self.write_df(fake, "never-matched", 1)
            env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_NO_UPDATE_CHECK": "1", "SYNOS_YES": "1"}
            requested = ".build/engine-src/SynOS-Studio-main/storage"
            result = subprocess.run(["sh", "build.sh", "check", "--storage", requested], cwd=bundle, env=env,
                                    capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(2, result.returncode, result.stdout + result.stderr)
            self.assertIn(str(bundle / requested), result.stderr, "the refusal names the storage path it resolved")
            self.assertIn(str(bundle / ".build" / "engine-src"), result.stderr, "and the copied context it sits inside")
            self.assertIn("copies, archives or hashes whole", result.stderr)
            self.assertFalse((bundle / requested).exists(), "refused before the directory is created")
            self.assertFalse((bundle / ".build" / "container-root").exists(), "and before anything is remembered for the next run")

    def test_default_storage_is_still_accepted_and_is_not_refused(self) -> None:
        """The automatic default (.build/container-storage) must not trip
        the new refusal: it is a sibling of dist/ and of any engine source,
        never inside either."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_bundle(tmp)
            fake = self.make_fake_bin(tmp)
            self.write_root_id(fake)
            log = tmp / "runtime.log"
            self.write_logging_runtime(fake, "podman", log)
            self.write_df(fake, "never-matched", 1)
            env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_NO_UPDATE_CHECK": "1", "SYNOS_YES": "1"}
            result = subprocess.run(["sh", "build.sh", "check"], cwd=bundle, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            expected = bundle / ".build" / "container-storage"
            self.assertTrue(any(f"--root {expected}" in c for c in log.read_text(encoding="utf-8").splitlines()))

    def test_a_context_already_containing_podman_storage_is_refused_naming_path_and_removal(self) -> None:
        """Item 171: debris an earlier run already left behind - podman's
        own storage shape, confirmed against a real corrupted bundle:
        overlay/, overlay-containers/, overlay-images/, overlay-layers/,
        libpod/, db.sql, defaultNetworkBackend - is refused wherever it
        sits, naming both the debris and the path to remove."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_bundle(tmp)
            fake = self.make_fake_bin(tmp)
            self.write_root_id(fake)
            log = tmp / "runtime.log"
            self.write_logging_runtime(fake, "podman", log)
            self.write_df(fake, "never-matched", 1)
            engine_checkout = tmp / "engine-checkout"
            engine_checkout.mkdir()
            (engine_checkout / "bases").mkdir()
            (engine_checkout / "overlay").mkdir()
            (engine_checkout / "libpod").mkdir()
            (engine_checkout / "db.sql").write_bytes(b"")
            env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_NO_UPDATE_CHECK": "1", "SYNOS_YES": "1",
                   "SYNOS_ENGINE_SOURCE": str(engine_checkout)}
            result = subprocess.run(["sh", "build.sh", "check"], cwd=bundle, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(2, result.returncode, result.stdout + result.stderr)
            self.assertIn(str(engine_checkout), result.stderr)
            self.assertIn("already contains podman's own storage", result.stderr)
            self.assertIn(f"rm -rf {engine_checkout}", result.stderr)
            self.assertFalse(log.exists() and log.read_text(encoding="utf-8").strip(), "no podman call before the refusal")

    def test_podman_storage_debris_one_level_inside_the_context_is_also_caught(self) -> None:
        """The field case: a previous --storage landed the debris in a
        subdirectory of the context (a name nothing here controls), not
        at the context's own root."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_bundle(tmp)
            fake = self.make_fake_bin(tmp)
            self.write_root_id(fake)
            log = tmp / "runtime.log"
            self.write_logging_runtime(fake, "podman", log)
            self.write_df(fake, "never-matched", 1)
            engine_checkout = tmp / "engine-checkout"
            engine_checkout.mkdir()
            (engine_checkout / "bases").mkdir()
            debris = engine_checkout / "storage"
            debris.mkdir()
            (debris / "overlay-layers").mkdir()
            (debris / "defaultNetworkBackend").write_bytes(b"")
            env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_NO_UPDATE_CHECK": "1", "SYNOS_YES": "1",
                   "SYNOS_ENGINE_SOURCE": str(engine_checkout)}
            result = subprocess.run(["sh", "build.sh", "check"], cwd=bundle, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(2, result.returncode, result.stdout + result.stderr)
            self.assertIn(str(debris), result.stderr)
            self.assertIn("already contains podman's own storage", result.stderr)
            self.assertIn(f"rm -rf {debris}", result.stderr)

    def test_a_clean_engine_source_is_not_refused_by_the_storage_scan(self) -> None:
        """Negative control: an ordinary checkout, with no podman storage
        markers anywhere in it, must never trip this refusal."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_bundle(tmp)
            fake = self.make_fake_bin(tmp, self.BUILD_TOOLS)
            self.write_root_id(fake)
            log = tmp / "runtime.log"
            self.write_logging_runtime(fake, "podman", log)
            self.write_df(fake, "never-matched", 1)
            engine_checkout = tmp / "engine-checkout"
            (engine_checkout / "bases" / "ubuntu").mkdir(parents=True)
            (engine_checkout / "bases" / "ubuntu" / "Containerfile").write_text("FROM scratch\n", encoding="utf-8")
            (engine_checkout / "storage-notes.txt").write_text("not podman storage\n", encoding="utf-8")
            env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_NO_UPDATE_CHECK": "1", "SYNOS_YES": "1",
                   "SYNOS_ENGINE_SOURCE": str(engine_checkout)}
            result = subprocess.run(["sh", "build.sh", "check"], cwd=bundle, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertNotIn("already contains podman's own storage", result.stdout + result.stderr)

    def test_dirty_extracted_engine_source_directory_is_cleaned_before_reextraction(self) -> None:
        """Item 170: a content-addressed extraction directory left dirty by
        an earlier, pre-fix launcher (no completion marker, and poisoned
        with podman storage debris inside it, some of it root-owned) is
        never reused - it is removed (escalating through the same sudo
        path the runtime itself already uses) and extracted again from
        scratch, rather than the build silently walking into it."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_bundle(tmp)
            fake = self.make_fake_bin(tmp, self.BUILD_TOOLS + ("gzip", "sha256sum", "tail", "cut"))
            self.write_root_id(fake)
            log = tmp / "runtime.log"
            # Both "pull" (no published image) and "image inspect" (nothing
            # built here yet) must fail, so the run actually reaches the
            # extraction logic instead of returning early on a cache hit.
            # --root/--runroot (podman's default storage location) are
            # prepended before the real subcommand, so this matches "$*"
            # rather than a fixed positional argument.
            write_executable(fake / "podman", (
                "#!/bin/sh\n"
                f"printf '%s\\n' \"$*\" >> '{log}'\n"
                "case \"$*\" in\n"
                "  *' pull '*) exit 1 ;;\n"
                "  *' image '*) exit 1 ;;\n"
                "  *) exit 0 ;;\n"
                "esac\n"
            ))
            self.write_df(fake, "never-matched", 1)

            import hashlib
            import io
            import tarfile
            data = b"FROM scratch\n"
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:gz") as tf:
                info = tarfile.TarInfo(name="x/bases/ubuntu/Containerfile")
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
            archive_bytes = buf.getvalue()
            source_id = hashlib.sha256(archive_bytes).hexdigest()[:12]

            engine_src_dir = tmp / "engine-src"
            engine_src_dir.mkdir()
            archive_file = engine_src_dir / "engine-src.tar.gz"
            archive_file.write_bytes(archive_bytes)
            curl_script = (
                "#!/bin/sh\n"
                "out=\"\"; prev=\"\"\n"
                "for a in \"$@\"; do\n"
                "    if [ \"$prev\" = \"-o\" ]; then out=$a; fi\n"
                "    prev=$a\n"
                "done\n"
                f"[ -n \"$out\" ] && cp '{archive_file}' \"$out\"\n"
                "printf '200'\n"
            )
            write_executable(fake / "curl", curl_script)

            # a dirty leftover from an earlier run: no .ok marker, and
            # podman's own storage debris sitting inside it
            dirty = bundle / ".build" / "engine-src" / source_id
            dirty.mkdir(parents=True)
            (dirty / "overlay").mkdir()
            (dirty / "libpod").mkdir()
            (dirty / "leftover-file").write_text("stale\n", encoding="utf-8")

            env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_NO_UPDATE_CHECK": "1", "SYNOS_YES": "1",
                   "SYNOS_CHANNEL": "development"}
            result = subprocess.run(["sh", "build.sh"], cwd=bundle, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertFalse((dirty / "overlay").exists(), "the dirty tree's debris must not survive re-extraction")
            self.assertFalse((dirty / "leftover-file").exists())
            self.assertTrue((dirty / "bases" / "ubuntu" / "Containerfile").exists(), "a real extraction ran")
            self.assertTrue((dirty.parent / f"{source_id}.ok").exists(), "a completion marker is written this time")

    def test_poisoned_relative_remembered_value_resolves_bundle_relative_and_is_rewritten_absolute(self) -> None:
        """Item 173: `.build/container-root` holding a bare relative name
        (written by a launcher older than the absolute-path fix) is not
        discarded - the person chose that name on purpose, and discarding
        it would silently move their storage to a location they never
        chose, exactly when this script is trying hardest not to do that.
        It is resolved against the bundle, used for this run, and the file
        itself is rewritten absolute so it is never ambiguous again."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_bundle(tmp)
            fake = self.make_fake_bin(tmp)
            self.write_root_id(fake)
            (bundle / ".build").mkdir()
            (bundle / ".build" / "container-root").write_text("storage\n", encoding="utf-8")
            log = tmp / "runtime.log"
            self.write_logging_runtime(fake, "podman", log)
            expected = bundle / "storage"
            self.write_df(fake, str(expected), 100 * 1024 * 1024)
            env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_NO_UPDATE_CHECK": "1", "SYNOS_YES": "1"}
            result = subprocess.run(["sh", "build.sh", "check"], cwd=bundle, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            calls = log.read_text(encoding="utf-8").splitlines()
            self.assertTrue(any(f"--root {expected}" in c for c in calls), calls)
            self.assertEqual(str(expected), (bundle / ".build" / "container-root").read_text(encoding="utf-8").strip(),
                             "the remembered file is rewritten absolute the first time it is read")

    def test_storage_under_the_bundle_but_outside_the_copied_context_still_works(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_bundle(tmp)
            fake = self.make_fake_bin(tmp)
            self.write_root_id(fake)
            log = tmp / "runtime.log"
            self.write_logging_runtime(fake, "podman", log)
            expected = bundle / ".build" / "mystuff"
            self.write_df(fake, str(expected), 100 * 1024 * 1024)
            env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_NO_UPDATE_CHECK": "1", "SYNOS_YES": "1"}
            result = subprocess.run(["sh", "build.sh", "check", "--storage", ".build/mystuff"], cwd=bundle, env=env,
                                    capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertTrue(any(f"--root {expected}" in c for c in log.read_text(encoding="utf-8").splitlines()))


DEBRIS_NAMES = ("overlay", "overlay-containers", "overlay-images", "overlay-layers",
                "libpod", "db.sql", "defaultNetworkBackend", "storage.lock", "userns.lock")


class ResetStorageTests(_StorageHelpers, unittest.TestCase):
    """./build.sh reset-storage (item 181): the fix a storage-state mismatch
    (item 180) tells a person to run, in one command instead of a block of
    sudo rm -rf pasted from a chat."""

    def make_full_bundle(self, tmp: Path) -> Path:
        """A bundle with real content beside bundle.json/manifests/, the
        same shape reset-storage must never touch: profiles/, branding/,
        keys/ and README.md, none of them named like any storage debris."""
        bundle = self.make_bundle(tmp)
        (bundle / "profiles").mkdir()
        (bundle / "profiles" / "example.yml").write_text("id: example\n", encoding="utf-8")
        (bundle / "branding").mkdir()
        (bundle / "branding" / "logo.svg").write_text("<svg/>", encoding="utf-8")
        (bundle / "keys").mkdir()
        (bundle / "keys" / "signing.pub").write_text("KEY", encoding="utf-8")
        (bundle / "README.md").write_text("this bundle\n", encoding="utf-8")
        return bundle

    def write_debris(self, bundle: Path) -> None:
        for name in DEBRIS_NAMES:
            path = bundle / name
            if name in ("db.sql", "defaultNetworkBackend", "storage.lock", "userns.lock"):
                path.write_bytes(b"debris")
            else:
                path.mkdir()
                (path / "inside").write_bytes(b"debris")

    def test_a_storage_path_with_a_space_is_one_path_and_its_neighbour_survives(self) -> None:
        """The removal list is handed to rm -rf, so a space in a storage location must
        not split it in two. With the list kept as a space-separated string,
        "<bundle>/My Storage" became "<bundle>/My" plus "Storage": it deleted a
        neighbouring directory that was nobody's storage and left the real one behind.
        The decoy here is exactly that neighbour."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_full_bundle(tmp)
            fake = self.make_fake_bin(tmp, self.BUILD_TOOLS)
            self.write_root_id(fake)
            self.write_logging_runtime(fake, "podman", tmp / "runtime.log")
            spaced = bundle / "My Storage"
            (spaced / "overlay").mkdir(parents=True)
            decoy = bundle / "My"
            decoy.mkdir()
            (decoy / "precious").write_text("not storage, not yours to delete\n", encoding="utf-8")
            (bundle / ".build").mkdir()
            (bundle / ".build" / "container-root").write_text(str(spaced) + "\n", encoding="utf-8")
            env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_NO_UPDATE_CHECK": "1", "SYNOS_YES": "1"}
            result = subprocess.run(["sh", "build.sh", "reset-storage"], cwd=bundle, env=env,
                                    capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn(str(spaced), result.stdout, "the path is listed whole, not in pieces")
            self.assertFalse(spaced.exists(), "the storage location with a space in it is what gets removed")
            self.assertTrue((decoy / "precious").is_file(), "its neighbour is untouched")

    def test_a_storage_root_under_any_other_name_is_found_by_its_markers(self) -> None:
        """In the field the damage landed in a directory called "storage", because that
        is what --storage storage resolved to -- a name no debris list would carry. A
        directory in the bundle that is itself a container storage root is removed
        whatever it is called, and the bundle root is never a candidate however many
        markers sit directly in it."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_full_bundle(tmp)
            fake = self.make_fake_bin(tmp, self.BUILD_TOOLS)
            self.write_root_id(fake)
            self.write_logging_runtime(fake, "podman", tmp / "runtime.log")
            oddly_named = bundle / "storage"
            (oddly_named / "libpod").mkdir(parents=True)
            (oddly_named / "overlay").mkdir()
            self.write_debris(bundle)          # markers directly at the bundle root too
            plain = bundle / "notes"
            plain.mkdir()
            (plain / "keep.txt").write_text("mine\n", encoding="utf-8")
            env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_NO_UPDATE_CHECK": "1", "SYNOS_YES": "1"}
            result = subprocess.run(["sh", "build.sh", "reset-storage"], cwd=bundle, env=env,
                                    capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertFalse(oddly_named.exists(), "a storage root is a storage root whatever its name")
            self.assertTrue((plain / "keep.txt").is_file(), "an ordinary directory is not touched")
            self.assertTrue((bundle / "bundle.json").is_file())
            self.assertNotIn(f"  {bundle}\n", result.stdout, "the bundle root itself is never on the removal list")
            self.assertNotIn(f"  {bundle} (", result.stdout)

    def test_reset_storage_needs_no_container_runtime_at_all(self) -> None:
        """A wedged build is exactly when podman may refuse to run -- that is what a
        storage-state mismatch is -- so cleaning up after one must not require a working
        runtime, or a sudo password for one. No podman, no docker, no sudo on PATH."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_full_bundle(tmp)
            fake = self.make_fake_bin(tmp, self.BUILD_TOOLS)   # no runtime written into it
            (bundle / ".build").mkdir()
            (bundle / ".build" / "container-storage" / "overlay").mkdir(parents=True)
            env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_NO_UPDATE_CHECK": "1", "SYNOS_YES": "1"}
            result = subprocess.run(["sh", "build.sh", "reset-storage"], cwd=bundle, env=env,
                                    capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertFalse((bundle / ".build" / "container-storage").exists())
            self.assertNotIn("podman", result.stderr.lower(), "it never asked for a runtime")
            self.assertNotIn("install", result.stdout.lower(), "and never offered to install one")

    def test_removes_exactly_the_owned_paths_and_leaves_bundle_files_alone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_full_bundle(tmp)
            fake = self.make_fake_bin(tmp, self.BUILD_TOOLS)
            self.write_root_id(fake)
            self.write_logging_runtime(fake, "podman", tmp / "runtime.log")

            remembered = tmp / "remembered-storage"
            remembered.mkdir()
            (remembered / "file").write_bytes(b"x")
            (bundle / ".build").mkdir()
            (bundle / ".build" / "container-root").write_text(str(remembered) + "\n", encoding="utf-8")
            (bundle / ".build" / "container-storage").mkdir()
            (bundle / ".build" / "container-storage" / "file").write_bytes(b"x")
            (bundle / ".build" / "engine-src").mkdir()
            (bundle / ".build" / "engine-src" / "somehash").mkdir()
            self.write_debris(bundle)

            env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_NO_UPDATE_CHECK": "1", "SYNOS_YES": "1"}
            result = subprocess.run(["sh", "build.sh", "reset-storage"], cwd=bundle, env=env,
                                    capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)

            self.assertFalse(remembered.exists(), "the remembered location is removed")
            self.assertFalse((bundle / ".build" / "container-root").exists(), "the remembered location is forgotten")
            self.assertFalse((bundle / ".build" / "container-storage").exists())
            self.assertFalse((bundle / ".build" / "engine-src").exists())
            for name in DEBRIS_NAMES:
                self.assertFalse((bundle / name).exists(), name)

            self.assertTrue((bundle / "bundle.json").is_file())
            self.assertTrue((bundle / "manifests" / "x.yml").is_file())
            self.assertTrue((bundle / "profiles" / "example.yml").is_file())
            self.assertTrue((bundle / "branding" / "logo.svg").is_file())
            self.assertTrue((bundle / "keys" / "signing.pub").is_file())
            self.assertTrue((bundle / "README.md").is_file())
            self.assertTrue((bundle / "build.sh").is_file())

    def test_nothing_to_remove_when_the_bundle_has_no_storage_of_its_own(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_full_bundle(tmp)
            fake = self.make_fake_bin(tmp, self.BUILD_TOOLS)
            self.write_root_id(fake)
            self.write_logging_runtime(fake, "podman", tmp / "runtime.log")
            env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_NO_UPDATE_CHECK": "1", "SYNOS_YES": "1"}
            result = subprocess.run(["sh", "build.sh", "reset-storage"], cwd=bundle, env=env,
                                    capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn("nothing to remove", result.stdout)

    def test_prompt_is_asked_without_yes_and_skipped_with_it(self) -> None:
        try:
            import pty  # noqa: F401
        except ImportError:
            self.skipTest("the pty module is not available on this platform")
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_full_bundle(tmp)
            fake = self.make_fake_bin(tmp, self.BUILD_TOOLS)
            self.write_root_id(fake)
            self.write_logging_runtime(fake, "podman", tmp / "runtime.log")
            (bundle / ".build").mkdir()
            (bundle / ".build" / "container-storage").mkdir()
            env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_NO_UPDATE_CHECK": "1"}
            output = run_under_pty(bundle, env, ["sh", "build.sh", "reset-storage"], send_when_seen="[y/N]", send=b"\n")
            self.assertIn("Remove this now? [y/N]", output)
            # bare Enter on a y/N prompt is "no": nothing removed
            self.assertTrue((bundle / ".build" / "container-storage").exists())

            env_yes = dict(env, SYNOS_YES="1")
            result = subprocess.run(["sh", "build.sh", "reset-storage"], cwd=bundle, env=env_yes,
                                    capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertNotIn("[y/N]", result.stdout)
            self.assertFalse((bundle / ".build" / "container-storage").exists())

    def test_declining_the_prompt_removes_nothing(self) -> None:
        try:
            import pty  # noqa: F401
        except ImportError:
            self.skipTest("the pty module is not available on this platform")
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_full_bundle(tmp)
            fake = self.make_fake_bin(tmp, self.BUILD_TOOLS)
            self.write_root_id(fake)
            self.write_logging_runtime(fake, "podman", tmp / "runtime.log")
            (bundle / ".build").mkdir()
            (bundle / ".build" / "container-storage").mkdir()
            self.write_debris(bundle)
            env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_NO_UPDATE_CHECK": "1"}
            output = run_under_pty(bundle, env, ["sh", "build.sh", "reset-storage"], send_when_seen="[y/N]", send=b"n\n")
            self.assertIn("nothing removed", output)
            self.assertTrue((bundle / ".build" / "container-storage").exists())
            for name in DEBRIS_NAMES:
                self.assertTrue((bundle / name).exists(), name)

    def test_no_terminal_and_no_yes_removes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_full_bundle(tmp)
            fake = self.make_fake_bin(tmp, self.BUILD_TOOLS)
            self.write_root_id(fake)
            self.write_logging_runtime(fake, "podman", tmp / "runtime.log")
            (bundle / ".build").mkdir()
            (bundle / ".build" / "container-storage").mkdir()
            env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_NO_UPDATE_CHECK": "1"}
            result = subprocess.run(["sh", "build.sh", "reset-storage"], cwd=bundle, env=env,
                                    capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertIn("nothing removed", result.stdout)
            self.assertTrue((bundle / ".build" / "container-storage").exists())

    def test_an_out_of_bundle_storage_flag_is_refused_and_named(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_full_bundle(tmp)
            fake = self.make_fake_bin(tmp, self.BUILD_TOOLS)
            self.write_root_id(fake)
            self.write_logging_runtime(fake, "podman", tmp / "runtime.log")
            outside = tmp / "somewhere-else"
            outside.mkdir()
            env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_NO_UPDATE_CHECK": "1", "SYNOS_YES": "1"}
            result = subprocess.run(["sh", "build.sh", "reset-storage", "--storage", str(outside)], cwd=bundle, env=env,
                                    capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(2, result.returncode, result.stdout + result.stderr)
            self.assertIn(str(outside), result.stderr)
            self.assertIn("not this bundle's own storage", result.stderr)
            self.assertTrue(outside.exists(), "a refused path is never touched")

    def test_a_storage_flag_matching_the_remembered_value_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_full_bundle(tmp)
            fake = self.make_fake_bin(tmp, self.BUILD_TOOLS)
            self.write_root_id(fake)
            self.write_logging_runtime(fake, "podman", tmp / "runtime.log")
            remembered = tmp / "remembered-storage"
            remembered.mkdir()
            (bundle / ".build").mkdir()
            (bundle / ".build" / "container-root").write_text(str(remembered) + "\n", encoding="utf-8")
            env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_NO_UPDATE_CHECK": "1", "SYNOS_YES": "1"}
            result = subprocess.run(["sh", "build.sh", "reset-storage", "--storage", str(remembered)], cwd=bundle, env=env,
                                    capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            self.assertFalse(remembered.exists())

    def test_storage_mismatch_is_recognised_and_translated_with_podmans_text_shown(self) -> None:
        """Item 180: reproduced directly against a real podman (moving a
        --root out from under an existing storage) - "database static dir
        ... does not match our static dir ...: database configuration
        mismatch" - shown verbatim, then explained, wherever it surfaces."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_bundle(tmp)
            fake = self.make_fake_bin(tmp, self.BUILD_TOOLS)
            self.write_root_id(fake)
            self.write_df(fake, "never-matched", 1)
            mismatch_line = ('Error: database static dir "" does not match our static dir '
                              '"/wherever/libpod": database configuration mismatch')
            write_executable(fake / "podman", (
                "#!/bin/sh\n"
                "case \"$*\" in\n"
                "  *' pull '*) exit 1 ;;\n"
                "  *' image '*) exit 1 ;;\n"
                f"  *' build '*) printf '%s\\n' '{mismatch_line}' >&2; exit 125 ;;\n"
                "  *) exit 0 ;;\n"
                "esac\n"
            ))
            env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_NO_UPDATE_CHECK": "1", "SYNOS_YES": "1",
                   "SYNOS_ENGINE_SOURCE": str(ROOT)}
            result = subprocess.run(["sh", "build.sh"], cwd=bundle, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(2, result.returncode, result.stdout + result.stderr)
            self.assertIn(mismatch_line, result.stderr, "podman's own text is shown, never swallowed")
            self.assertIn("carries state from a different configuration", result.stderr)
            self.assertIn("./build.sh reset-storage", result.stderr)

    def test_storage_mismatch_on_the_final_run_is_also_recognised(self) -> None:
        """Same class of failure, the other place it can surface: the
        container that runs `synos build` itself never starts, so
        dist/build.log (written from inside it) cannot show this - only
        this run's own stderr can, which is why it is captured here too."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bundle = self.make_bundle(tmp)
            fake = self.make_fake_bin(tmp, self.BUILD_TOOLS)
            self.write_root_id(fake)
            self.write_df(fake, "never-matched", 1)
            mismatch_line = ('Error: database run root "/old" does not match our run root '
                              '"/new": database configuration mismatch')
            write_executable(fake / "podman", (
                "#!/bin/sh\n"
                "case \"$*\" in\n"
                "  *' cat /opt/synos/tools/bundle_launcher.sh'*) exit 0 ;;\n"
                "  *' --privileged '*) "
                f"printf '%s\\n' '{mismatch_line}' >&2; exit 125 ;;\n"
                "  *) exit 0 ;;\n"
                "esac\n"
            ))
            env = {"PATH": str(fake), "HOME": str(tmp), "SYNOS_NO_UPDATE_CHECK": "1", "SYNOS_YES": "1",
                   "SYNOS_BUILDER_IMAGE": "my-registry/synos-builder:test"}
            result = subprocess.run(["sh", "build.sh"], cwd=bundle, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertEqual(2, result.returncode, result.stdout + result.stderr)
            self.assertIn(mismatch_line, result.stderr, "podman's own text is shown, never swallowed")
            self.assertIn("carries state from a different configuration", result.stderr)
            self.assertIn("./build.sh reset-storage", result.stderr)


