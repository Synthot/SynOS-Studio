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
    # Every name here was asked for by name — resolved by tools/render_manifest.py
    # through bases/${BASE_ID}/packages.map (and, for a spellcheck/translations
    # role, bases/${BASE_ID}/language-packages.map, which drops a language's own
    # verified-absent packages before they ever reach PROFILE_INSTALL_PACKAGES at
    # all). Reaching this branch means the archive this image is building against
    # does not have a name the manifest resolution believed it would — one line
    # per package, unmistakable, not folded into a single list a scrollback can
    # bury; not a hard failure (a profile still gets everything else it asked
    # for), but never silent either.
    for pkg in "${SKIPPED[@]}"; do
        print_warn "PACKAGE UNEXPECTEDLY MISSING on ${BASE_ID} ${TARGET_SUITE}: '${pkg}' was requested by name for this build and is not installable — the image will ship without it. This is unexpected; check bases/${BASE_ID}/packages.map (and language-packages.map, if this is a language-related name) against the real archive."
    done
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
