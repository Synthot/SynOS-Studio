#!/bin/sh

command -v getarg >/dev/null 2>&1 || . /lib/dracut-lib.sh

getargbool 0 rd.synos.live || return 0

live_dir="$(getarg rd.live.dir)"
[ -n "$live_dir" ] || live_dir=LiveOS
squash_image="$(getarg rd.live.squashimg)"
[ -n "$squash_image" ] || squash_image=rootfs.squashfs

case "$live_dir" in
    /*|*..*|*[!A-Za-z0-9_./-]*)
        die "Invalid SynOS Live directory: $live_dir"
        return 1
        ;;
esac
case "$squash_image" in
    */*|*..*|*[!A-Za-z0-9_.-]*)
        die "Invalid SynOS Live image name: $squash_image"
        return 1
        ;;
esac

media_root=/run/initramfs/live
source_image="$media_root/$live_dir/$squash_image"
if [ ! -d "$media_root" ]; then
    die "SynOS Live media is unavailable at $media_root"
    return 1
fi
if [ ! -f "$source_image" ]; then
    die "SynOS Live root image is unavailable at $source_image"
    return 1
fi

runtime_root=/run/synos-live
mkdir -p "$NEWROOT/cdrom" "$runtime_root"
mount --bind "$media_root" "$NEWROOT/cdrom" || {
    die "SynOS could not preserve the Live media at /cdrom"
    return 1
}

# Dracut carries its /run mount across switch_root. Put the runtime contract on
# that shared mount so it cannot be hidden when initrd /run replaces newroot's
# initially empty /run directory during the pivot.
: > "$runtime_root/rootfs.squashfs"
mount --bind "$source_image" "$runtime_root/rootfs.squashfs" || {
    die "SynOS could not expose the Live root image to the installer"
    return 1
}

cat > "$runtime_root/environment" <<EOF
SYNOS_LIVE=1
SYNOS_LIVE_MEDIA=/cdrom
SYNOS_LIVE_SOURCE=/run/synos-live/rootfs.squashfs
SYNOS_LIVE_DIRECTORY=$live_dir
SYNOS_LIVE_IMAGE=$squash_image
EOF

info "SynOS Live media and installer source contracts are ready"
