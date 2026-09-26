#!/bin/bash

set -e                  # exit on error
set -o pipefail         # exit on pipeline error
set -u                  # treat unset variable as error

print_ok "Setting up hostname..."
echo "${BRAND_HOSTNAME_PREFIX:-$TARGET_NAME}" > /etc/hostname
judge "Set up hostname to ${BRAND_HOSTNAME_PREFIX:-$TARGET_NAME}"

print_ok "Filtering available language packs (resolved from bases/${BASE_ID}/packages.map: translations, and bases/${BASE_ID}/language-packages.map)..."
VALID_PACKAGES=()
GONE_PACKAGES=()
for pkg in $LANGUAGE_PACKS; do
    if apt-get install -s -y "$pkg" >/dev/null 2>&1; then
        VALID_PACKAGES+=("$pkg")
    else
        GONE_PACKAGES+=("$pkg")
    fi
done

if [ "${#GONE_PACKAGES[@]}" -gt 0 ]; then
    # Every name in $LANGUAGE_PACKS was asked for by name — resolved through
    # bases/${BASE_ID}/language-packages.map (tools/generate_language_packages.py),
    # which already checked it against a real archive snapshot. Reaching this
    # branch means the archive has moved since that check, not that this was
    # ever expected to be optional; say so plainly instead of a routine-looking
    # one-line warning, so it is not mistaken for the ordinary, already-verified
    # "this language has nothing for this role" case (bases/${BASE_ID}/language-packages.map's own `unavailable:` entries, which never reach this
    # loop at all — render_manifest.py drops those before LANGUAGE_PACKS is built).
    print_warn "LANGUAGE PACKAGE UNEXPECTEDLY MISSING: ${#GONE_PACKAGES[@]} name(s) this build asked for by name are not installable on this image's archive right now: ${GONE_PACKAGES[*]}"
    print_warn "These were resolved through bases/${BASE_ID}/language-packages.map, which already verified them against a real archive snapshot — this means the archive has changed since, not that this is expected. The image will ship without them. Re-run tools/generate_language_packages.py and tools/packages_map_conformance.py to confirm."
fi
mkdir -p /usr/share/synos
{
    echo "requested=${LANGUAGE_PACKS}"
    echo "installed=${VALID_PACKAGES[*]:-}"
    echo "unexpectedly_missing=${GONE_PACKAGES[*]:-}"
} > /usr/share/synos/language-packs.txt
judge "Record language pack evidence"

print_ok "Installing available language packs..."
if [ "${#VALID_PACKAGES[@]}" -gt 0 ]; then
    apt install -y "${VALID_PACKAGES[@]}" \
        --no-install-recommends
    judge "Install language packs"
else
    print_warn "No language packs were valid for installation."
fi
