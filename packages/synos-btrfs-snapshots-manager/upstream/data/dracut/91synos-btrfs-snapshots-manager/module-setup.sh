#!/bin/bash

check() {
    require_binaries \
        /usr/libexec/synos-btrfs-snapshots-manager-initramfs \
        /usr/libexec/synos-btrfs-snapshots-manager-confirm \
        /usr/bin/btrfs || return 1
    return 0
}

depends() {
    echo "btrfs fs-lib"
    return 0
}

installkernel() {
    instmods btrfs
}

install() {
    inst_multiple \
        /usr/libexec/synos-btrfs-snapshots-manager-initramfs \
        /usr/libexec/synos-btrfs-snapshots-manager-confirm \
        /usr/bin/btrfs \
        cat chmod cp ln mkdir mount umount readlink blkid
    inst_simple \
        /usr/share/synos-btrfs-snapshots-manager/recovery-protocol-version \
        /etc/synos-btrfs-snapshots-manager/recovery-protocol-version

    # Dracut strips every executable in the completed image.  The rollback
    # transaction binds the packaged confirmation engine byte-for-byte, so keep
    # this initramfs copy non-executable until the recovery hook needs it.
    chmod 0644 \
        "$initdir/usr/libexec/synos-btrfs-snapshots-manager-confirm" || return 1
    inst_hook pre-mount 50 "$moddir/synos-btrfs-snapshots-manager.sh"
}
