#!/bin/bash

set -e                  # exit on error
set -o pipefail         # exit on pipeline error
set -u                  # treat unset variable as error

# A snapshot-pinned build must not ship the frozen mirror: installed systems
# update from the live archive named in the manifest.
if [ "${SNAPSHOT_PINNED:-false}" = "true" ]; then
    print_ok "Restoring live apt mirrors for the installed system..."
    sed -i \
        -e "s|${APT_SOURCE}|${RELEASE_APT_SOURCE}|g" \
        -e "s|${SECURITY_SOURCE}|${RELEASE_SECURITY_SOURCE}|g" \
        "/etc/apt/sources.list.d/${BASE_ID}.sources"
    rm -f /etc/apt/apt.conf.d/80-snapshot-build
    judge "Restore live apt mirrors"
fi

# The build-time local repository never ships: installed systems update from
# the published SynOS repository configured by synos-apt-config.
print_ok "Removing the build-time local package repository source..."
rm -f /etc/apt/sources.list.d/synos-local.sources /etc/apt/preferences.d/synos-local /etc/apt/apt.conf.d/98-build-noninteractive
judge "Remove local repository source"

# Clean up root home
print_ok "Cleaning up /root/..."
rm -f /root/.config/mimeapps.list || true
rm -rf /root/.local/share/gnome-shell/extensions || true
rm -rf /root/.cache || true
judge "Clean up /root/"

# Clean up apt cache
print_ok "Cleaning up apt cache..."
if mountpoint -q /var/cache/apt/archives; then
    print_ok "Archive cache is the build host's persistent cache; left in place (unmounted before the image is sealed)."
else
    find /var/cache/apt/archives -mindepth 1 -delete 2>/dev/null || true
fi
rm -f /var/cache/apt/pkgcache.bin /var/cache/apt/srcpkgcache.bin || true
judge "Clean up apt cache"

# Clean up apt lists (save ~50-80MB in the squashfs; the installed system
# will re-fetch them on first apt update anyway)
print_ok "Cleaning up apt lists..."
find /var/lib/apt/lists -mindepth 1 -maxdepth 1 ! -name 'lock' ! -name 'partial' -delete 2>/dev/null || true
judge "Clean up apt lists"

# Clean up log files -- contents only, never the directories. Plenty of
# packages ship or create their own directory under /var/log (dpkg -L
# <pkg> lists it as a path the package owns), with its own ownership and
# mode, created once by a maintainer script -- not recreated at every
# start, and never by dpkg again once the package is already "installed".
# auditd is the one this build was actually caught by: deleting
# /var/log/audit here left the daemon with nowhere to write, so it failed
# every boot, live and installed, with "Could not open dir /var/log/audit"
# (exit 6/NOTCONFIGURED), retried five times, hit systemd's start-limit,
# and stayed failed forever -- but it is not the only one. nginx-common
# ships ./var/log/nginx/ the same way; cups, samba and plenty of the rest
# of the catalog do too. Deleting the directory itself (the old
# `-mindepth 1 -delete`, confirmed against a real tree to remove every
# such directory along with its mode and ownership) breaks all of them
# identically, silently, until whichever daemon happens to need that
# directory tries to start. `-type f` clears every log file -- the actual
# build-time content this step exists to drop -- while leaving every
# package-created directory, its mode and its ownership exactly as the
# package's own maintainer script set them, so nothing here needs to know
# which packages care or special-case any one of them.
print_ok "Cleaning up log files..."
find /var/log -type f -delete 2>/dev/null || true
judge "Clean up log files"

# Any package apt left in dpkg's "rc" state (removed, but its config data
# was kept) is dead weight in the shipped image: nothing uses it, but it
# still shows up in `dpkg -l`, in `dpkg -s <meta-package>`'s own Depends
# resolution, and in the packages.lock/SBOM build_iso() generates further
# down this same build (the manifest step there lists every package dpkg
# still knows about, "rc" included, not just "ii"). This is exactly what a
# Conflicts/Replaces/Provides package swap leaves behind mid-build -- e.g.
# a profile's own firmware-audio role asking for Debian's firmware-sof-signed
# where firmware-sof-synos (Provides: firmware-sof-signed) was already
# installed by the desktop stack group: apt correctly removes the one and
# installs the other in the same transaction (synos-desktop-core's
# `firmware-sof-synos | firmware-sof-signed` Depends ends up satisfied,
# verified against a live boot), but dpkg keeps the removed side's status
# entry around as "rc" rather than purging it outright. Purge every such
# leftover here, before the manifest is generated, so the shipped image's
# own package database -- and everything derived from it -- reflects only
# what is actually installed.
print_ok "Purging packages left in dpkg's removed-but-not-purged state..."
RC_PACKAGES=$(dpkg -l | awk '$1 == "rc" {print $2}')
if [ -n "$RC_PACKAGES" ]; then
    # shellcheck disable=SC2086
    dpkg --purge $RC_PACKAGES || true
fi
judge "Purge packages left in dpkg's removed-but-not-purged state"

# Truncate machine id
print_ok "Truncating machine id..."
truncate -s 0 /etc/machine-id || true
truncate -s 0 /var/lib/dbus/machine-id || true
judge "Truncate machine id"

# Ubuntu enables ssh.socket when openssh-server is first installed. The Live
# image must not expose a listener merely because it carries the payload. This
# is deliberately an image-finalization action, not a global systemd preset:
# installed machines retain administrator-selected SSH state across upgrades
# and preset-all operations.
print_ok "Disabling Secure Shell listeners in the Live image..."
systemctl disable ssh.service ssh.socket
judge "Disable Live Secure Shell listeners"

# SSH host keys identify one machine and must never be cloned through the
# SquashFS template. The native installer creates target-owned keys after the
# Live system has been copied.
print_ok "Removing build-time SSH host identity..."
if [[ -d /etc/ssh ]]; then
    find /etc/ssh -maxdepth 1 \
        \( -name 'ssh_host_*_key' -o -name 'ssh_host_*_key.pub' \) \
        -delete
fi
judge "Remove build-time SSH host identity"

# Remove timezone files (systemd.timezone= on kernel cmdline sets them at boot)
print_ok "Removing timezone files..."
rm -f /etc/localtime /etc/timezone || true
judge "Remove timezone files"

# Clean bash history and temp files
print_ok "Removing bash history and temporary files..."
find /tmp -mindepth 1 -delete 2>/dev/null || true
rm -f ~/.bash_history 2>/dev/null || true
export HISTSIZE=0
judge "Remove bash history and temporary files"

# Remove usr-is-merged folders
print_ok "Removing usr-is-merged folders..."
rm -rf /bin.usr-is-merged /lib.usr-is-merged /sbin.usr-is-merged || true
judge "Remove usr-is-merged folders"
