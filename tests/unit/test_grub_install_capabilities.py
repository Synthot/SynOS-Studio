"""grub-install capability handling: the installer must not hard-code one
GRUB generation's --*-extra-removable shape.

Real bug, from a real install attempt: Debian trixie's grub-install
(2.12-9+deb13u2) has no --no-extra-removable option at all -- it offers
--force-extra-removable instead, and its default already does not install
the EFI/BOOT removable-media fallback. The installer used to plan
--no-extra-removable unconditionally for every UEFI target, and
bootloader.py's own _verify_grub_install_options() correctly refused to run
a command the target does not support:

    ERROR: Target grub-install does not support planned option(s):
    --no-extra-removable

No Debian image could be installed at all.

Fixtures under tests/fixtures/grub_install_help/ are the real `grub-install
--help` text, captured from real containers (not written by hand):
  - debian-trixie-2.12-9+deb13u2.txt   -- no --no-extra-removable
  - ubuntu-noble-2.12-1ubuntu7.3.txt   -- has --no-extra-removable
  - ubuntu-devel-2.14-2ubuntu3.txt     -- has --no-extra-removable

No test here invokes a real `grub-install`, `dracut`, `chroot` or
`efibootmgr`; every command InstallBootloaderStep would run is intercepted by
FakeCommandRunner. These tests run on any machine, offline, without root.
"""
from __future__ import annotations

import dataclasses
import hashlib
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
FIXTURES = ROOT / "tests/fixtures/grub_install_help"


def _load_installer_core_module(name: str):
    """Import installer_core.<name> without running installer_core/__init__.py
    (which wires in the full executor, including GTK/NetworkManager-facing
    modules that have no business loading for a pure logic unit test)."""
    if str(SRC) not in sys.path:
        # installer_core.validation reaches for top-level "keyboard_layouts"
        # and "languages" modules that live beside installer_core/, not
        # inside it.
        sys.path.insert(0, str(SRC))
    if "installer_core" not in sys.modules:
        package = types.ModuleType("installer_core")
        package.__path__ = [str(SRC / "installer_core")]
        sys.modules["installer_core"] = package
    return importlib.import_module(f"installer_core.{name}")


boot_commands = _load_installer_core_module("boot_commands")
bootloader = _load_installer_core_module("bootloader")
model = _load_installer_core_module("model")
command = _load_installer_core_module("command")
steps = _load_installer_core_module("steps")
storage_graph_planning = _load_installer_core_module("storage_graph_planning")
storage_inventory = _load_installer_core_module("storage_inventory")
storage_planning = _load_installer_core_module("storage_planning")
validation = _load_installer_core_module("validation")

Architecture = model.Architecture
Firmware = model.Firmware
SecureBoot = model.SecureBoot
InstallMode = model.InstallMode
Filesystem = model.Filesystem
AuthenticationMode = model.AuthenticationMode
MokPasswordPolicy = model.MokPasswordPolicy
InstallPlan = model.InstallPlan
SourceSpec = model.SourceSpec
StorageSpec = model.StorageSpec
DiskIdentity = model.DiskIdentity
PlatformSpec = model.PlatformSpec
IdentitySpec = model.IdentitySpec
AccessSpec = model.AccessSpec
RegionalSpec = model.RegionalSpec
KeyboardSpec = model.KeyboardSpec
SoftwareSpec = model.SoftwareSpec
BootSpec = model.BootSpec
SCHEMA_VERSION = model.SCHEMA_VERSION

CommandError = command.CommandError
InstallContext = steps.InstallContext
DiskTopologyBinding = storage_inventory.DiskTopologyBinding
build_erase_disk_storage_graph = (
    storage_graph_planning.build_erase_disk_storage_graph
)
validate_plan = validation.validate_plan

InstallBootloaderStep = bootloader.InstallBootloaderStep
_adapt_for_grub_capabilities = bootloader._adapt_for_grub_capabilities
_verify_grub_install_options = bootloader._verify_grub_install_options
GuidedCoexistenceExecutionPlan = storage_planning.GuidedCoexistenceExecutionPlan

sys.path.insert(0, str(SRC))
import languages  # noqa: E402  (top-level module beside installer_core/)

DEFAULT_LOCALE = languages.DEFAULT_LOCALE
DEFAULT_TIMEZONE = languages.DEFAULT_TIMEZONE
DEFAULT_KEYBOARD = languages.DEFAULT_KEYBOARD


def _read_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


DEBIAN_TRIXIE_HELP = _read_fixture("debian-trixie-2.12-9+deb13u2.txt")
UBUNTU_NOBLE_HELP = _read_fixture("ubuntu-noble-2.12-1ubuntu7.3.txt")
UBUNTU_DEVEL_HELP = _read_fixture("ubuntu-devel-2.14-2ubuntu3.txt")


# --------------------------------------------------------------- fixtures


def _build_erase_disk_plan(
    *,
    architecture=Architecture.AMD64,
    firmware=Firmware.UEFI,
    secure_boot=SecureBoot.DISABLED,
    disk_path: str = "/dev/vda",
) -> InstallPlan:
    """A real, fully valid InstallPlan for the release-one erase-disk path.

    Built the same way guided_test_plan.py builds its own test plans: no
    shortcuts around validate_plan(), because build_boot_commands() itself
    calls it, and a plan validate_plan() would reject is not a plan a real
    install would ever reach this code with.
    """

    disk = DiskIdentity(
        path=disk_path,
        stable_id="test-disk-0",
        expected_size_bytes=64 * 1024**3,
    )
    storage = StorageSpec(
        mode=InstallMode.ERASE_DISK,
        disk=disk,
        filesystem=Filesystem.EXT4,
        esp_size_mib=1024,
        swap_size_mib=2048,
        graph=None,
    )
    is_bios = firmware is Firmware.BIOS
    draft = InstallPlan(
        schema_version=SCHEMA_VERSION,
        source=SourceSpec(),
        storage=storage,
        platform=PlatformSpec(
            architecture=architecture,
            firmware=firmware,
            secure_boot=secure_boot,
        ),
        identity=IdentitySpec(
            hostname="synos-test",
            username="synostest",
            full_name="SynOS Test",
            authentication=AuthenticationMode.PASSWORD,
            password_hash="$6$" + "a" * 86,
        ),
        access=AccessSpec(),
        regional=RegionalSpec(
            locale=DEFAULT_LOCALE,
            timezone=DEFAULT_TIMEZONE,
            keyboard=KeyboardSpec(DEFAULT_KEYBOARD),
        ),
        software=SoftwareSpec(),
        boot=BootSpec(
            install_fallback_path=is_bios,
            mok_password_policy=(
                MokPasswordPolicy.SYNOS_DEFAULT
                if secure_boot is SecureBoot.ENABLED
                else MokPasswordPolicy.NOT_APPLICABLE
            ),
        ),
    )
    digest = hashlib.sha256(disk_path.encode()).hexdigest()
    graph = build_erase_disk_storage_graph(
        draft,
        DiskTopologyBinding(
            stable_id=disk.stable_id,
            expected_size_bytes=disk.expected_size_bytes,
            topology_digest=digest,
        ),
        digest,
    )
    plan = dataclasses.replace(
        draft, storage=dataclasses.replace(storage, graph=graph)
    )
    validate_plan(plan)
    return plan


@dataclass
class FakeCommandRunner:
    """Same shape as tests/unit/test_secure_boot_dkms.py's FakeCommandRunner:
    an ordered list of (predicate, CompletedProcess) rules; the first
    matching rule answers each call."""

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


def _prepare_target(target: Path) -> None:
    (target / "usr/sbin").mkdir(parents=True)
    (target / "usr/sbin/grub-install").touch()
    (target / "usr/sbin/update-grub").touch()
    (target / "usr/bin").mkdir(parents=True)
    (target / "usr/bin/dracut").touch()
    (target / "usr/bin/lsinitrd").touch()
    (target / "usr/lib/grub/i386-pc").mkdir(parents=True)
    (target / "usr/lib/grub/i386-pc/modinfo.sh").touch()
    (target / "usr/lib/grub/x86_64-efi").mkdir(parents=True)
    (target / "usr/lib/grub/x86_64-efi/modinfo.sh").touch()
    (target / "boot/efi").mkdir(parents=True)


def _make_context(plan: InstallPlan, target: Path) -> InstallContext:
    logs: list[str] = []
    context = InstallContext(plan=plan, log=logs.append)
    context.values["target"] = target
    context.values["target_efi_mounted"] = True
    context.logs = logs  # type: ignore[attr-defined]
    return context


PARTUUID = "11111111-1111-1111-1111-111111111111"
ESP_DEVICE = "/dev/vda2"  # amd64 erase-disk layout: 1=bios-boot, 2=efi-system


def _efibootmgr_verbose_text(loader: str) -> str:
    return (
        "BootCurrent: 0001\n"
        "Timeout: 1 seconds\n"
        "BootOrder: 0001,0000\n"
        "Boot0000* Windows Boot Manager\tHD(1,GPT,22222222-2222-2222-2222-222222222222,0x800,0x100000)/File(\\EFI\\Microsoft\\Boot\\bootmgfw.efi)\n"
        f"Boot0001* SynOS\tHD(2,GPT,{PARTUUID},0x800,0x100000)/File({loader})\n"
    )


def _wire_runner_for_erase_disk_uefi(
    runner: FakeCommandRunner,
    plan: InstallPlan,
    target: Path,
    help_text: str,
) -> None:
    disk = plan.storage.disk.path
    loader = boot_commands.guided_loader_path(plan)
    runner.when("chroot", str(target), "grub-install", "--help", stdout=help_text)
    runner.when(
        "chroot", str(target), "dracut", "--force", "--no-hostonly",
        "--no-hostonly-cmdline", "--omit",
        "dmsquash-live dmsquash-live-autooverlay livenet synos-live-layers",
        "--regenerate-all",
    )
    runner.when("chroot", str(target), "grub-install", "--target=i386-pc")
    runner.when("chroot", str(target), "grub-install", "--target=x86_64-efi")
    runner.when("chroot", str(target), "update-grub")
    runner.when("efibootmgr", "--create", "--disk", disk)
    runner.when("blkid", "-s", "PARTUUID", "-o", "value", ESP_DEVICE, stdout=f"{PARTUUID}\n")
    runner.when(
        "efibootmgr", "--verbose",
        stdout=_efibootmgr_verbose_text(loader),
    )
    runner.when("efibootmgr", "--bootorder")


def _execute_erase_disk_uefi(help_text: str) -> tuple[InstallContext, FakeCommandRunner, Path]:
    plan = _build_erase_disk_plan()
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp)
        _prepare_target(target)
        runner = FakeCommandRunner()
        _wire_runner_for_erase_disk_uefi(runner, plan, target, help_text)
        context = _make_context(plan, target)
        context.values["partition_devices"] = {"efi-system": ESP_DEVICE}
        step = InstallBootloaderStep(runner=runner)
        step.execute(context)  # must not raise
        return context, runner, target


# ----------------------------------------------------------- planning tests


class BootCommandsStillPlanOptimisticallyTests(unittest.TestCase):
    """boot_commands.py has no target to probe; it keeps declaring
    --no-extra-removable for every UEFI target. The capability decision is
    made later, by the executor, in bootloader.py."""

    def test_uefi_erase_disk_plans_the_flag_and_no_fallback_file(self):
        plan = _build_erase_disk_plan(firmware=Firmware.UEFI)
        commands = boot_commands.build_boot_commands(plan, "/target")
        efi_install = commands.installs[-1]
        self.assertIn("--no-extra-removable", efi_install)
        self.assertEqual("", commands.efi_fallback)
        self.assertEqual(
            boot_commands.guided_loader_path(plan), commands.loader_path
        )
        self.assertTrue(commands.nvram_create)

    def test_bios_erase_disk_never_plans_the_flag_and_keeps_the_fallback(self):
        plan = _build_erase_disk_plan(
            firmware=Firmware.BIOS, secure_boot=SecureBoot.NOT_APPLICABLE
        )
        commands = boot_commands.build_boot_commands(plan, "/target")
        for install in commands.installs:
            self.assertNotIn("--no-extra-removable", install)
        self.assertEqual("EFI/BOOT/BOOTX64.EFI", commands.efi_fallback)
        self.assertEqual("", commands.loader_path)
        self.assertFalse(commands.nvram_create)


# --------------------------------------------------------- capability tests


class AdaptForGrubCapabilitiesTests(unittest.TestCase):
    """_adapt_for_grub_capabilities is the one, narrow, documented place
    that drops --no-extra-removable -- and only that option."""

    def _efi_install_for(self, help_text: str) -> tuple[str, ...]:
        plan = _build_erase_disk_plan()
        commands = boot_commands.build_boot_commands(plan, "/target")
        adapted = _adapt_for_grub_capabilities(commands.installs, help_text)
        return adapted[-1]

    def test_debian_trixie_drops_the_unsupported_flag(self):
        efi_install = self._efi_install_for(DEBIAN_TRIXIE_HELP)
        self.assertNotIn("--no-extra-removable", efi_install)
        self.assertEqual(
            (
                "chroot", "/target", "grub-install", "--target=x86_64-efi",
                "--efi-directory=/boot/efi", "--bootloader-id=SynOS",
                "--recheck", "--no-nvram",
            ),
            efi_install,
        )

    def test_ubuntu_noble_keeps_the_supported_flag(self):
        efi_install = self._efi_install_for(UBUNTU_NOBLE_HELP)
        self.assertEqual(
            (
                "chroot", "/target", "grub-install", "--target=x86_64-efi",
                "--efi-directory=/boot/efi", "--bootloader-id=SynOS",
                "--recheck", "--no-nvram", "--no-extra-removable",
            ),
            efi_install,
        )

    def test_ubuntu_devel_2_14_keeps_the_supported_flag(self):
        efi_install = self._efi_install_for(UBUNTU_DEVEL_HELP)
        self.assertEqual(
            (
                "chroot", "/target", "grub-install", "--target=x86_64-efi",
                "--efi-directory=/boot/efi", "--bootloader-id=SynOS",
                "--recheck", "--no-nvram", "--no-extra-removable",
            ),
            efi_install,
        )

    def test_only_the_one_known_option_is_ever_dropped(self):
        """A grub-install missing something else must still fail loudly:
        this function is not a general silent filter."""
        plan = _build_erase_disk_plan()
        commands = boot_commands.build_boot_commands(plan, "/target")
        stripped_help = DEBIAN_TRIXIE_HELP.replace("--no-nvram", "--gone")
        adapted = _adapt_for_grub_capabilities(commands.installs, stripped_help)
        with self.assertRaises(RuntimeError) as caught:
            _verify_grub_install_options(stripped_help, adapted)
        self.assertIn("--no-nvram", str(caught.exception))
        # --no-extra-removable was correctly dropped and must not also be
        # reported as unsupported.
        self.assertNotIn("--no-extra-removable", str(caught.exception))


class VerifyGrubInstallOptionsStaysStrictTests(unittest.TestCase):
    def test_passes_once_the_unsupported_flag_is_adapted_away(self):
        plan = _build_erase_disk_plan()
        commands = boot_commands.build_boot_commands(plan, "/target")
        adapted = _adapt_for_grub_capabilities(commands.installs, DEBIAN_TRIXIE_HELP)
        _verify_grub_install_options(DEBIAN_TRIXIE_HELP, adapted)  # must not raise

    def test_still_rejects_a_genuinely_unsupported_option(self):
        plan = _build_erase_disk_plan(secure_boot=SecureBoot.ENABLED)
        commands = boot_commands.build_boot_commands(plan, "/target")
        help_without_secure_boot = DEBIAN_TRIXIE_HELP.replace(
            "--uefi-secure-boot", "--something-else"
        )
        adapted = _adapt_for_grub_capabilities(
            commands.installs, help_without_secure_boot
        )
        with self.assertRaises(RuntimeError) as caught:
            _verify_grub_install_options(help_without_secure_boot, adapted)
        self.assertIn("--uefi-secure-boot", str(caught.exception))


# ------------------------------------------------------- end-to-end / regression


class DebianShapedTargetNoLongerRaisesTests(unittest.TestCase):
    """The reported bug, reproduced and fixed: a Debian trixie-shaped target
    (grub-install with no --no-extra-removable) used to raise
    "Target grub-install does not support planned option(s):
    --no-extra-removable" out of InstallBootloaderStep.execute(). It must not
    any more, and the actual grub-install invoked must not carry the flag."""

    def test_debian_trixie_target_installs_without_the_flag(self):
        context, runner, target = _execute_erase_disk_uefi(DEBIAN_TRIXIE_HELP)
        efi_install = next(
            call for call in runner.calls
            if call[:4] == ("chroot", str(target), "grub-install", "--target=x86_64-efi")
        )
        self.assertNotIn("--no-extra-removable", efi_install)
        commands = context.values["boot_command_plan"]
        self.assertEqual("", commands.efi_fallback)
        self.assertEqual(
            boot_commands.guided_loader_path(context.plan), commands.loader_path
        )

    def test_ubuntu_noble_target_installs_with_the_flag(self):
        context, runner, target = _execute_erase_disk_uefi(UBUNTU_NOBLE_HELP)
        efi_install = next(
            call for call in runner.calls
            if call[:4] == ("chroot", str(target), "grub-install", "--target=x86_64-efi")
        )
        self.assertIn("--no-extra-removable", efi_install)

    def test_ubuntu_devel_2_14_target_installs_with_the_flag(self):
        context, runner, target = _execute_erase_disk_uefi(UBUNTU_DEVEL_HELP)
        efi_install = next(
            call for call in runner.calls
            if call[:4] == ("chroot", str(target), "grub-install", "--target=x86_64-efi")
        )
        self.assertIn("--no-extra-removable", efi_install)


class VendorOnlyBootDropsUnsupportedFlagTests(unittest.TestCase):
    """Guided coexistence and manual mode freeze their vendor-only boot
    command plan during preflight, before the target rootfs even exists
    (storage_planning.py's build_guided_coexistence_boot_commands /
    build_manual_boot_commands have no grub-install to probe either) --
    so they still bake --no-extra-removable in optimistically, exactly like
    build_boot_commands does. InstallBootloaderStep.execute() must adapt
    this frozen plan the same way it adapts the erase-disk one."""

    def _fake_plan(self):
        return types.SimpleNamespace(
            storage=types.SimpleNamespace(
                mode=InstallMode.GUIDED_COEXISTENCE,
                filesystem=Filesystem.EXT4,
                disk=types.SimpleNamespace(path="/dev/vda"),
            ),
            platform=types.SimpleNamespace(
                architecture=Architecture.AMD64,
                firmware=Firmware.UEFI,
                secure_boot=SecureBoot.DISABLED,
            ),
        )

    def test_guided_coexistence_debian_target_drops_flag(self):
        plan = self._fake_plan()

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            _prepare_target(target)
            boot_plan = boot_commands.build_guided_coexistence_boot_commands(
                plan, str(target), disk_path="/dev/vda", esp_partition_number=2,
            )
            # The frozen plan itself still declares the flag optimistically
            # -- unchanged, and exactly what storage_planning.py's own
            # parity checks (_verify_guided_parity / _verify_manual_parity)
            # require.
            self.assertIn("--no-extra-removable", boot_plan.install)
            runner = FakeCommandRunner()
            runner.when(
                "chroot", str(target), "grub-install", "--help",
                stdout=DEBIAN_TRIXIE_HELP,
            )
            runner.when(
                "chroot", str(target), "dracut", "--force", "--no-hostonly",
                "--no-hostonly-cmdline", "--omit",
                "dmsquash-live dmsquash-live-autooverlay livenet synos-live-layers",
                "--regenerate-all",
            )
            runner.when("chroot", str(target), "grub-install", "--target=x86_64-efi")
            runner.when("chroot", str(target), "update-grub")
            runner.when("efibootmgr", "--create", "--disk", "/dev/vda")
            runner.when(
                "blkid", "-s", "PARTUUID", "-o", "value", ESP_DEVICE,
                stdout=f"{PARTUUID}\n",
            )
            runner.when(
                "efibootmgr", "--verbose",
                stdout=_efibootmgr_verbose_text(boot_plan.loader_path),
            )
            runner.when("efibootmgr", "--bootorder")

            context = InstallContext(plan=plan, log=lambda *_: None)
            context.values["target"] = target
            context.values["target_efi_mounted"] = True
            context.values["partition_devices"] = {"efi-system": ESP_DEVICE}
            context.values["guided_storage_execution_plan"] = (
                GuidedCoexistenceExecutionPlan(
                    commands=None,
                    boot_commands=boot_plan,
                    write_set=None,
                    esp_partition_number=2,
                    reuses_esp=False,
                )
            )

            step = InstallBootloaderStep(runner=runner)
            step.execute(context)  # must not raise

            install_call = next(
                call for call in runner.calls
                if call[:4]
                == ("chroot", str(target), "grub-install", "--target=x86_64-efi")
            )
            self.assertNotIn("--no-extra-removable", install_call)
            adapted = context.values["boot_command_plan"]
            self.assertNotIn("--no-extra-removable", adapted.install)
            # The loader path guarantee (explicit NVRAM entry, never a
            # shared EFI/BOOT fallback) is unaffected by dropping the flag.
            self.assertEqual(boot_plan.loader_path, adapted.loader_path)


if __name__ == "__main__":
    unittest.main()
