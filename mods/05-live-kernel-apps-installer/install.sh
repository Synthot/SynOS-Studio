#!/bin/bash
set -e                  # exit on error
set -o pipefail         # exit on pipeline error
set -u                  # treat unset variable as error
#==========================
# Product stack: live boot, desktop, installer and conditional payloads,
# as resolved from packages/stack.yml (see STACK_* in args.sh).
#==========================
# shellcheck disable=SC1091
source /root/mods/stack.sh

wait_network

print_ok "Installing the Dracut Live stack..."
install_stack_group live

print_ok "Installing the desktop stack..."
# DKMS legitimately needs gcc/make/dpkg-dev, but dpkg-dev only recommends the
# unrelated build-essential C++ stack; the group's exclude list keeps it out.
install_stack_group desktop

print_ok "Installing the native installer..."
install_stack_group installer

# Carry the Btrfs recovery UI inside the ISO without making it a desktop
# metapackage dependency. The native installer retains this package for Btrfs
# targets and purges it from ext4 targets through its explicit cleanup policy.
print_ok "Installing conditional Disk Snapshots Manager payload..."
install_stack_group snapshots
judge "Install Disk Snapshots Manager payload"

# Carry VMware desktop integration in the amd64 Live image so VMware guests
# can resize dynamically before and after installation. The native installer
# retains it for VMware targets and purges it everywhere else.
print_ok "Installing conditional VMware guest integration payload..."
install_stack_group vmware
