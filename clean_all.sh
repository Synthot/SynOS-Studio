#!/bin/bash

#==========================
# Set up the environment
#==========================
set -e                  # exit on error
set -o pipefail         # exit on pipeline error
set -u                  # treat unset variable as error
SCRIPT_DIR="$(dirname "$(readlink -f "$0")")"
export SCRIPT_DIR

function clean_all() {
    echo "Cleaning up..."
    sudo umount "$SCRIPT_DIR/new_building_os/sys" || sudo umount -lf "$SCRIPT_DIR/new_building_os/sys" || true
    sudo umount "$SCRIPT_DIR/new_building_os/proc" || sudo umount -lf "$SCRIPT_DIR/new_building_os/proc" || true
    sudo umount "$SCRIPT_DIR/new_building_os/dev" || sudo umount -lf "$SCRIPT_DIR/new_building_os/dev" || true
    sudo umount "$SCRIPT_DIR/new_building_os/run" || sudo umount -lf "$SCRIPT_DIR/new_building_os/run" || true
    sudo umount "$SCRIPT_DIR/image/isolinux/efi" || sudo umount -lf "$SCRIPT_DIR/image/isolinux/efi" || true
    sudo rm -rf "$SCRIPT_DIR/new_building_os" || true
    sudo rm -rf "$SCRIPT_DIR/image" || true
    rm -f "$SCRIPT_DIR"/*.iso || true
}

# =============   main  ================
cd "$SCRIPT_DIR"

clean_all

# Build outputs inside package recipes (created as root by the container build).
for d in packages/*/upstream/obj packages/*/upstream/deploy packages/*/upstream/target packages/*/upstream/src/target packages/*/upstream/locale; do
    [ -e "$d" ] && sudo rm -rf "$d"
done
sudo rm -rf .build/packages-work .build/repo .build/forks packages/.build
echo "Package build outputs removed."
