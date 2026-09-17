#!/bin/bash
set -e                  # exit on error
set -o pipefail         # exit on pipeline error
set -u                  # treat unset variable as error
#==========================
# Brand kit: install the <brand>-branding package rendered by
# tools/render_brand.py from branding/<brand>/. It owns os-release, icons,
# the Plymouth theme, the login logo, the wallpaper and desktop defaults.
# Runs after the desktop (05) so plymouth and dconf exist, and before the
# live initrd (80) so the boot splash is embedded.
#==========================

shopt -s nullglob
debs=(/root/branding/"${BRAND_ID}"-branding_*_all.deb)
if [ "${#debs[@]}" -eq 0 ]; then
    print_error "No brand kit package found in /root/branding for brand ${BRAND_ID}"
    exit 1
fi

print_ok "Installing brand kit ${debs[0]##*/}..."
apt install -y --no-install-recommends "${debs[0]}"
judge "Install brand kit"

# The kit may have been pulled in earlier by a stack package (synos-wallpapers
# depends on it) before Plymouth and dconf existed: run its configuration
# again now that the desktop is present.
print_ok "Re-running brand kit configuration on the complete desktop..."
dpkg-reconfigure "${BRAND_ID}-branding"
judge "Configure brand kit"

print_ok "Verifying brand identity..."
# shellcheck disable=SC1091
os_id=$(. /etc/os-release; printf '%s' "$ID")
test "$os_id" = "$BRAND_ID"
test "$(readlink -f /etc/alternatives/default.plymouth)" = "/usr/share/plymouth/themes/${BRAND_ID}/${BRAND_ID}.plymouth"
test -s "/usr/share/${BRAND_ID}/branding/brand.env"
judge "Verify brand identity (os-release ID=${os_id}, plymouth theme ${BRAND_ID})"
