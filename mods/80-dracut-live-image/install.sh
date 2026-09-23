#!/bin/bash
set -e
set -o pipefail
set -u
# shellcheck disable=SC1091
source /root/mods/stack.sh

print_ok "Building the dedicated non-host-only Dracut Live initrd..."

kernel_version=$(find /lib/modules -mindepth 1 -maxdepth 1 -type d \
    -printf '%f\n' | sort -V | tail -n 1)

# Every package is installed: the image's own initrd is generated once, for
# the image's kernel, by dracut itself (the diverted update-initramfs would
# run the boot-proof verifier, which needs the GRUB configuration an
# installed system has and a build chroot does not), and update-initramfs is
# switched back on for the installed system (install_all_mods.sh switched it
# off for the build).
dracut --force "/boot/initrd.img-$kernel_version" "$kernel_version"
judge "Generate the installed system's initrd for $kernel_version"
printf 'update_initramfs=yes\n' > /etc/initramfs-tools/update-initramfs.conf

# Belt and suspenders: mod 05 already checked this right after installing the
# "live" stack group, but re-checking here is cheap and this is the step that
# actually pays for a missing module — 30+ minutes into a build, if it were
# the only check.
ensure_dracut_live_modules

live_initrd=/boot/synos-live-initrd.img
dracut \
    --force \
    --no-hostonly \
    --no-hostonly-cmdline \
    --add "$LIVE_DRACUT_MODULES" \
    --add-drivers "loop squashfs overlay" \
    "$live_initrd" \
    "$kernel_version"

judge "Build dedicated Dracut Live initrd"
