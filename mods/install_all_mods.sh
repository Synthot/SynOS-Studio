#!/bin/bash

#==========================
# Set up the environment
#==========================
set -e                  # exit on error
set -o pipefail         # exit on pipeline error
set -u                  # treat unset variable as error
export HOME=/root
# These files are copied into the chroot immediately before this script runs.
# shellcheck disable=SC1091
source /root/mods/shared.sh
# shellcheck disable=SC1091
source /root/mods/args.sh

#==========================
# Variables for mods
#==========================
print_ok "Building variables for mods:"

echo "TARGET_UBUNTU_VERSION=$TARGET_UBUNTU_VERSION"
echo "APT_SOURCE=$APT_SOURCE"
echo "TARGET_NAME=$TARGET_NAME"
echo "TARGET_BUSINESS_NAME=$TARGET_BUSINESS_NAME"
echo "TARGET_BUILD_VERSION=$TARGET_BUILD_VERSION"

#==========================
# One initrd, built once
#==========================
# Package scripts call `update-initramfs -u`, and some (the NVIDIA driver's)
# pass the running kernel, which is the build host's, not the image's; with
# dracut that is a hard failure. Updates are switched off for the whole
# package installation and the initrd is generated once, explicitly, by
# 80-dracut-live-image, which switches them back on for the installed system.
mkdir -p /etc/initramfs-tools
printf 'update_initramfs=no\n' > /etc/initramfs-tools/update-initramfs.conf

#==========================
# Execute mods
#==========================
# The mods run in two phases so the build order is a stated contract, not an
# accident of numeric file naming. 85-cleanup-mod deletes /var/lib/apt/lists
# (see its install.sh) to keep the image small; if it ran as just another
# numbered mod here, anything that installs packages *after* the mods loop
# (the Ansible profile step, build.sh's run_ansible_chroot) would silently
# find an empty apt cache. build.sh instead runs this script twice:
#   install_all_mods.sh early    -> every mod except the cleanup mod
#   install_all_mods.sh cleanup  -> only the cleanup mod, run last, after
#                                    the profile has been applied
# Calling this script with no phase (or any other value) runs every mod in
# file order, for anyone invoking it by hand outside build.sh.
CLEANUP_MOD="85-cleanup-mod"
PHASE="${1:-all}"

for mod in "$SCRIPT_DIR"/*; do
    if [[ -d "$mod" && -f "$mod/install.sh" ]]; then
        mod_name="$(basename "$mod")"
        case "$PHASE" in
            early)
                [[ "$mod_name" == "$CLEANUP_MOD" ]] && continue
                ;;
            cleanup)
                [[ "$mod_name" == "$CLEANUP_MOD" ]] || continue
                ;;
        esac
        print_info "Processing mod: $mod"
        mod_t0=$(date +%s)
        (
            cd "$mod" && \
            chmod +x install.sh && \
            bash "$mod/install.sh"
        )
        mod_dur=$(( $(date +%s) - mod_t0 ))
        print_ok "Mod $mod_name finished in ${mod_dur}s"
        # This runs inside the chroot, before most of the image's own
        # packages exist, so it writes plain JSON lines with printf (shared.sh,
        # not python) to a file next to this script -- SCRIPT_DIR here is
        # /root/mods (args.sh derives it from $0, not the sourcing script's
        # own path). build.sh's run_chroot/run_cleanup_mod read it back into
        # the host-side ledger right after this script returns to them.
        record_phase_timing "$SCRIPT_DIR/timings.jsonl" "$mod_name" "$mod_dur"
    fi
done
