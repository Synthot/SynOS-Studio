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
# One simulated transaction answers "can every one of these install
# together" far more cheaply than asking apt about each name in its own
# process: measured against a real archive, ~8s for 39 packages asked
# about together against ~109s asking apt about each of the 39
# individually — every apt invocation repays the same archive-index load
# the previous one already paid for, so batching pays that cost once
# instead of once per package. That is also the expected case: packages.map
# (see below) is supposed to have already dropped names this suite's
# archive cannot supply, so the candidate list should resolve as one unit
# on a healthy build.
#
# A single "apt install pkg-a pkg-b" transaction aborts entirely the
# instant one name cannot be resolved, though, and cannot say *which* one
# — so a failed batch falls back to asking apt about each candidate on its
# own, exactly as this whole check used to work name-by-name, every time.
# A cache lookup per name (no solver) was measured too, to catch a name
# the archive simply lacks before ever risking the batch: on the same
# archive it did correctly avoid a name-by-name fallback for that failure
# alone, but the lookup itself is not actually cheap in wall time — 39 of
# them cost more (~47s) than the one batched transaction they were meant
# to protect (~8s), because each one repays that same index load. It is
# not used here for that reason: it made the expected case slower to make
# an unexpected case somewhat faster.
resolve_installable_profile_packages() {
    local candidates=("$@")
    [ "${#candidates[@]}" -eq 0 ] && return 0
    if apt-get install -s -y "${candidates[@]}" >/dev/null 2>&1; then
        INSTALL+=("${candidates[@]}")
        return 0
    fi
    local pkg
    for pkg in "${candidates[@]}"; do
        if apt-get install -s -y "$pkg" >/dev/null 2>&1; then
            INSTALL+=("$pkg")
        else
            SKIPPED+=("$pkg")
        fi
    done
}

INSTALL=()
SKIPPED=()
CANDIDATES=()
for pkg in ${PROFILE_INSTALL_PACKAGES:-}; do
    if dpkg -s "$pkg" >/dev/null 2>&1; then
        continue
    fi
    CANDIDATES+=("$pkg")
done
resolve_installable_profile_packages "${CANDIDATES[@]}"

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
