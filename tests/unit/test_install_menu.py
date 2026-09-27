"""install-test-engine.sh's interactive menu: what a person gets when they run
the script on a terminal with no arguments at all. Every test below drives it
the way a person does -- by feeding it keystrokes -- and asserts on what it
renders and on what it actually did or did not touch on disk.

stderr is folded into stdout (stderr=STDOUT) in every menu run, because the
menu's prompts are printed to stderr on purpose (so a $(...) capture returns
only the answer, and so a piped stdout still leaves the questions on the
operator's terminal): folding them together is the only way to assert on the
order a person actually reads.

Split out of test_install_test_engine.py to keep both files under the
reviewable line-count cap tests/unit/test_architecture.py enforces; it shares
that module's fixtures (the closed PATH, the fake df/stat, the snippet runner)
rather than duplicating them, the same way test_conformance_publish.py shares
test_catalog_conformance.py's.
"""
from __future__ import annotations

import importlib.util
import os
import pty
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

# Shared fixtures, not duplicated. The TestCase classes stay behind the module
# alias on purpose: importing one by name into this module's namespace would
# make unittest collect and run its tests a second time from here.
import test_install_test_engine as engine_tests
from test_install_test_engine import (
    ROOT, SCRIPT, BASH, build_closed_path, fake_df_stat, fake_engine_root,
    fake_nginx, run_snippet,
)


MENU_SYSTEMCTL_DISABLED = 'case "$1" in is-enabled) exit 1 ;; esac\nexit 0\n'
MENU_SYSTEMCTL_ENABLED = 'case "$1" in is-enabled) exit 0 ;; esac\nexit 0\n'


def menu_environment(tmp: str, *, runtime: str = "podman", dependencies: bool = True,
                     timers_enabled: bool = False, assume_tty: bool = True,
                     engine: Path | None = None, config: Path | None = None,
                     units: Path | None = None) -> dict:
    """The closed-PATH environment every menu test runs in (build_closed_path,
    same item-152 reasoning as DryRunTests): `dependencies=False` makes this
    a genuinely bare machine — podman, qemu, xorriso, tesseract and every
    browser candidate really absent, not merely shadowed — and `runtime`
    names which of podman/docker exists when they are present.

    The SYNOS_* variables are the script's own test-only overrides (the same
    convention as SYNOS_NGINX_ROOT/SYNOS_OS_RELEASE_FILE): the menu takes no
    flags by definition — passing one is what makes it *not* run — so these
    are how a test points it at a temporary machine instead of this one.
    SYNOS_MENU_ASSUME_TTY stands in for a terminal; the two tests that must
    prove the terminal check itself is real use a real pty and leave it
    unset."""
    bindir = Path(tmp) / "bin"
    fakes = dict(engine_tests.DryRunTests.RUNTIME_FAKES)
    fakes["systemctl"] = MENU_SYSTEMCTL_ENABLED if timers_enabled else MENU_SYSTEMCTL_DISABLED
    omit = {"podman", "docker", "nginx", "df", "stat"}
    if dependencies:
        fakes[runtime] = "exit 0\n"
    else:
        for absent in ("qemu-system-x86_64", "xorriso", "tesseract", "chromium"):
            fakes.pop(absent, None)
        omit |= {"qemu-system-x86_64", "xorriso", "tesseract", "chromium",
                 "chromium-browser", "google-chrome", "google-chrome-stable"}
    path = build_closed_path(bindir, fakes=fakes, omit=omit)
    # The one big filesystem is the test's own temporary directory, so the
    # storage the menu picks automatically is somewhere it may really write.
    fake_df_stat(bindir, [(tmp, 5000 * 1048576, "ext4")])
    osr = Path(tmp) / "os-release"
    osr.write_text("ID=ubuntu\nID_LIKE=debian\n", encoding="utf-8")
    env = dict(os.environ)
    env["PATH"] = path
    env["SYNOS_OS_RELEASE_FILE"] = str(osr)
    env["SYNOS_ENGINE_ROOT"] = str(engine if engine is not None else Path(tmp) / "not-cloned-yet")
    env["SYNOS_CONFIG_PATH"] = str(config if config is not None else Path(tmp) / "etc" / "conformance.yml")
    env["SYNOS_UNIT_DIR"] = str(units if units is not None else Path(tmp) / "units")
    env["SYNOS_MONITORING_CONFIG"] = str(Path(tmp) / "etc" / "forge.toml")
    env["SYNOS_NGINX_ROOT"] = str(Path(tmp) / "nginx")
    if assume_tty:
        env["SYNOS_MENU_ASSUME_TTY"] = "1"
    else:
        env.pop("SYNOS_MENU_ASSUME_TTY", None)
    return env


def run_menu(tmp: str, keystrokes: str, *, args: tuple[str, ...] = ("--dry-run",),
             **environment) -> subprocess.CompletedProcess:
    env = menu_environment(tmp, **environment)
    return subprocess.run([BASH, str(SCRIPT), *args], input=keystrokes, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          env=env, cwd=str(ROOT), check=False, timeout=180)


def run_on_a_real_terminal(tmp: str, keystrokes: str, *, args: tuple[str, ...] = (),
                           **environment) -> subprocess.CompletedProcess:
    """Runs the script with a real pty on stdin — the only way to prove the
    `[ -t 0 ]` check itself, rather than the override that stands in for it,
    is what opens the menu."""
    env = menu_environment(tmp, assume_tty=False, **environment)
    controller, terminal = pty.openpty()
    try:
        process = subprocess.Popen([BASH, str(SCRIPT), *args], stdin=terminal,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, env=env, cwd=str(ROOT))
        os.close(terminal)
        terminal = -1
        os.write(controller, keystrokes.encode())
        stdout, _ = process.communicate(timeout=180)
    finally:
        if terminal != -1:
            os.close(terminal)
        os.close(controller)
    return subprocess.CompletedProcess(process.args, process.returncode, stdout, "")


def a_written_config(tmp: str, *, dev_site: str, storage: str, workdir: str,
                     path: Path | None = None, mode: int = 0o600) -> Path:
    """A configuration exactly as this installer itself writes one (rendered
    by the real write_config, never a hand-typed lookalike), with a rehearsal
    destination and no production one — the half-installed staging box the
    publish tests start from."""
    rendered = run_snippet(
        f'write_config "https://dev.studio.example/data/catalog.json" "https://dev.studio.example" '
        f'"{workdir}" "{storage}" 5 "2" "synos-conformance" "{dev_site}" "www-data:www-data" "0644"')
    assert rendered.returncode == 0, rendered.stderr
    config = path if path is not None else Path(tmp) / "etc" / "conformance.yml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(rendered.stdout, encoding="utf-8")
    config.chmod(mode)
    return config


class MenuEntryConditionTests(unittest.TestCase):
    """When the menu runs at all: a terminal, no arguments, no -y/--yes.
    Everything else must behave exactly as this script always did — which is
    what makes the flags safe to keep scripting."""

    def test_no_arguments_and_no_terminal_never_enters_the_menu(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = menu_environment(tmp, dependencies=False, assume_tty=False)
            result = subprocess.run([BASH, str(SCRIPT)], stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, env=env, cwd=str(ROOT), check=False, timeout=180)
            self.assertNotIn("Choose 1-9", result.stdout)
            # exactly what it did before the menu existed: no terminal means
            # ask_yes cannot be answered, so the missing packages are a
            # refusal with the operator's own instruction, exit 4
            self.assertEqual(4, result.returncode, result.stdout)
            self.assertIn("install them yourself", result.stdout)

    def test_a_real_terminal_with_no_arguments_opens_the_menu(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_on_a_real_terminal(tmp, "q\n", dependencies=False)
            self.assertEqual(0, result.returncode, result.stdout)
            self.assertIn("SynOS catalog test engine -- installer", result.stdout)
            self.assertIn("Choose 1-9, or q to finish:", result.stdout)
            self.assertIn("nothing was created, updated or changed.", result.stdout)

    def test_dash_yes_on_a_real_terminal_never_opens_the_menu(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_on_a_real_terminal(tmp, "", args=("--dry-run", "--yes"),
                                            engine=fake_engine_root(tmp))
            self.assertNotIn("Choose 1-9", result.stdout)
            self.assertIn("[dry-run]", result.stdout)

    def test_dry_run_on_its_own_opens_the_menu(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_menu(tmp, "q\n", dependencies=False)
            self.assertEqual(0, result.returncode, result.stdout)
            self.assertIn("Choose 1-9, or q to finish:", result.stdout)
            self.assertIn("--dry-run: every choice below prints what it would do", result.stdout)

    def test_overwrite_config_on_its_own_opens_the_menu(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_menu(tmp, "q\n", args=("--overwrite-config",), dependencies=False)
            self.assertEqual(0, result.returncode, result.stdout)
            self.assertIn("Choose 1-9, or q to finish:", result.stdout)

    def test_any_other_flag_means_no_menu_at_all(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_menu(tmp, "q\n", args=("--dry-run", "--site-url=https://studio.example"),
                              engine=fake_engine_root(tmp))
            self.assertNotIn("Choose 1-9", result.stdout)
            self.assertEqual(0, result.returncode, result.stdout)
            self.assertIn("[dry-run]", result.stdout)

    def test_help_still_prints_the_whole_header_including_the_menu(self) -> None:
        result = subprocess.run([BASH, str(SCRIPT), "--help"], capture_output=True, text=True, check=False)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("--engine-root=PATH", result.stdout)          # the first option block
        self.assertIn("Running it with no arguments at all", result.stdout)  # the last one
        self.assertNotIn("set -u", result.stdout)                    # stops at the first non-comment line


class MenuStateTests(unittest.TestCase):
    """The menu doubles as the answer to "what is on this box?" -- so every
    entry's state line is asserted here against a machine whose state the
    test built, for an empty one and a half-installed one, and every one of
    them is derived from the machine (units in UNIT_DIR, git, the
    configuration's own keys, systemctl) rather than from anything this
    script wrote to remember what it did."""

    def test_an_empty_machine_reports_every_entry_as_not_installed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_menu(tmp, "q\n", dependencies=False)
            self.assertEqual(0, result.returncode, result.stdout)
            self.assertRegex(result.stdout, r"1\) Prerequisites.*\n\s+not installed -- missing: .*podman")
            self.assertRegex(result.stdout, r"2\) Engine checkout.*\n\s+not installed -- nothing at .*not-cloned-yet")
            self.assertRegex(result.stdout, r"3\) Test engine configuration.*\n\s+not installed -- no .*conformance\.yml")
            self.assertRegex(result.stdout, r"4\) Local development site.*\n\s+not applicable -- no configuration yet")
            self.assertRegex(result.stdout, r"5\) Publish destinations.*\n\s+not applicable -- no configuration yet")
            self.assertRegex(result.stdout, r"6\) Monitoring console.*\n\s+not installed -- no unit")
            self.assertRegex(result.stdout, r"7\) Enable the timers.*\n\s+not applicable -- the units are not installed yet")

    def test_a_half_installed_machine_reports_each_entry_precisely(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = fake_engine_root(tmp)
            dev_site = Path(tmp) / "www" / "dev"
            dev_site.mkdir(parents=True)
            config = a_written_config(tmp, dev_site=str(dev_site), storage=tmp,
                                      workdir=f"{tmp}/wd")
            units = Path(tmp) / "units"
            units.mkdir()
            for unit in ("synos-conformance-check.service", "synos-conformance-check.timer"):
                (units / unit).write_text(f"[Unit]\nWorkingDirectory={engine}\n", encoding="utf-8")
            result = run_menu(tmp, "q\n", engine=engine, config=config, units=units)
            self.assertEqual(0, result.returncode, result.stdout)
            self.assertRegex(result.stdout, r"1\) Prerequisites.*\n\s+installed -- every dependency present")
            self.assertRegex(result.stdout, r"3\) Test engine configuration.*\n\s+half installed -- configuration: yes, 2 of 4 units")
            self.assertRegex(result.stdout, r"4\) Local development site.*\n\s+installed -- " + str(dev_site))
            self.assertRegex(result.stdout, r"5\) Publish destinations.*\n\s+half installed -- rehearsal only")
            self.assertRegex(result.stdout, r"7\) Enable the timers.*\n\s+not applicable -- the units are not installed yet")

    def test_units_pointing_at_another_checkout_read_as_out_of_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = fake_engine_root(tmp)
            units = Path(tmp) / "units"
            units.mkdir()
            for unit in ("synos-conformance-build.service", "synos-conformance-build.timer",
                         "synos-conformance-check.service", "synos-conformance-check.timer"):
                (units / unit).write_text("[Unit]\nWorkingDirectory=/opt/an-older-checkout\n", encoding="utf-8")
            config = a_written_config(tmp, dev_site=f"{tmp}/www", storage=tmp, workdir=f"{tmp}/wd")
            result = run_menu(tmp, "q\n", engine=engine, config=config, units=units)
            self.assertRegex(result.stdout,
                             r"3\) Test engine configuration.*\n\s+out of date -- 4 units in .* still point at /opt/an-older-checkout")

    def test_enabled_timers_are_reported_as_enabled_from_systemctl_not_a_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = fake_engine_root(tmp)
            units = Path(tmp) / "units"
            units.mkdir()
            for unit in ("synos-conformance-build.service", "synos-conformance-build.timer",
                         "synos-conformance-check.service", "synos-conformance-check.timer"):
                (units / unit).write_text(f"[Unit]\nWorkingDirectory={engine}\n", encoding="utf-8")
            config = a_written_config(tmp, dev_site=f"{tmp}/www", storage=tmp, workdir=f"{tmp}/wd")
            enabled = run_menu(tmp, "q\n", engine=engine, config=config, units=units, timers_enabled=True)
            self.assertRegex(enabled.stdout, r"7\) Enable the timers.*\n\s+installed -- both timers enabled")
            disabled = run_menu(tmp, "q\n", engine=engine, config=config, units=units, timers_enabled=False)
            self.assertRegex(disabled.stdout,
                             r"7\) Enable the timers.*\n\s+not installed -- units in place, both timers still disabled")

    def test_a_dirty_checkout_is_reported_as_untouchable_before_anything_is_attempted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = Path(tmp) / "engine"
            engine.mkdir()
            git_env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
                       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"}
            subprocess.run(["git", "init", "-q", "-b", "main", str(engine)], check=True)
            (engine / "tools").mkdir()
            (engine / "tools" / "catalog_conformance.py").write_text("# stub\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(engine), "add", "-A"], check=True)
            subprocess.run(["git", "-C", str(engine), "commit", "-qm", "one"], check=True, env=git_env)
            (engine / "tools" / "catalog_conformance.py").write_text("# edited by hand\n", encoding="utf-8")
            result = run_menu(tmp, "q\n", engine=engine)
            self.assertRegex(result.stdout,
                             r"2\) Engine checkout.*\n\s+installed, untouchable -- .* has uncommitted changes")


class MenuOneChoiceDoesOneThingTests(unittest.TestCase):
    def test_choosing_the_engine_checkout_touches_nothing_else(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = fake_engine_root(tmp)   # a real directory that is not a checkout: left exactly alone
            config = Path(tmp) / "etc" / "conformance.yml"
            units = Path(tmp) / "units"
            # no --dry-run: this has to prove nothing appeared on disk, not
            # that nothing was printed
            result = run_menu(tmp, "2\n\n\n\nq\n", args=(), runtime="docker",
                              engine=engine, config=config, units=units)
            self.assertEqual(0, result.returncode, result.stdout)
            self.assertIn("engine checkout:", result.stdout)
            self.assertFalse(config.exists(), "the engine choice must not write a configuration")
            self.assertFalse(units.exists(), "the engine choice must not install any unit")
            self.assertNotIn("every dependency is already present", result.stdout)  # no package step
            self.assertNotIn("installed " + str(units), result.stdout)
            self.assertIn("engine checkout (" + str(engine) + "): left-alone", result.stdout)

    def test_an_unanswerable_question_returns_to_the_menu_instead_of_ending_it(self) -> None:
        # the flags refuse a missing catalog URL outright, with exit 2; from
        # the menu that would throw away the whole session over one skipped
        # question, so it says so and comes back to the list
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "etc" / "conformance.yml"
            result = run_menu(tmp, "3\n-\n-\n\n\n\nq\n", args=(), runtime="docker",
                              engine=fake_engine_root(tmp), config=config)
            self.assertEqual(0, result.returncode, result.stdout)
            self.assertIn("cannot run without the catalog JSON URL", result.stdout)
            self.assertFalse(config.exists())
            self.assertIn("Choose 1-9", result.stdout.split("cannot run without")[1])


class MenuEverythingForAStagingServerTests(unittest.TestCase):
    """The one choice the owner actually makes on a fresh staging box: the
    sensible set, in an order where the two answers that belong *in* the
    configuration (the development site, the publish destinations) are
    collected before the configuration is written, so it is written once."""

    KEYSTROKES = (
        "8\n"        # everything for a staging server
        "y\n"        # go through those now?
        "\n"         # runtime storage: the largest disk with room
        "\n\n\n"     # engine checkout path, repository, ref: the defaults
        "/srv/www/dev\n\n\n"                     # development site root, owner, mode
        "y\n\nstudio.example\n\nbuilder\n\n3\n"  # production: protocol, host, port, user, path, credential "not now"
        "https://dev.studio.example\n\n\n\n\n"   # site url, catalog url, entries, jobs, workdir
        "n\n"        # the monitoring console: not this time
        "y\n"        # enable the timers
        "q\n"
    )

    def _run(self, tmp: str) -> subprocess.CompletedProcess:
        return run_menu(tmp, self.KEYSTROKES, engine=fake_engine_root(tmp))

    def test_the_expected_set_runs_in_the_expected_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = self._run(tmp)
            self.assertEqual(0, result.returncode, result.stdout)
            expected_order = [
                "distribution family:",                    # 1. prerequisites
                "container runtime:",
                "engine checkout:",                        # 2. the checkout
                "Development site document root",          # 3. the development site
                "Set up the production destination now?",  # 4. the publish destinations
                "[dry-run] would write",                   # 5. the configuration
                "synos-conformance-build.service",         #    and the units
                "Set up the monitoring console too",       # 6. the console
                "Enable and start both conformance timers",  # 7. the timers
                "summary:",
            ]
            positions = [result.stdout.find(marker) for marker in expected_order]
            for marker, position in zip(expected_order, positions):
                self.assertNotEqual(-1, position, f"{marker!r} never appeared:\n{result.stdout}")
            self.assertEqual(sorted(positions), positions,
                             "the staging sequence ran out of order:\n" + result.stdout)

    def test_the_one_configuration_it_writes_holds_both_destinations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = self._run(tmp)
            self.assertIn('remote_path: "/srv/www/dev/data/build-status.json"', result.stdout)
            self.assertIn("- name: dev-site", result.stdout)
            self.assertIn('host: "studio.example"', result.stdout)
            self.assertEqual(1, result.stdout.count("[dry-run] would write"),
                             "the configuration must be written once, not once per answer")

    def test_dry_run_with_menu_input_changes_nothing_on_disk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = self._run(tmp)
            self.assertEqual(0, result.returncode, result.stdout)
            self.assertFalse((Path(tmp) / "etc" / "conformance.yml").exists())
            self.assertFalse((Path(tmp) / "units").exists())
            self.assertFalse((Path(tmp) / "nginx").exists())
            self.assertFalse(Path("/var/lib/synos-conformance").exists())
            self.assertFalse(Path("/srv/www/dev").exists())
            self.assertIn("[dry-run] skipping verification", result.stdout)


class MenuSecretHandlingTests(unittest.TestCase):
    """The production upload credential: never echoed, never in the output at
    all, never in a unit, and refused outright for a configuration inside a
    checkout. Declining is a first-class answer that still finishes."""

    PASSWORD = "swordfish-must-never-be-printed"

    def _machine(self, tmp: str, *, config_dir: str = "etc", mode: int = 0o600):
        engine = fake_engine_root(tmp)
        dev_site = Path(tmp) / "www" / "dev"
        dev_site.mkdir(parents=True)
        workdir = Path(tmp) / "wd"
        workdir.mkdir()
        config = a_written_config(tmp, dev_site=str(dev_site), storage=tmp, workdir=str(workdir),
                                  path=Path(tmp) / config_dir / "conformance.yml", mode=mode)
        return engine, config

    # protocol, host, port, username, remote path, then the credential choice
    PRODUCTION_ANSWERS = "y\n\nstudio.example\n\nbuilder\n\n"

    def test_a_typed_password_never_appears_in_the_output_but_does_reach_the_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine, config = self._machine(tmp)
            result = run_menu(tmp, "5\n" + self.PRODUCTION_ANSWERS + f"2\n{self.PASSWORD}\n{self.PASSWORD}\nq\n",
                              args=("--overwrite-config",), engine=engine, config=config)
            self.assertEqual(0, result.returncode, result.stdout)
            self.assertNotIn(self.PASSWORD, result.stdout)
            self.assertIn("password accepted", result.stdout)
            written = config.read_text(encoding="utf-8")
            self.assertIn(f'password: "{self.PASSWORD}"', written)
            self.assertIn("- name: production", written)
            # and the one thing the operator is told to do about it
            self.assertIn("chmod 600", result.stdout)

    def test_the_config_file_keeps_the_mode_it_already_had(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine, config = self._machine(tmp, mode=0o600)
            result = run_menu(tmp, "5\n" + self.PRODUCTION_ANSWERS + f"2\n{self.PASSWORD}\n{self.PASSWORD}\nq\n",
                              args=("--overwrite-config",), engine=engine, config=config)
            self.assertEqual(0, result.returncode, result.stdout)
            self.assertEqual(0o600, config.stat().st_mode & 0o777)

    def test_the_written_configuration_is_one_the_uploader_itself_accepts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine, config = self._machine(tmp)
            run_menu(tmp, "5\n" + self.PRODUCTION_ANSWERS + f"2\n{self.PASSWORD}\n{self.PASSWORD}\nq\n",
                     args=("--overwrite-config",), engine=engine, config=config)
            spec = importlib.util.spec_from_file_location("status_uploader", ROOT / "tools" / "status_uploader.py")
            uploader = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = uploader  # dataclasses needs the module importable by name
            spec.loader.exec_module(uploader)
            destinations = uploader.parse_upload_destinations(
                yaml.safe_load(config.read_text(encoding="utf-8"))["upload"])
            self.assertEqual(["dev-site", "production"], [d.name for d in destinations])
            self.assertEqual("local", destinations[0].protocol)   # the rehearsal comes first
            self.assertEqual("sftp", destinations[1].protocol)
            self.assertEqual(22, destinations[1].port)
            self.assertEqual(self.PASSWORD, destinations[1].password)

    def test_a_declined_credential_still_finishes_and_names_the_file_to_edit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine, config = self._machine(tmp)
            result = run_menu(tmp, "5\n" + self.PRODUCTION_ANSWERS + "3\nq\n",
                              args=("--overwrite-config",), engine=engine, config=config)
            self.assertEqual(0, result.returncode, result.stdout)
            self.assertIn("leaving the production destination commented out", result.stdout)
            self.assertIn(f'uncomment the "production" destination in {config}', result.stdout)
            written = config.read_text(encoding="utf-8")
            self.assertIn('#     host: "studio.example"', written)   # the answers kept, as a comment
            data = yaml.safe_load(written)
            self.assertEqual(["dev-site"], [d["name"] for d in data["upload"]])  # nothing uncommented
            for line in written.splitlines():
                if not line.strip().startswith("#"):
                    self.assertNotIn("password", line)

    def test_a_password_is_refused_for_a_configuration_inside_a_git_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine, config = self._machine(tmp, config_dir="a-checkout")
            (Path(tmp) / "a-checkout" / ".git").mkdir()
            result = run_menu(tmp, "5\n" + self.PRODUCTION_ANSWERS + f"2\n{self.PASSWORD}\n{self.PASSWORD}\nq\n",
                              args=("--overwrite-config",), engine=engine, config=config)
            self.assertEqual(0, result.returncode, result.stdout)
            self.assertIn("refusing to put a password there", result.stdout)
            self.assertNotIn(self.PASSWORD, result.stdout)
            self.assertNotIn(self.PASSWORD, config.read_text(encoding="utf-8"))

    def test_a_dry_run_never_even_asks_for_a_password(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine, config = self._machine(tmp)
            before = config.read_text(encoding="utf-8")
            result = run_menu(tmp, "5\n" + self.PRODUCTION_ANSWERS + "2\nq\n",
                              args=("--dry-run", "--overwrite-config"), engine=engine, config=config)
            self.assertEqual(0, result.returncode, result.stdout)
            self.assertIn("[dry-run] would ask for that password here", result.stdout)
            self.assertEqual(before, config.read_text(encoding="utf-8"))

    def test_no_secret_ever_reaches_a_systemd_unit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine, config = self._machine(tmp)
            units = Path(tmp) / "units"
            run_menu(tmp, "5\n" + self.PRODUCTION_ANSWERS + f"2\n{self.PASSWORD}\n{self.PASSWORD}\n3\n"
                     "\n\n\n\n\nq\n", args=("--overwrite-config",), engine=engine, config=config, units=units)
            for unit in sorted(units.glob("*")):
                self.assertNotIn(self.PASSWORD, unit.read_text(encoding="utf-8"), str(unit))


class MenuNeverClobbersAConfigTests(unittest.TestCase):
    """The one rule this script must never break, reached through the menu
    this time: a configuration that differs from the answers given is
    reported and left exactly as it is, and only --overwrite-config replaces
    it -- so a password typed at the menu's prompt cannot even reach a file
    that was not meant to be replaced."""

    def test_a_differing_config_is_left_alone_when_the_menu_was_not_given_overwrite_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = fake_engine_root(tmp)
            dev_site = Path(tmp) / "www" / "dev"
            dev_site.mkdir(parents=True)
            workdir = Path(tmp) / "wd"
            workdir.mkdir()
            config = a_written_config(tmp, dev_site=str(dev_site), storage=tmp, workdir=str(workdir))
            before = config.read_text(encoding="utf-8")
            password = "never-lands-anywhere"
            result = run_menu(tmp, f"5\ny\n\nstudio.example\n\nbuilder\n\n2\n{password}\n{password}\nq\n",
                              args=(), engine=engine, config=config)
            self.assertEqual(0, result.returncode, result.stdout)
            self.assertEqual(before, config.read_text(encoding="utf-8"))
            self.assertIn("differs from what these answers would produce", result.stdout)
            self.assertIn("--overwrite-config", result.stdout)
            self.assertNotIn(password, result.stdout)
            self.assertNotIn(password, config.read_text(encoding="utf-8"))


class MenuKeyFileCredentialTests(unittest.TestCase):
    def test_an_ssh_key_answer_puts_no_secret_in_the_configuration_at_all(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = fake_engine_root(tmp)
            dev_site = Path(tmp) / "www" / "dev"
            dev_site.mkdir(parents=True)
            workdir = Path(tmp) / "wd"
            workdir.mkdir()
            config = a_written_config(tmp, dev_site=str(dev_site), storage=tmp, workdir=str(workdir))
            key = Path(tmp) / "upload-key"
            result = run_menu(tmp, f"5\ny\n\nstudio.example\n\nbuilder\n\n1\n{key}\nq\n",
                              args=("--overwrite-config",), engine=engine, config=config)
            self.assertEqual(0, result.returncode, result.stdout)
            written = config.read_text(encoding="utf-8")
            self.assertIn(f'key_path: "{key}"', written)
            self.assertNotIn("password:", written)
            data = yaml.safe_load(written)
            self.assertEqual(["dev-site", "production"], [d["name"] for d in data["upload"]])
            # the key itself is the operator's to put there, and is named as such
            self.assertIn(f"put the upload private key at {key}", result.stdout)


class MenuMonitoringConsoleTests(unittest.TestCase):
    """The optional half, driven from the menu: a private, separate
    repository's checkout, its own loopback-only unit, and an nginx site in
    front of it that is authenticated or nothing at all. Every refusal that
    half has still fires from here -- the menu asks the questions, the same
    function makes the decisions."""

    def _machine(self, tmp: str):
        engine = fake_engine_root(tmp)
        dev_site = Path(tmp) / "www" / "dev"
        dev_site.mkdir(parents=True)
        workdir = Path(tmp) / "wd"
        workdir.mkdir()
        config = a_written_config(tmp, dev_site=str(dev_site), storage=tmp, workdir=str(workdir))
        app = Path(tmp) / "forge"
        (app / "forge").mkdir(parents=True)
        (app / "forge" / "app.py").write_text("# stub\n", encoding="utf-8")
        (app / "packaging").mkdir(parents=True)
        (app / "packaging" / "synos-forge.service").write_text(
            "[Unit]\n[Service]\nUser=x\nGroup=x\nWorkingDirectory=/opt/x\n"
            "Environment=SYNOS_FORGE_CONFIG=/etc/x.toml\n"
            "ExecStart=/opt/x/.venv/bin/uvicorn forge.app:app --port 8420\n"
            "ReadWritePaths=/var/lib/x\n", encoding="utf-8")
        return engine, config, app

    def _run(self, tmp: str, keystrokes: str, engine: Path, config: Path) -> subprocess.CompletedProcess:
        env = menu_environment(tmp, engine=engine, config=config)
        fake_nginx(Path(tmp) / "bin")   # after the closed PATH is built: nginx is omitted from it
        return subprocess.run([BASH, str(SCRIPT), "--dry-run"], input=keystrokes, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              env=env, cwd=str(ROOT), check=False, timeout=180)

    def test_the_console_goes_behind_an_authenticated_loopback_site(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine, config, app = self._machine(tmp)
            # checkout path, no repository, port, nginx port, no allowed range,
            # no existing htpasswd file, the generated credential's user name
            result = self._run(tmp, f"6\n{app}\n\n\n\n\n\n\nq\n", engine, config)
            self.assertEqual(0, result.returncode, result.stdout)
            self.assertIn("listen 127.0.0.1:8421;", result.stdout)
            self.assertIn("auth_basic ", result.stdout)
            self.assertIn("proxy_pass http://127.0.0.1:8420/;", result.stdout)
            self.assertIn("would generate a random password", result.stdout)
            self.assertIn("monitoring nginx site: would write", result.stdout)
            self.assertIn("[dry-run] would validate with 'nginx -t' and reload nginx only on a pass",
                          result.stdout)

    def test_a_certificate_that_does_not_exist_is_still_refused_with_exit_8(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine, config, app = self._machine(tmp)
            missing_cert = Path(tmp) / "nowhere.pem"
            result = self._run(tmp, f"6\n{app}\n\n\n\n10.0.0.0/8\n{missing_cert}\n{tmp}/nowhere.key\n\n\nq\n",
                               engine, config)
            self.assertEqual(8, result.returncode, result.stdout)
            self.assertIn("does not exist", result.stdout)

    def test_declining_plain_http_beyond_loopback_keeps_it_on_loopback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine, config, app = self._machine(tmp)
            # an allowed range, no certificate, and "no" to serving it in the clear
            result = self._run(tmp, f"6\n{app}\n\n\n\n10.0.0.0/8\n\nn\n\n\nq\n", engine, config)
            self.assertEqual(0, result.returncode, result.stdout)
            self.assertIn("keeping it loopback-only", result.stdout)
            self.assertIn("listen 127.0.0.1:8421;", result.stdout)
            self.assertNotIn("allow 10.0.0.0/8;", result.stdout)


if __name__ == "__main__":
    unittest.main()
