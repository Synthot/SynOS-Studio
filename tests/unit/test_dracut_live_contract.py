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
        self.assertIn("--no-hostonly", script)
        self.assertIn("--no-hostonly-cmdline", script)
        for module in (
            "dmsquash-live",
            "dmsquash-live-autooverlay",
            "overlayfs",
            "synos-live-layers",
        ):
            self.assertIn(module, script)
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
        self.assertIn('update-initramfs -c -k "$kernel_version"', live)
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
