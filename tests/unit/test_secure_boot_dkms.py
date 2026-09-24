"""VerifyDkmsSignaturesStep: an optional DKMS driver must never block an
installation, but a module that *is* built and enrolled under Secure Boot
still has to be genuinely signed by the installer's own MOK.

No test here invokes a real `dkms`, `chroot` or `openssl` binary — every
command a step would run is intercepted by FakeCommandRunner and answered
with a canned subprocess.CompletedProcess, so these tests run on any
machine, offline, without root.
"""
from __future__ import annotations

import importlib
import sys
import tempfile
import types
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from subprocess import CompletedProcess
from typing import Callable, Sequence

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "packages/synos-installer-beta/upstream/src"


def _load_installer_core_module(name: str):
    """Import installer_core.<name> without running installer_core/__init__.py
    (which wires in the full executor, including GTK/NetworkManager-facing
    modules that have no business loading for a pure logic unit test)."""
    if str(SRC) not in sys.path:
        # installer_core.validation reaches for a top-level "keyboard_layouts"
        # module that lives beside installer_core/, not inside it.
        sys.path.insert(0, str(SRC))
    if "installer_core" not in sys.modules:
        package = types.ModuleType("installer_core")
        package.__path__ = [str(SRC / "installer_core")]
        sys.modules["installer_core"] = package
    return importlib.import_module(f"installer_core.{name}")


secure_boot = _load_installer_core_module("secure_boot")
steps = _load_installer_core_module("steps")
command = _load_installer_core_module("command")

InstallContext = steps.InstallContext
StepStatus = steps.StepStatus
StepRunner = steps.StepRunner
StepWarning = steps.StepWarning
VerifyDkmsSignaturesStep = secure_boot.VerifyDkmsSignaturesStep
CommandError = command.CommandError


@dataclass
class FakeCommandRunner:
    """A CommandRunner stand-in driven by an ordered list of
    (predicate, CompletedProcess) rules; the first matching rule answers each
    call. Honors `check` exactly like the real CommandRunner: a nonzero
    result raises CommandError unless the caller passed check=False."""

    rules: list[tuple[Callable[[tuple[str, ...]], bool], CompletedProcess]] = field(
        default_factory=list
    )
    calls: list[tuple[str, ...]] = field(default_factory=list)

    def when(self, *prefix: str, returncode: int = 0, stdout: str = "", stderr: str = ""):
        def matches(argv: tuple[str, ...]) -> bool:
            return argv[: len(prefix)] == tuple(prefix)

        self.rules.append((matches, CompletedProcess(prefix, returncode, stdout, stderr)))

    def require_commands(self, commands: Sequence[str]) -> None:
        return None

    def run(
        self,
        command_: Sequence[str],
        *,
        input_text: str | None = None,
        timeout: int | None = None,
        check: bool = True,
        log_output: bool = True,
        environment: dict[str, str] | None = None,
    ) -> CompletedProcess:
        argv = tuple(str(part) for part in command_)
        self.calls.append(argv)
        for matches, result in self.rules:
            if matches(argv):
                if check and result.returncode != 0:
                    raise CommandError(f"Command exited with {result.returncode}: {argv}")
                return CompletedProcess(argv, result.returncode, result.stdout, result.stderr)
        raise AssertionError(f"FakeCommandRunner has no rule for: {argv}")


def _make_context(runner: FakeCommandRunner, target: Path, *, secure_boot_enabled=False):
    plan = types.SimpleNamespace(
        platform=types.SimpleNamespace(
            secure_boot=(
                secure_boot.SecureBoot.ENABLED
                if secure_boot_enabled
                else secure_boot.SecureBoot.DISABLED
            )
        )
    )
    logs: list[str] = []
    context = InstallContext(plan=plan, log=logs.append)
    context.values["target"] = target
    context.logs = logs  # type: ignore[attr-defined]
    return context


class DkmsAutoinstallUsesTargetKernelTests(unittest.TestCase):
    """The chroot bug: `dkms autoinstall` with no -k builds against the live
    session's `uname -r`, not the kernel just installed in /target."""

    def test_dkms_not_installed_is_a_silent_no_op(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            runner = FakeCommandRunner()
            step = VerifyDkmsSignaturesStep(runner=runner)
            context = _make_context(runner, target)
            step.execute(context)
            self.assertEqual([], context.values["dkms_modules"])
            self.assertEqual([], runner.calls)

    def test_no_installed_kernel_is_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            (target / "usr/sbin").mkdir(parents=True)
            (target / "usr/sbin/dkms").touch()
            runner = FakeCommandRunner()
            step = VerifyDkmsSignaturesStep(runner=runner)
            context = _make_context(runner, target)
            step.execute(context)  # must not raise
            self.assertEqual([], context.values["dkms_modules"])
            self.assertTrue(
                any("no installed kernel" in line for line in context.logs)
            )

    def test_autoinstall_is_invoked_with_the_target_kernel_version(self):
        kernel = "6.12.9-amd64"
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            (target / "usr/sbin").mkdir(parents=True)
            (target / "usr/sbin/dkms").touch()
            (target / "lib/modules" / kernel).mkdir(parents=True)

            runner = SequencedStatusRunner(target, kernel)

            step = VerifyDkmsSignaturesStep(runner=runner)
            context = _make_context(runner, target)
            step.execute(context)  # must not raise: the module reaches "installed"

            self.assertIn(
                ("chroot", str(target), "dkms", "autoinstall", "-k", kernel),
                runner.calls,
            )
            # No call anywhere asks dkms to build for the live session's own
            # kernel implicitly (a bare "autoinstall" with no "-k").
            self.assertNotIn(
                ("chroot", str(target), "dkms", "autoinstall"), runner.calls
            )


class OptionalModuleFailureIsNonFatalTests(unittest.TestCase):
    """Requirement: an optional driver must never block an installation."""

    def _target_with_kernel(self, tmp: str, kernel: str) -> Path:
        target = Path(tmp)
        (target / "usr/sbin").mkdir(parents=True)
        (target / "usr/sbin/dkms").touch()
        (target / "lib/modules" / kernel).mkdir(parents=True)
        return target

    def test_missing_headers_is_reported_and_continues(self):
        kernel = "6.12.9-amd64"
        with tempfile.TemporaryDirectory() as tmp:
            target = self._target_with_kernel(tmp, kernel)
            runner = FakeCommandRunner()
            # headers check fails: no /lib/modules/<kernel>/build
            runner.when(
                "chroot", str(target), "test", "-e", f"/lib/modules/{kernel}/build",
                returncode=1,
            )
            runner.when(
                "chroot", str(target), "dkms", "status", "-k", kernel,
                stdout=f"hid-xpadneo/0.11, {kernel}, x86_64: added\n",
            )

            step = VerifyDkmsSignaturesStep(runner=runner)
            context = _make_context(runner, target)
            with self.assertRaises(StepWarning) as caught:
                step.execute(context)

            self.assertIn("hid-xpadneo", str(caught.exception))
            # autoinstall must never be attempted with no headers to build against
            self.assertFalse(
                any(call[3:5] == ("dkms", "autoinstall") for call in runner.calls)
            )
            self.assertTrue(
                any(
                    f"kernel headers for {kernel} are not installed; "
                    "no out-of-tree modules can be built" in line
                    for line in context.logs
                ),
                context.logs,
            )
            outcomes = context.values["dkms_modules"]
            self.assertEqual(1, len(outcomes))
            self.assertEqual("hid-xpadneo", outcomes[0]["module"])
            self.assertEqual(kernel, outcomes[0]["kernel"])
            self.assertEqual("headers-missing", outcomes[0]["state"])

    def test_module_that_fails_to_build_is_reported_with_log_tail_and_continues(self):
        kernel = "6.12.9-amd64"
        with tempfile.TemporaryDirectory() as tmp:
            target = self._target_with_kernel(tmp, kernel)
            runner = FakeCommandRunner()
            runner.when("chroot", str(target), "test", "-e", f"/lib/modules/{kernel}/build")
            # before autoinstall: registered but not yet built
            runner.when(
                "chroot", str(target), "dkms", "status", "-k", kernel,
                stdout=f"hid-xpadneo/0.11, {kernel}, x86_64: added\n",
            )
            runner.when(
                "chroot", str(target), "dkms", "autoinstall", "-k", kernel,
                returncode=11,
            )
            runner.when(
                "chroot", str(target), "sh", "-c",
                stdout="drivers/hid/hid-xpadneo.c: error: unknown field 'foo'\n"
                "make: *** [Makefile:42: hid-xpadneo.o] Error 1\n",
            )

            step = VerifyDkmsSignaturesStep(runner=runner)
            context = _make_context(runner, target)
            with self.assertRaises(StepWarning) as caught:
                step.execute(context)

            message = str(caught.exception)
            self.assertIn("hid-xpadneo", message)
            self.assertIn("0.11", message)
            self.assertIn(kernel, message)

            outcomes = context.values["dkms_modules"]
            self.assertEqual(1, len(outcomes))
            self.assertEqual("hid-xpadneo", outcomes[0]["module"])
            self.assertEqual("0.11", outcomes[0]["version"])
            self.assertEqual(kernel, outcomes[0]["kernel"])
            self.assertIn("unknown field 'foo'", outcomes[0]["detail"])

            logged = "\n".join(context.logs)
            self.assertIn("hid-xpadneo", logged)
            self.assertIn(kernel, logged)
            self.assertIn("unknown field 'foo'", logged)

    def test_successful_build_reports_nothing_and_does_not_warn(self):
        kernel = "6.12.9-amd64"
        # dkms status is asked twice (before and after autoinstall) and must
        # answer "added" the first time, "installed" the second — a plain
        # FakeCommandRunner rule can't distinguish the two calls, hence the
        # small SequencedStatusRunner subclass below.
        with tempfile.TemporaryDirectory() as tmp:
            target = self._target_with_kernel(tmp, kernel)
            runner = SequencedStatusRunner(target, kernel)
            step = VerifyDkmsSignaturesStep(runner=runner)
            context = _make_context(runner, target)
            step.execute(context)  # must not raise StepWarning
            self.assertEqual([], context.values["dkms_modules"])

    def test_runner_run_invokes_no_real_dkms_binary(self):
        """Sanity guard: FakeCommandRunner never shells out, so nothing in
        these tests can have actually invoked a real `dkms`."""
        kernel = "6.12.9-amd64"
        with tempfile.TemporaryDirectory() as tmp:
            target = self._target_with_kernel(tmp, kernel)
            runner = FakeCommandRunner()
            runner.when("chroot", str(target), "test", "-e", f"/lib/modules/{kernel}/build")
            runner.when(
                "chroot", str(target), "dkms", "status", "-k", kernel,
                stdout=f"hid-xpadneo/0.11, {kernel}, x86_64: added\n",
            )
            runner.when(
                "chroot", str(target), "dkms", "autoinstall", "-k", kernel,
                returncode=11,
            )
            runner.when("chroot", str(target), "sh", "-c", stdout="boom\n")
            step = VerifyDkmsSignaturesStep(runner=runner)
            context = _make_context(runner, target)
            with self.assertRaises(StepWarning):
                step.execute(context)
            # Every recorded call is a "chroot <target> ..." invocation; none
            # of them is a bare, unchecked exec of "dkms" on this host.
            for call in runner.calls:
                self.assertEqual("chroot", call[0])


class SequencedStatusRunner(FakeCommandRunner):
    """`dkms status -k <kernel>` answers 'added' the first time (before
    autoinstall) and 'installed' the second time (after) — the shape of a
    module that built successfully."""

    def __init__(self, target: Path, kernel: str):
        super().__init__()
        self._target = str(target)
        self._kernel = kernel
        self._status_calls = 0
        self.when("chroot", self._target, "test", "-e", f"/lib/modules/{kernel}/build")
        self.when("chroot", self._target, "dkms", "autoinstall", "-k", kernel)

    def run(self, command_, **kwargs):
        argv = tuple(str(part) for part in command_)
        if argv[:5] == ("chroot", self._target, "dkms", "status", "-k"):
            self._status_calls += 1
            self.calls.append(argv)
            state = "added" if self._status_calls == 1 else "installed"
            return CompletedProcess(
                argv, 0, f"hid-xpadneo/0.11, {self._kernel}, x86_64: {state}\n", ""
            )
        return super().run(command_, **kwargs)


class SignedModuleVerificationStaysFatalTests(unittest.TestCase):
    """The one case that must remain fatal: a module that *was* built and
    installed, but is not signed by this installation's own MOK — a real
    Secure Boot violation, not an optional driver's build failure."""

    def _prepare_target(self, tmp: str, kernel: str) -> Path:
        target = Path(tmp)
        (target / "var/lib/shim-signed/mok").mkdir(parents=True)
        (target / "var/lib/shim-signed/mok/MOK.der").write_bytes(b"certificate")
        module_dir = target / "lib/modules" / kernel / "updates/dkms"
        module_dir.mkdir(parents=True)
        (module_dir / "hid-xpadneo.ko").write_bytes(b"module")
        return target

    def test_unsigned_built_module_fails_verification(self):
        kernel = "6.12.9-amd64"
        with tempfile.TemporaryDirectory() as tmp:
            target = self._prepare_target(tmp, kernel)
            runner = FakeCommandRunner()
            runner.when(
                "chroot", str(target), "openssl", "x509", "-inform", "DER",
                "-in", "/var/lib/shim-signed/mok/MOK.der", "-serial", "-noout",
                stdout="serial=ABCDEF\n",
            )
            runner.when(
                "chroot", str(target), "modinfo", "-F", "sig_key",
                f"/lib/modules/{kernel}/updates/dkms/hid-xpadneo.ko",
                stdout="112233\n",  # does not match the certificate serial
            )
            step = VerifyDkmsSignaturesStep(runner=runner)
            context = _make_context(runner, target, secure_boot_enabled=True)
            with self.assertRaises(RuntimeError) as caught:
                step.verify(context)
            self.assertIn("not signed by the installation MOK", str(caught.exception))

    def test_correctly_signed_module_passes_verification(self):
        kernel = "6.12.9-amd64"
        with tempfile.TemporaryDirectory() as tmp:
            target = self._prepare_target(tmp, kernel)
            runner = FakeCommandRunner()
            runner.when(
                "chroot", str(target), "openssl", "x509", "-inform", "DER",
                "-in", "/var/lib/shim-signed/mok/MOK.der", "-serial", "-noout",
                stdout="serial=ABCDEF\n",
            )
            runner.when(
                "chroot", str(target), "modinfo", "-F", "sig_key",
                f"/lib/modules/{kernel}/updates/dkms/hid-xpadneo.ko",
                stdout="ABCDEF\n",
            )
            step = VerifyDkmsSignaturesStep(runner=runner)
            context = _make_context(runner, target, secure_boot_enabled=True)
            step.verify(context)  # must not raise

    def test_verification_is_skipped_when_secure_boot_is_disabled(self):
        kernel = "6.12.9-amd64"
        with tempfile.TemporaryDirectory() as tmp:
            target = self._prepare_target(tmp, kernel)
            runner = FakeCommandRunner()  # no rules at all: any call would raise
            step = VerifyDkmsSignaturesStep(runner=runner)
            context = _make_context(runner, target, secure_boot_enabled=False)
            step.verify(context)  # must not raise, and must not call the runner
            self.assertEqual([], runner.calls)


class StepRunnerIntegrationTests(unittest.TestCase):
    """A failed optional module keeps the overall installation succeeding;
    the step itself is reported as a warning, not swallowed."""

    def test_install_result_still_succeeds_with_a_warning_step(self):
        kernel = "6.12.9-amd64"
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            (target / "usr/sbin").mkdir(parents=True)
            (target / "usr/sbin/dkms").touch()
            (target / "lib/modules" / kernel).mkdir(parents=True)
            runner = FakeCommandRunner()
            runner.when(
                "chroot", str(target), "test", "-e", f"/lib/modules/{kernel}/build",
                returncode=1,
            )
            runner.when(
                "chroot", str(target), "dkms", "status", "-k", kernel,
                stdout=f"hid-xpadneo/0.11, {kernel}, x86_64: added\n",
            )
            step = VerifyDkmsSignaturesStep(runner=runner)
            plan = types.SimpleNamespace(
                platform=types.SimpleNamespace(secure_boot=secure_boot.SecureBoot.DISABLED)
            )
            logs: list[str] = []
            context = InstallContext(plan=plan, log=logs.append)
            context.values["target"] = target
            # Bypass context.validate_plan(); it is exercised elsewhere and
            # this SimpleNamespace plan is not a full InstallPlan.
            context.validate_plan = lambda: None  # type: ignore[method-assign]

            runner_step = StepRunner([step])
            result = runner_step.run(context)

            self.assertTrue(result.succeeded)
            self.assertEqual(1, len(result.results))
            self.assertEqual(StepStatus.WARNING, result.results[0].status)
            self.assertIn("hid-xpadneo", result.results[0].message)


if __name__ == "__main__":
    unittest.main()
