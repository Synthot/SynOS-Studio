from pathlib import Path
import struct
import tempfile
import unittest

from framework.errors import ConfigurationError
from framework.iso import _pe_machine, _validate_dracut_live_contract


ROOT = Path(__file__).resolve().parents[2]


class DracutLiveContractTests(unittest.TestCase):
    def test_iso_uses_one_direct_squashfs_under_liveos(self) -> None:
        build = (ROOT / "build.sh").read_text()
        self.assertIn("image/{LiveOS,isolinux,.disk}", build)
        self.assertIn("image/LiveOS/rootfs.squashfs", build)
        self.assertEqual(build.count("mksquashfs new_building_os"), 1)
        self.assertIn('-e "boot/synos-live-initrd.img"', build)
        self.assertNotIn("image/casper", build)
        self.assertNotIn("/casper/", build)

    def test_every_live_entry_uses_the_dracut_contract(self) -> None:
        build = (ROOT / "build.sh").read_text()
        common = (
            "root=live:CDLABEL=$TARGET_NAME rd.live.dir=LiveOS "
            "rd.live.squashimg=rootfs.squashfs rd.overlay "
            "rd.synos.live=1"
        )
        self.assertIn(f'LIVE_BOOT_ARGS="{common}"', build)
        self.assertIn("rd.overlay=LABEL=SYNOS-PERSIST", build)
        self.assertNotIn("SYNOS-PERSISTENCE", build)
        self.assertIn("rd.live.overlay.cowfs=ext4", build)
        self.assertIn("rd.live.check=1", build)
        self.assertEqual(build.count("-partition_offset 16"), 2)
        self.assertIn("implantisomd5 --force", build)
        self.assertNotIn("boot=casper", build)

    def test_dedicated_live_initrd_recipe_only_builds_the_image(self) -> None:
        script = (ROOT / "mods/80-dracut-live-image/install.sh").read_text()
        stack = (ROOT / "mods/stack.sh").read_text()
        self.assertIn("--no-hostonly", script)
        self.assertIn("--no-hostonly-cmdline", script)
        # The module list lives once, in mods/stack.sh's LIVE_DRACUT_MODULES,
        # so mod 80's --add and ensure_dracut_live_modules's presence check
        # can never drift apart (see test_control_dependencies.py's sibling
        # class of bug: a name hardcoded in one place and never verified
        # anywhere else).
        self.assertIn('--add "$LIVE_DRACUT_MODULES"', script)
        self.assertIn("ensure_dracut_live_modules", script)
        for module in (
            "dmsquash-live",
            "dmsquash-live-autooverlay",
            "overlayfs",
            "synos-live-layers",
        ):
            self.assertIn(module, stack)
        self.assertIn("/boot/synos-live-initrd.img", script)
        self.assertIn('judge "Build dedicated Dracut Live initrd"', script)
        for test_logic in ("dpkg-query", "lsinitrd", "command -v", "test -s", "grep"):
            self.assertNotIn(test_logic, script)
        self.assertFalse((ROOT / "mods/46-casper-patch/install.sh").exists())
        self.assertFalse((ROOT / "mods/80-initramfs-update/install.sh").exists())

    def test_initrd_is_generated_once_for_the_image_kernel(self) -> None:
        """Package scripts must never run dracut for the build host's kernel:
        updates are off for the whole package installation and the initrd is
        generated once, explicitly, before the live initrd, then switched
        back on for the installed system."""
        mods = (ROOT / "mods/install_all_mods.sh").read_text()
        live = (ROOT / "mods/80-dracut-live-image/install.sh").read_text()
        self.assertIn("update_initramfs=no", mods)
        self.assertLess(mods.index("update_initramfs=no"), mods.index("# Execute mods"))
        self.assertIn("update_initramfs=yes", live)
        self.assertIn('dracut --force "/boot/initrd.img-$kernel_version" "$kernel_version"', live)
        self.assertNotIn("update-initramfs -", live, "the diverted update-initramfs would run the verifier in the chroot")
        self.assertLess(live.index("update_initramfs=yes"), live.index("live_initrd=/boot/synos-live-initrd.img"))

    def test_update_initramfs_wrapper_skips_verification_while_updates_are_off(self) -> None:
        """A package script calling update-initramfs during the image build
        (updates off) must not trip the boot-proof verifier: nothing was
        rebuilt. With updates on, the verifier runs as before."""
        import os
        import stat
        import subprocess
        wrapper = ROOT / "packages/synos-core-system/assets/usr/libexec/synos-update-initramfs"
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            real = tmp / "real"
            real.write_text('#!/bin/sh\nprintf "real %s\\n" "$*" >> "$SYNOS_TEST_LOG"\nexit 0\n', encoding="utf-8")
            verify = tmp / "verify"
            verify.write_text('#!/bin/sh\nprintf "verify %s\\n" "$*" >> "$SYNOS_TEST_LOG"\nexit 1\n', encoding="utf-8")
            for script in (real, verify):
                script.chmod(script.stat().st_mode | stat.S_IEXEC)
            log = tmp / "log"
            conf = tmp / "update-initramfs.conf"
            env = dict(os.environ, SYNOS_UPDATE_INITRAMFS_REAL=str(real), SYNOS_MIGRATION_VERIFY=str(verify), SYNOS_UPDATE_INITRAMFS_CONF=str(conf), SYNOS_TEST_LOG=str(log))
            env.pop("DPKG_MAINTSCRIPT_PACKAGE", None)
            conf.write_text("update_initramfs=no\n", encoding="utf-8")
            off = subprocess.run(["sh", str(wrapper), "-u", "-k", "6.8.0-139-generic"], env=env, capture_output=True, text=True)
            self.assertEqual(0, off.returncode, off.stderr)
            self.assertEqual(["real -u -k 6.8.0-139-generic"], log.read_text(encoding="utf-8").splitlines())
            log.write_text("", encoding="utf-8")
            conf.write_text("update_initramfs=yes\n", encoding="utf-8")
            on = subprocess.run(["sh", str(wrapper), "-u", "-k", "6.8.0-139-generic"], env=env, capture_output=True, text=True)
            self.assertEqual(1, on.returncode, "with updates on, the verifier's verdict is the wrapper's")
            self.assertEqual(["real -u -k 6.8.0-139-generic", "verify --verify"], log.read_text(encoding="utf-8").splitlines())

    def test_update_initramfs_wrapper_computes_updates_off_before_checking_implementation(self) -> None:
        """The 'updates off during the image build' state must be known
        before a missing diverted implementation is treated as an error,
        never after: otherwise a build with updates off can still depend on
        an implementation that a base such as Debian never diverted."""
        wrapper = (ROOT / "packages/synos-core-system/assets/usr/libexec/synos-update-initramfs").read_text()
        self.assertLess(
            wrapper.index("updates_off=false"),
            wrapper.index('if [ -x "$REAL_UPDATE_INITRAMFS" ]'),
            "updates_off must be computed before the implementation is required",
        )

    def _run_wrapper(self, tmp, args, real_exists, verify_exit=0, conf_text=None,
                      maintscript=False):
        """Run the update-initramfs wrapper against fakes: real_exists picks
        whether SYNOS_UPDATE_INITRAMFS_REAL points at an executable fake or a
        path that was never created (the diversion-target-missing case), and
        the fake verifier logs its invocation and exits verify_exit. Returns
        (CompletedProcess, log lines)."""
        import os
        import stat
        import subprocess
        wrapper = ROOT / "packages/synos-core-system/assets/usr/libexec/synos-update-initramfs"
        real = tmp / "real"
        verify = tmp / "verify"
        log = tmp / "log"
        if real_exists:
            real.write_text('#!/bin/sh\nprintf "real %s\\n" "$*" >> "$SYNOS_TEST_LOG"\nexit 0\n', encoding="utf-8")
            real.chmod(real.stat().st_mode | stat.S_IEXEC)
        verify.write_text(
            '#!/bin/sh\nprintf "verify %s\\n" "$*" >> "$SYNOS_TEST_LOG"\nexit {}\n'.format(verify_exit),
            encoding="utf-8",
        )
        verify.chmod(verify.stat().st_mode | stat.S_IEXEC)
        conf = tmp / "update-initramfs.conf"
        if conf_text is not None:
            conf.write_text(conf_text, encoding="utf-8")
        env = dict(
            os.environ,
            SYNOS_UPDATE_INITRAMFS_REAL=str(real),
            SYNOS_MIGRATION_VERIFY=str(verify),
            SYNOS_UPDATE_INITRAMFS_CONF=str(conf),
            SYNOS_TEST_LOG=str(log),
        )
        if maintscript:
            env["DPKG_MAINTSCRIPT_PACKAGE"] = "some-package"
        else:
            env.pop("DPKG_MAINTSCRIPT_PACKAGE", None)
        result = subprocess.run(["sh", str(wrapper), *args], env=env, capture_output=True, text=True)
        lines = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
        return result, lines

    def test_update_initramfs_wrapper_noop_when_implementation_missing_and_updates_off(self) -> None:
        """A base such as Debian's, whose Dracut ships no update-initramfs
        compatibility command, never gets a diversion (see postinst's
        install_update_initramfs_guard). During an image build (updates
        off) that must be a silent, successful no-op, exactly like the
        implementation-present case -- never the fatal error that took the
        real build down."""
        with tempfile.TemporaryDirectory() as tmp:
            result, lines = self._run_wrapper(
                Path(tmp), ["-u", "-k", "6.8.0-139-generic"],
                real_exists=False, conf_text="update_initramfs=no\n",
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual([], lines, "neither a real implementation nor the verifier should run")

    def test_update_initramfs_wrapper_builds_with_dracut_when_implementation_missing_and_updates_on(self) -> None:
        """On an installed, pure-Dracut system with updates on, a package
        calling update-initramfs with no diverted implementation to call
        must still get its initrd built -- through the synchronous
        rebuild/verify path that already knows how to drive Dracut, not a
        hard failure."""
        with tempfile.TemporaryDirectory() as tmp:
            result, lines = self._run_wrapper(
                Path(tmp), ["-u", "-k", "6.8.0-139-generic"],
                real_exists=False, conf_text="update_initramfs=yes\n",
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(["verify --rebuild", "verify --verify"], lines)

    def test_update_initramfs_wrapper_noop_on_delete_when_implementation_missing(self) -> None:
        """Deleting a removed kernel's image is left to the base's own
        kernel hooks when there is no diverted implementation to ask;
        the wrapper must not fail or invoke the verifier for -d."""
        with tempfile.TemporaryDirectory() as tmp:
            result, lines = self._run_wrapper(
                Path(tmp), ["-d", "-k", "6.8.0-139-generic"],
                real_exists=False, conf_text="update_initramfs=yes\n",
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual([], lines)

    def test_update_initramfs_wrapper_noop_on_maintscript_trigger_when_implementation_missing(self) -> None:
        """The shared dpkg trigger invocation (DPKG_MAINTSCRIPT_PACKAGE, a
        bare -u) may run before a pending kernel postinst has produced a
        modules tree; with no diverted implementation either, it must still
        be a safe no-op rather than a failure or a premature rebuild."""
        with tempfile.TemporaryDirectory() as tmp:
            result, lines = self._run_wrapper(
                Path(tmp), ["-u"],
                real_exists=False, conf_text="update_initramfs=yes\n",
                maintscript=True,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual([], lines)

    _FAKE_DPKG_DIVERT = (
        "#!/bin/sh\n"
        "set -eu\n"
        'STATE="$FAKE_DIVERT_STATE"\n'
        'if [ "$1" = --listpackage ]; then\n'
        "    path=$2\n"
        '    line=$(grep -F "|$path|" "$STATE" 2>/dev/null || true)\n'
        '    [ -n "$line" ] || exit 1\n'
        '    printf "%s\\n" "$line" | cut -d"|" -f1\n'
        "    exit 0\n"
        "fi\n"
        'package=""; divert=""; original=""; add=false; rename=false\n'
        "while [ $# -gt 0 ]; do\n"
        "    case $1 in\n"
        "        --package) shift; package=$1 ;;\n"
        "        --add) add=true ;;\n"
        "        --rename) rename=true ;;\n"
        "        --divert) shift; divert=$1 ;;\n"
        "        *) original=$1 ;;\n"
        "    esac\n"
        "    shift\n"
        "done\n"
        'if [ "$add" = true ]; then\n'
        '    printf "%s|%s|%s\\n" "$package" "$original" "$divert" >> "$STATE"\n'
        '    if [ "$rename" = true ] && [ -e "$original" ]; then\n'
        '        mv "$original" "$divert"\n'
        "    fi\n"
        "fi\n"
    )

    def _run_postinst_configure(self, tmp):
        """Run scripts/postinst 'configure' against a fake dpkg-divert and a
        fake root, using only fakes -- no real dpkg. Returns the completed
        process; callers set up usr/sbin and usr/libexec under tmp first."""
        import os
        import stat
        import subprocess
        postinst = ROOT / "packages/synos-core-system/scripts/postinst"
        dpkg_divert = tmp / "dpkg-divert"
        dpkg_divert.write_text(self._FAKE_DPKG_DIVERT, encoding="utf-8")
        dpkg_divert.chmod(dpkg_divert.stat().st_mode | stat.S_IEXEC)
        state = tmp / "divert-state"
        if not state.exists():
            state.write_text("", encoding="utf-8")
        env = dict(
            os.environ,
            SYNOS_MIGRATION_DPKG_DIVERT=str(dpkg_divert),
            FAKE_DIVERT_STATE=str(state),
            SYNOS_MIGRATION_UPDATE_INITRAMFS=str(tmp / "usr/sbin/update-initramfs"),
            SYNOS_MIGRATION_UPDATE_INITRAMFS_DIVERT=str(tmp / "usr/sbin/update-initramfs.synos-dracut"),
            SYNOS_MIGRATION_UPDATE_INITRAMFS_WRAPPER=str(tmp / "usr/libexec/synos-update-initramfs"),
            SYNOS_MIGRATION_UPDATE_GRUB=str(tmp / "usr/sbin/update-grub"),
            SYNOS_MIGRATION_UPDATE_GRUB_DIVERT=str(tmp / "usr/sbin/update-grub.synos-grub"),
            SYNOS_MIGRATION_UPDATE_GRUB_WRAPPER=str(tmp / "usr/libexec/synos-update-grub"),
            SYNOS_MIGRATION_STATE_DIR=str(tmp / "var/lib/synos-dracut-migration"),
        )
        (tmp / "var/lib/synos-dracut-migration").mkdir(parents=True, exist_ok=True)
        return subprocess.run(["sh", str(postinst), "configure"], env=env, capture_output=True, text=True)

    def _write_executable(self, path, body="#!/bin/sh\nexit 0\n"):
        import stat
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)

    def test_postinst_update_initramfs_guard_is_idempotent_with_no_compat_shim(self) -> None:
        """On a base whose Dracut ships no update-initramfs compatibility
        command (Debian's), nothing should ever be diverted, on the first
        postinst run or on any later reconfigure/upgrade. Diverting the
        guard's own symlink onto itself on a second run would make the
        wrapper's diverted implementation check see a "real" implementation
        that is really itself, calling into it forever."""
        import os
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            self._write_executable(tmp / "usr/libexec/synos-update-initramfs")
            self._write_executable(tmp / "usr/libexec/synos-update-grub")
            self._write_executable(tmp / "usr/sbin/update-grub")  # GRUB always ships this

            first = self._run_postinst_configure(tmp)
            self.assertEqual(0, first.returncode, first.stderr)
            divert_state = (tmp / "divert-state").read_text(encoding="utf-8")
            self.assertNotIn("update-initramfs", divert_state)
            self.assertEqual(
                str(tmp / "usr/libexec/synos-update-initramfs"),
                os.path.realpath(tmp / "usr/sbin/update-initramfs"),
            )
            self.assertFalse((tmp / "usr/sbin/update-initramfs.synos-dracut").exists())

            second = self._run_postinst_configure(tmp)
            self.assertEqual(0, second.returncode, second.stderr)
            divert_state = (tmp / "divert-state").read_text(encoding="utf-8")
            self.assertNotIn(
                "update-initramfs", divert_state,
                "a reconfigure must not divert the guard's own symlink onto itself",
            )
            self.assertEqual(
                str(tmp / "usr/libexec/synos-update-initramfs"),
                os.path.realpath(tmp / "usr/sbin/update-initramfs"),
            )
            self.assertFalse(
                (tmp / "usr/sbin/update-initramfs.synos-dracut").exists(),
                "the diverted path must never come to exist as a symlink to the guard itself",
            )

    def test_postinst_diverts_a_real_compat_shim_and_stays_idempotent(self) -> None:
        """When a base does provide a real update-initramfs (an upgrade from
        a system where one was installed), the postinst must divert it
        exactly once and keep it diverted -- not itself -- across a later
        reconfigure."""
        import os
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            self._write_executable(tmp / "usr/libexec/synos-update-initramfs")
            self._write_executable(tmp / "usr/libexec/synos-update-grub")
            self._write_executable(tmp / "usr/sbin/update-grub")
            self._write_executable(
                tmp / "usr/sbin/update-initramfs",
                '#!/bin/sh\necho "real update-initramfs $*"\n',
            )

            first = self._run_postinst_configure(tmp)
            self.assertEqual(0, first.returncode, first.stderr)
            divert_target = tmp / "usr/sbin/update-initramfs.synos-dracut"
            self.assertTrue(divert_target.exists())
            self.assertIn("real update-initramfs", divert_target.read_text(encoding="utf-8"))
            self.assertEqual(
                str(tmp / "usr/libexec/synos-update-initramfs"),
                os.path.realpath(tmp / "usr/sbin/update-initramfs"),
            )

            second = self._run_postinst_configure(tmp)
            self.assertEqual(0, second.returncode, second.stderr)
            self.assertIn(
                "real update-initramfs", divert_target.read_text(encoding="utf-8"),
                "the diverted file must still be the real implementation, not the guard",
            )
            divert_state = (tmp / "divert-state").read_text(encoding="utf-8")
            self.assertEqual(
                1, divert_state.count("update-initramfs.synos-dracut"),
                "the diversion must be registered exactly once, not re-added on reconfigure",
            )

    def test_build_recipe_leaves_artifact_validation_to_tests(self) -> None:
        build = (ROOT / "build.sh").read_text()
        makefile = (ROOT / "makefile").read_text()
        for action in ("grub-mkfont", "mksquashfs", "implantisomd5 --force"):
            self.assertIn(action, build)
        for dependency in ("fonts-unifont", "isomd5sum", "sbsigntool"):
            self.assertIn(dependency, makefile)
        for test_logic in (
            "GRUB font source not found",
            "The dedicated Dracut Live initrd is missing",
            "unsquashfs -s",
            "sbverify --list",
            "fsck.vfat -vn",
            "command -v implantisomd5",
        ):
            self.assertNotIn(test_logic, build)
        for progress_marker in (
            'judge "Prepare readable Live GRUB font"',
            'judge "Copy kernel files"',
            'judge "Compress rootfs"',
            'judge "Create EFI boot image"',
            'judge "Embed ISO media checksum"',
        ):
            self.assertIn(progress_marker, build)

    def test_build_dependencies_include_media_check_implanter(self) -> None:
        makefile = (ROOT / "makefile").read_text()
        self.assertIn("isomd5sum", makefile)

    def test_arm64_cross_build_uses_target_root_secure_boot_payload(self) -> None:
        makefile = (ROOT / "makefile").read_text()
        build = (ROOT / "build.sh").read_text()
        arm64_dependencies = makefile.split("DEPS_arm64 :=", 1)[1].split(
            "HOST_ARCH", 1
        )[0]

        self.assertNotIn("shim-signed", arm64_dependencies)
        self.assertNotIn("grub-efi-arm64", arm64_dependencies)
        self.assertIn("qemu-user-binfmt", makefile)
        self.assertIn("ifneq ($(HOST_ARCH),$(TARGET_ARCH))", makefile)
        for payload in (
            "gcdaa64.efi.signed",
            "shimaa64.efi.signed.latest",
            "mmaa64.efi",
        ):
            self.assertIn(payload, build)
        self.assertIn("mmd -i efiboot.img ::/EFI ::/EFI/BOOT", build)
        self.assertIn("::/EFI/BOOT/BOOTAA64.EFI", build)
        self.assertIn("::/EFI/BOOT/grubaa64.efi", build)
        self.assertIn("::/EFI/BOOT/mmaa64.efi", build)
        self.assertIn("search --no-floppy --label --set=synos_iso", build)
        self.assertIn("configfile \\$prefix/grub.cfg", build)

    def test_grub_acceptance_contract_covers_temporary_and_persistent_modes(self) -> None:
        common = (
            "root=live:CDLABEL=synos rd.live.dir=LiveOS "
            "rd.live.squashimg=rootfs.squashfs rd.synos.live=1"
        )
        entries = [
            f"linux /LiveOS/vmlinuz {common} rd.overlay locale=l{index}\n"
            "initrd /LiveOS/initrd"
            for index in range(28)
        ]
        entries.extend(
            (
                f"linux /LiveOS/vmlinuz {common} rd.overlay nomodeset\n"
                "initrd /LiveOS/initrd",
                f"linux /LiveOS/vmlinuz {common} "
                "rd.overlay=LABEL=SYNOS-PERSIST "
                "rd.live.overlay.cowfs=ext4\ninitrd /LiveOS/initrd",
                f"linux /LiveOS/vmlinuz {common} rd.overlay rd.live.check=1\n"
                "initrd /LiveOS/initrd",
            )
        )
        _validate_dracut_live_contract("\n".join(entries))
        with self.assertRaises(ConfigurationError):
            _validate_dracut_live_contract("\n".join(entries).replace(
                "rd.overlay=LABEL=SYNOS-PERSIST", "persistent"
            ))

    def test_efi_inspector_reads_the_pe_machine_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            payload = Path(directory) / "BOOTAA64.EFI"
            image = bytearray(0x88)
            image[:2] = b"MZ"
            struct.pack_into("<I", image, 0x3C, 0x80)
            image[0x80:0x84] = b"PE\0\0"
            struct.pack_into("<H", image, 0x84, 0xAA64)
            payload.write_bytes(image)

            self.assertEqual(0xAA64, _pe_machine(payload))
            payload.write_bytes(b"not a PE image")
            with self.assertRaisesRegex(ConfigurationError, "not a PE image"):
                _pe_machine(payload)


if __name__ == "__main__":
    unittest.main()
