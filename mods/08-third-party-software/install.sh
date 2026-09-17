#!/bin/bash
set -e                  # exit on error
set -o pipefail         # exit on pipeline error
set -u                  # treat unset variable as error
#==========================
# Third-party software declared by the profile and staged by
# tools/render_manifest.py into /root/software:
#   repos/*.sources + keyrings/*.asc, repos.txt   apt repositories and their packages
#   debs.txt                                      vendor .deb files (sha256 verified)
#   flatpak.txt                                   remotes and apps, preinstalled or first boot
#   appimages.txt                                 AppImages (sha256 verified) with launchers
# Everything here is verified against a key or checksum before it enters
# the image; nothing is fetched that the profile did not name.
#==========================

STAGE=/root/software
if [ ! -d "$STAGE" ] || [ -z "$(ls -A "$STAGE")" ]; then
    print_ok "No third-party software declared by profile ${PROFILE_ID}."
    exit 0
fi

wait_network

#--- apt repositories --------------------------------------------------
if [ -f "$STAGE/repos.txt" ]; then
    install -d -m 0755 /etc/apt/keyrings
    while IFS=$'\t' read -r name packages; do
        [ -z "$name" ] && continue
        print_ok "Adding repository $name..."
        install -m 0644 "$STAGE/keyrings/$name.asc" "/etc/apt/keyrings/$name.asc"
        install -m 0644 "$STAGE/repos/$name.sources" "/etc/apt/sources.list.d/$name.sources"
        judge "Add repository $name"
    done < "$STAGE/repos.txt"

    print_ok "Refreshing package lists with the new repositories..."
    apt update
    judge "apt update"

    while IFS=$'\t' read -r name packages; do
        [ -z "$name" ] || [ -z "${packages:-}" ] && continue
        print_ok "Installing from $name: $packages"
        # shellcheck disable=SC2086
        apt install -y --no-install-recommends $packages
        judge "Install packages from $name"
    done < "$STAGE/repos.txt"
fi

#--- vendor .deb files -------------------------------------------------
if [ -f "$STAGE/debs.txt" ]; then
    tmp=$(mktemp -d)
    while IFS=$'\t' read -r url sha256; do
        [ -z "$url" ] && continue
        file="$tmp/$(basename "$url")"
        print_ok "Downloading $url..."
        curl --fail --silent --show-error --location -o "$file" "$url"
        echo "$sha256  $file" | sha256sum --check --status
        judge "Verify $(basename "$url") sha256"
        apt install -y --no-install-recommends "$file"
        judge "Install $(basename "$url")"
    done < "$STAGE/debs.txt"
    rm -rf "$tmp"
fi

#--- flatpak -----------------------------------------------------------
if [ -f "$STAGE/flatpak.txt" ]; then
    preinstall=$(awk -F'|' '$1=="preinstall"{print $2}' "$STAGE/flatpak.txt")
    print_ok "Installing Flatpak (preinstall=$preinstall)..."
    apt install -y --no-install-recommends flatpak
    judge "Install flatpak"

    while IFS='|' read -r kind name url; do
        [ "$kind" = remote ] || continue
        print_ok "Adding Flatpak remote $name..."
        flatpak remote-add --system --if-not-exists "$name" "$url"
        judge "Add Flatpak remote $name"
    done < "$STAGE/flatpak.txt"

    mapfile -t apps < <(awk -F'|' '$1=="app"{print $2}' "$STAGE/flatpak.txt")
    if [ "${#apps[@]}" -gt 0 ]; then
        if [ "$preinstall" = true ]; then
            print_ok "Preinstalling ${#apps[@]} Flatpak apps into the image..."
            flatpak install --system --noninteractive --assumeyes "${apps[@]}"
            judge "Preinstall Flatpak apps"
        else
            print_ok "Deferring ${#apps[@]} Flatpak apps to first boot..."
            install -d -m 0755 /etc/synos
            printf '%s\n' "${apps[@]}" > /etc/synos/flatpak-first-boot
            cat > /etc/systemd/system/synos-flatpak-first-boot.service <<'UNIT'
[Unit]
Description=SynOS first boot: install the profile's Flatpak applications
After=network-online.target
Wants=network-online.target
ConditionPathExists=/etc/synos/flatpak-first-boot

[Service]
Type=oneshot
ExecStart=/bin/sh -c 'xargs -a /etc/synos/flatpak-first-boot flatpak install --system --noninteractive --assumeyes && rm -f /etc/synos/flatpak-first-boot'

[Install]
WantedBy=multi-user.target
UNIT
            install -d /etc/systemd/system/multi-user.target.wants
            ln -sf /etc/systemd/system/synos-flatpak-first-boot.service \
                /etc/systemd/system/multi-user.target.wants/synos-flatpak-first-boot.service
            judge "Schedule Flatpak apps for first boot"
        fi
    fi
fi

#--- appimages ---------------------------------------------------------
if [ -f "$STAGE/appimages.txt" ]; then
    install -d -m 0755 /opt/appimages /usr/share/applications
    while IFS=$'\t' read -r name url sha256; do
        [ -z "$name" ] && continue
        target="/opt/appimages/$name.AppImage"
        print_ok "Downloading AppImage $name..."
        curl --fail --silent --show-error --location -o "$target" "$url"
        echo "$sha256  $target" | sha256sum --check --status
        judge "Verify $name sha256"
        chmod 0755 "$target"
        cat > "/usr/share/applications/appimage-$name.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=$name
Exec=$target
Icon=application-x-executable
Categories=Utility;
DESKTOP
        judge "Install AppImage $name"
    done < "$STAGE/appimages.txt"
fi

#--- evidence ----------------------------------------------------------
install -d -m 0755 /usr/share/synos
cp -r "$STAGE" /usr/share/synos/third-party-software
rm -rf /usr/share/synos/third-party-software/keyrings
judge "Record third-party software evidence"
