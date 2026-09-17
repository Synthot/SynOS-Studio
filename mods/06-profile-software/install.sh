#!/bin/bash
set -e                  # exit on error
set -o pipefail         # exit on pipeline error
set -u                  # treat unset variable as error
#==========================
# Profile software: install what the manifest's profile declares and
# remove what it excludes. The lists are concrete package names resolved
# by tools/render_manifest.py through bases/<base>/packages.map, so this
# mod never needs to know which distribution it runs on.
#==========================

print_ok "Profile ${PROFILE_ID} (${PROFILE_CHAIN}) bundles: ${PROFILE_BUNDLES:-none}"

#--- install -------------------------------------------------------------
INSTALL=()
SKIPPED=()
for pkg in ${PROFILE_INSTALL_PACKAGES:-}; do
    if dpkg -s "$pkg" >/dev/null 2>&1; then
        continue
    fi
    if apt-get install -s -y "$pkg" >/dev/null 2>&1; then
        INSTALL+=("$pkg")
    else
        SKIPPED+=("$pkg")
    fi
done

if [ "${#SKIPPED[@]}" -gt 0 ]; then
    print_warn "Not installable on ${BASE_ID} ${TARGET_SUITE}, skipped: ${SKIPPED[*]}"
fi

if [ "${#INSTALL[@]}" -gt 0 ]; then
    print_ok "Installing ${#INSTALL[@]} profile packages..."
    apt install -y --no-install-recommends "${INSTALL[@]}"
    judge "Install profile packages"
else
    print_ok "No additional profile packages to install."
fi

#--- remove --------------------------------------------------------------
REMOVE=()
for pkg in ${PROFILE_REMOVE_PACKAGES:-}; do
    if dpkg -s "$pkg" >/dev/null 2>&1; then
        REMOVE+=("$pkg")
    fi
done

if [ "${#REMOVE[@]}" -gt 0 ]; then
    print_ok "Removing ${#REMOVE[@]} packages excluded by the profile..."
    apt purge -y "${REMOVE[@]}"
    apt autoremove -y --purge
    judge "Remove excluded packages"
else
    print_ok "No excluded packages present."
fi

#--- evidence ------------------------------------------------------------
mkdir -p /usr/share/synos
{
    echo "profile=${PROFILE_ID}"
    echo "chain=${PROFILE_CHAIN}"
    echo "bundles=${PROFILE_BUNDLES:-}"
    echo "installed=${INSTALL[*]:-}"
    echo "skipped=${SKIPPED[*]:-}"
    echo "removed=${REMOVE[*]:-}"
} > /usr/share/synos/profile-software.txt
judge "Record profile software evidence"
