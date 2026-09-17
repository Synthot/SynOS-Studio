#!/bin/bash

check() {
    # This module is an ISO-build artifact. Installed-system host-only images
    # must never acquire Live root discovery merely because a stale package is
    # present while an installer transaction is still being finalized.
    [[ $hostonly ]] && return 1
    return 255
}

depends() {
    echo "dmsquash-live dmsquash-live-autooverlay overlayfs"
    return 0
}

install() {
    # The auto-overlay module carries a different priority prefix per
    # distribution (70 on Ubuntu, 90 on Debian): locate it by name.
    upstream=""
    for candidate in "$dracutbasedir"/modules.d/*dmsquash-live-autooverlay/create-overlay.sh; do
        [[ -x "$candidate" ]] && upstream="$candidate" && break
    done
    if [[ -z "$upstream" ]]; then
        dfatal "The upstream Dracut auto-overlay helper is unavailable under $dracutbasedir/modules.d"
        return 1
    fi
    inst_script "$upstream" "/sbin/create-overlay.upstream"
    # The dependency already installed /sbin/create-overlay. dracut-install
    # does not replace an existing destination reliably, so remove exactly
    # that initrd entry before installing the SynOS compatibility wrapper.
    rm -f "$initdir/sbin/create-overlay"
    inst_script "$moddir/synos-create-overlay.sh" "/sbin/create-overlay"
    inst_hook pre-pivot 90 "$moddir/synos-live-prepare.sh"
}
