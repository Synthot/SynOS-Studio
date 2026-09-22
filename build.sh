#!/bin/bash

#==========================
# Set up the environment
#==========================
set -e                  # exit on error
set -o pipefail         # exit on pipeline error
set -u                  # treat unset variable as error
SCRIPT_DIR="$(dirname "$(readlink -f "$0")")"
export SCRIPT_DIR

source "$SCRIPT_DIR/shared.sh"
source "$SCRIPT_DIR/args.sh"

# Map Debian arch name to GRUB target name (amd64 -> x86_64, arm64 -> arm64)
case "$TARGET_ARCH" in
    amd64) GRUB_EFI_TARGET="x86_64-efi" ;;
    arm64) GRUB_EFI_TARGET="arm64-efi" ;;
    *)
        print_error "Unsupported target architecture: $TARGET_ARCH"
        exit 1
        ;;
esac

function bind_signal() {
    print_ok "Bind signal..."
    trap umount_on_exit EXIT
    judge "Bind signal"
}

function clean() {
    print_ok "Cleaning up previous build..."
    sudo umount new_building_os/sys || sudo umount -lf new_building_os/sys || true
    sudo umount new_building_os/proc || sudo umount -lf new_building_os/proc || true
    sudo umount new_building_os/var/cache/apt/archives 2>/dev/null || true
    sudo umount new_building_os/dev || sudo umount -lf new_building_os/dev || true
    sudo umount new_building_os/run || sudo umount -lf new_building_os/run || true
    sudo rm -rf new_building_os image || true
    judge "Clean up build artifacts"
}

function download_base_system() {
    print_ok "Creating new_building_os directory..."
    sudo mkdir -p new_building_os
    judge "Create build directory"

    print_ok "Calling debootstrap to download the $BASE_ID $TARGET_SUITE base system (arch: $TARGET_ARCH)..."
    # The base adapter names the archive keyring; the matching host package
    # (ubuntu-keyring or debian-archive-keyring) is installed by the makefile.
    local keyring_args=()
    if [ -n "${APT_KEYRING:-}" ]; then
        if [ ! -f "$APT_KEYRING" ]; then
            print_error "Archive keyring $APT_KEYRING is missing on the build host (see DEPS_base_$BASE_ID in the makefile)"
            exit 1
        fi
        keyring_args=(--keyring="$APT_KEYRING")
    fi
    mkdir -p "$SCRIPT_DIR/.build/apt-cache"
    sudo debootstrap --arch="$TARGET_ARCH" --variant=minbase \
        --cache-dir="$SCRIPT_DIR/.build/apt-cache" \
        "${keyring_args[@]}" \
        --include=ca-certificates,wget,dbus \
        "$TARGET_SUITE" new_building_os "$APT_SOURCE"
    judge "Download base system"
}

function mount_folders() {
    if [ -d /run/systemd/system ] && [ -z "${SYNOS_IN_CONTAINER:-}" ]; then
        print_ok "Reloading systemd daemon..."
        sudo systemctl daemon-reload
        judge "Reload systemd daemon"
    else
        print_ok "No host systemd (container build); skipping daemon reload."
    fi

    print_ok "Mounting /dev /run from host to build dir..."
    sudo mount --bind /dev new_building_os/dev
    sudo mount --bind /run new_building_os/run
    judge "Mount /dev /run"

    # Downloaded packages persist across builds: the archive cache of the
    # chroot is the repository's .build/apt-cache, bind-mounted for the run.
    print_ok "Mounting the package download cache into the chroot..."
    mkdir -p "$SCRIPT_DIR/.build/apt-cache/partial"
    sudo mkdir -p new_building_os/var/cache/apt/archives
    sudo mount --bind "$SCRIPT_DIR/.build/apt-cache" new_building_os/var/cache/apt/archives
    judge "Mount package cache"

    print_ok "Mounting /proc /sys /dev/pts within chroot..."
    sudo chroot new_building_os mount none -t proc /proc
    sudo chroot new_building_os mount none -t sysfs /sys
    sudo chroot new_building_os mount none -t devpts /dev/pts
    judge "Mount /proc /sys /dev/pts"

    print_ok "Copying mods to chroot /root/mods..."
    sudo cp -r "$SCRIPT_DIR/mods" new_building_os/root/mods
    sudo cp "$SCRIPT_DIR/args.sh" new_building_os/root/mods/args.sh
    sudo cp "$SCRIPT_DIR/shared.sh" new_building_os/root/mods/shared.sh

    print_ok "Copying the $BRAND_ID brand kit package to chroot /root/branding..."
    BRAND_DEB=$(ls "$SCRIPT_DIR/.build/branding/$BRAND_ID/${BRAND_ID}-branding_"*_all.deb 2>/dev/null | head -n1)
    if [ -z "$BRAND_DEB" ]; then
        print_error "Brand kit package is missing; run 'make brand' (tools/render_brand.py)"
        exit 1
    fi
    sudo mkdir -p new_building_os/root/branding
    sudo cp "$BRAND_DEB" new_building_os/root/branding/
    judge "Copy brand kit package"

    print_ok "Copying the local SynOS package repository to chroot /root/repo..."
    sudo rm -rf new_building_os/root/repo
    if [ -f "$SCRIPT_DIR/${LOCAL_REPO_DIR:-.build/repo}/Packages" ]; then
        sudo cp -r "$SCRIPT_DIR/${LOCAL_REPO_DIR:-.build/repo}" new_building_os/root/repo
        judge "Copy local package repository"
    else
        print_error "the local package repository is missing; run 'make packages'"
        exit 1
    fi

    print_ok "Copying staged third-party software to chroot /root/software..."
    sudo rm -rf new_building_os/root/software
    sudo cp -r "$SCRIPT_DIR/${SOFTWARE_STAGING_DIR:-.build/software}" new_building_os/root/software
    judge "Copy staged software"
}

function render_sources_template() {
    # Render bases/<base>/sources.tmpl with the adapter variables. Only the
    # listed placeholders are substituted, so the template stays declarative.
    local template="$1"
    sed \
        -e "s|\${APT_MIRROR}|$APT_SOURCE|g" \
        -e "s|\${SECURITY_MIRROR}|$SECURITY_SOURCE|g" \
        -e "s|\${SUITE}|$TARGET_SUITE|g" \
        -e "s|\${COMPONENTS}|$APT_COMPONENTS|g" \
        -e "s|\${KEYRING}|$APT_KEYRING|g" \
        -e "s|\${ARCH}|$TARGET_ARCH|g" \
        "$template"
}

function setup_apt() {
    local base_dir="$SCRIPT_DIR/bases/$BASE_ID"
    local template="$base_dir/sources.tmpl"
    if [ ! -f "$template" ]; then
        print_error "Base adapter $base_dir has no sources.tmpl"
        exit 1
    fi

    print_ok "Setting up $BASE_ID apt sources in chroot from bases/$BASE_ID/sources.tmpl..."
    sudo mkdir -p new_building_os/etc/apt/sources.list.d
    render_sources_template "$template" | \
        sudo tee "new_building_os/etc/apt/sources.list.d/$BASE_ID.sources" > /dev/null
    judge "Set up $BASE_ID apt sources"

    if [ -f "$SCRIPT_DIR/${LOCAL_REPO_DIR:-.build/repo}/Packages" ]; then
        print_ok "Adding the local SynOS package repository as a build-time apt source..."
        local verify="Trusted: yes"    # unsigned local repository: trusted only here, removed by mod 85
        local public_key="$SCRIPT_DIR/${SYNOS_KEYS_DIR:-keys}/public/synos-archive-keyring.gpg"
        if [ "$(cat "$SCRIPT_DIR/${LOCAL_REPO_DIR:-.build/repo}/SIGNED" 2>/dev/null)" = "yes" ] && [ -s "$public_key" ]; then
            # The keyring package is itself in this repository, so the key goes in first.
            sudo install -D -m 0644 "$public_key" new_building_os/usr/share/keyrings/synos-archive-keyring.gpg
            verify="Signed-By: /usr/share/keyrings/synos-archive-keyring.gpg"
        fi
        sudo tee new_building_os/etc/apt/sources.list.d/synos-local.sources > /dev/null <<LOCALREPO
Types: deb
URIs: file:///root/repo
Suites: ./
$verify
LOCALREPO
        # Local packages replace same-named archive packages during the build.
        sudo tee new_building_os/etc/apt/preferences.d/synos-local > /dev/null <<LOCALPIN
Package: *
Pin: origin ""
Pin-Priority: 1001
LOCALPIN
        judge "Add local package repository"
    fi

    if [ "${SNAPSHOT_PINNED:-false}" = "true" ]; then
        print_ok "Build pinned to archive snapshot $SNAPSHOT_DATE; relaxing Release validity for the chroot..."
        echo 'Acquire::Check-Valid-Until "false";' | \
            sudo tee new_building_os/etc/apt/apt.conf.d/80-snapshot-build > /dev/null
        judge "Configure snapshot build"
    fi

    # The keyring named by the template must exist inside the chroot too.
    if [ -n "${APT_KEYRING:-}" ] && [ ! -f "new_building_os$APT_KEYRING" ]; then
        print_ok "Copying archive keyring $APT_KEYRING into the chroot..."
        sudo install -D -m 0644 "$APT_KEYRING" "new_building_os$APT_KEYRING"
        judge "Copy archive keyring"
    fi

    # Remove stale legacy-format sources.list (debootstrap artifact).
    # Debian 12+ and Ubuntu 24.04+ use deb822 .sources files instead.
    sudo rm -f new_building_os/etc/apt/sources.list

    # The build never answers prompts: keep dpkg's defaults for conffiles so a
    # collision fails loudly in the log instead of hanging on a question.
    print_ok "Configuring non-interactive dpkg in chroot..."
    printf 'Dpkg::Options {"--force-confdef";};\n' | sudo tee new_building_os/etc/apt/apt.conf.d/98-build-noninteractive > /dev/null
    judge "Configure non-interactive dpkg"

    print_ok "Enabling apt recommends in chroot..."
    echo 'APT::Install-Recommends "true";' | sudo tee new_building_os/etc/apt/apt.conf.d/99-enable-recommends > /dev/null
    judge "Enable apt recommends"

    print_ok "Running apt update in chroot..."
    sudo chroot new_building_os apt update
    judge "Apt update in chroot"

    # Upgrade base system BEFORE mods run.  Swap packages (mod 01)
    # must not be visible to this upgrade — apt would try to
    # "normalize" them back to Ubuntu's lower version and fail.
    print_ok "Upgrading base system packages..."
    sudo chroot new_building_os apt -y upgrade
    judge "Upgrade base system"
}

function run_chroot() {
    print_ok "Running install_all_mods.sh in new_building_os..."
    print_warn "============================================"
    print_warn "   The following will run in chroot ENV!"
    print_warn "============================================"
    sudo chroot new_building_os /usr/bin/env DEBIAN_FRONTEND=${DEBIAN_FRONTEND:-readline} /root/mods/install_all_mods.sh -
    print_warn "============================================"
    print_warn "   chroot ENV execution completed!"
    print_warn "============================================"
    judge "Run install_all_mods.sh in new_building_os"

    print_ok "Sleeping for 5 seconds to allow chroot to exit cleanly..."
    sleep 5
}

function run_ansible_chroot() {
    local collection_playbook="$SCRIPT_DIR/ansible/collections/ansible_collections/synos/workstation/playbooks/customize_chroot.yml"
    if ! command -v ansible-playbook >/dev/null 2>&1; then
        if [ "${ANSIBLE_CHROOT_REQUIRED:-false}" = "true" ]; then
            print_error "The profile needs Ansible inside the chroot (policy, hardening or playbooks) but ansible-playbook is not installed"
            exit 1
        fi
        print_warn "ansible-playbook not installed; profile policy playbooks skipped (nothing required them)"
        return 0
    fi
    print_ok "Applying the profile inside the chroot with Ansible..."
    local playbooks=("$collection_playbook")
    for playbook in ${ANSIBLE_PLAYBOOKS:-}; do
        playbooks+=("$SCRIPT_DIR/$playbook")
    done
    sudo env \
        ANSIBLE_COLLECTIONS_PATH="$SCRIPT_DIR/ansible/collections" \
        ANSIBLE_HOST_KEY_CHECKING=false \
        ansible-playbook \
            --inventory "$SCRIPT_DIR/new_building_os," \
            --connection community.general.chroot \
            --extra-vars "@$SCRIPT_DIR/${ANSIBLE_VARS_FILE:-.build/ansible-vars.json}" \
            "${playbooks[@]}"
    judge "Apply profile with Ansible in chroot"
}

function umount_folders() {
    print_ok "Cleaning mods from chroot /root/mods..."
    sudo rm -rf new_building_os/root/mods new_building_os/root/branding new_building_os/root/software new_building_os/root/repo
    judge "Clean up chroot /root/mods"

    print_ok "Unmounting /proc /sys /dev/pts within chroot..."
    sudo chroot new_building_os umount /dev/pts || sudo chroot new_building_os umount -lf /dev/pts
    sudo chroot new_building_os umount /sys || sudo chroot new_building_os umount -lf /sys
    sudo chroot new_building_os umount /proc || sudo chroot new_building_os umount -lf /proc
    judge "Unmount /proc /sys /dev/pts"

    print_ok "Unmounting the package cache and /dev /run outside of chroot..."
    sudo umount new_building_os/var/cache/apt/archives || sudo umount -lf new_building_os/var/cache/apt/archives || true
    sudo umount new_building_os/dev || sudo umount -lf new_building_os/dev
    sudo umount new_building_os/run || sudo umount -lf new_building_os/run
    judge "Unmount package cache and /dev /run"
}

function prepare_iso_directory() {
    print_ok "Creating image directory..."
    # image/ may be a mount point (the bundle launcher gives the engine a real
    # filesystem there, GRUB cannot probe a container's overlay): empty it then.
    sudo rm -rf image 2>/dev/null || sudo find image -mindepth 1 -delete
    mkdir -p image/{LiveOS,isolinux,.disk}
    judge "Create image directory"
}

function prepare_live_grub_font() {
    print_ok "Generating 28px Unicode font for the Live ISO..."
    mkdir -p \
        image/isolinux \
        image/boot/grub/fonts
    grub-mkfont \
        --size="28" \
        --output="image/isolinux/synos-unicode-28.pf2" \
        "/usr/share/fonts/opentype/unifont/unifont.otf"
    cp "image/isolinux/synos-unicode-28.pf2" \
        "image/boot/grub/fonts/synos-unicode-28.pf2"
    judge "Prepare readable Live GRUB font"
}

function write_iso_checksum() {
    # Writes $2 in the standard `sha256sum` format for $1 (name relative to
    # $1's own directory), so `sha256sum -c` can verify it from inside dist/.
    local iso_path="$1"
    local sha_path="$2"
    local iso_dir iso_base
    iso_dir="$(dirname "$iso_path")"
    iso_base="$(basename "$iso_path")"
    (cd "$iso_dir" && sha256sum "$iso_base") > "$sha_path"
}

function build_iso() {
    print_ok "Building ISO image..."

    # Copy the kernel and the separately-built non-host-only Live initrd.
    print_ok "Copying the Dracut Live boot artifacts to /LiveOS..."
    # Resolve the distro-maintained symlinks — they always point to the
    # current kernel, so we never pick a stale one left behind by apt.
    REAL_VMLINUZ=$(readlink -f new_building_os/vmlinuz 2>/dev/null)
    [ -f "$REAL_VMLINUZ" ] || REAL_VMLINUZ=$(readlink -f new_building_os/boot/vmlinuz 2>/dev/null)
    REAL_INITRD="new_building_os/boot/synos-live-initrd.img"
    sudo cp "$REAL_VMLINUZ" image/LiveOS/vmlinuz
    sudo cp "$REAL_INITRD" image/LiveOS/initrd
    judge "Copy kernel files"

    print_ok "Copying the brand GRUB theme..."
    GRUB_THEME_CFG=""
    if [ -d "$SCRIPT_DIR/.build/branding/$BRAND_ID/grub-theme" ]; then
        mkdir -p "image/boot/grub/themes/$BRAND_ID"
        cp -r "$SCRIPT_DIR/.build/branding/$BRAND_ID/grub-theme/." "image/boot/grub/themes/$BRAND_ID/"
        _theme_fonts=""
        for _pf2 in "image/boot/grub/themes/$BRAND_ID"/*.pf2; do
            [ -f "$_pf2" ] || continue
            _theme_fonts="$_theme_fonts
loadfont /boot/grub/themes/$BRAND_ID/$(basename "$_pf2")"
        done
        GRUB_THEME_CFG="insmod png
insmod gfxmenu$_theme_fonts
set theme=/boot/grub/themes/$BRAND_ID/theme.txt
export theme"
    elif [ -f "$SCRIPT_DIR/.build/branding/$BRAND_ID/grub-background.png" ]; then
        mkdir -p image/boot/grub
        cp "$SCRIPT_DIR/.build/branding/$BRAND_ID/grub-background.png" image/boot/grub/background.png
        GRUB_THEME_CFG="insmod png
if background_image /boot/grub/background.png ; then
    set color_normal=light-gray/black
    set color_highlight=white/dark-gray
fi"
    fi
    judge "Copy brand GRUB theme"

    print_ok "Generating grub.cfg..."
    touch "image/$TARGET_NAME"
    cp "$SCRIPT_DIR/args.sh" "image/$TARGET_NAME"
    judge "Copy build args to disk"

    TRY_TEXT="Try or Install $TARGET_BUSINESS_NAME"
    TOGO_TEXT="$TARGET_BUSINESS_NAME To Go (Persistent on USB)"
    LIVE_BOOT_ARGS="root=live:CDLABEL=$TARGET_NAME rd.live.dir=LiveOS rd.live.squashimg=rootfs.squashfs rd.overlay rd.synos.live=1"

    # The Live regional policy (locale|label|timezone|keyboard, one row per
    # region locale) is validated here; the boot menu offers ONE entry, booting
    # the default region (first row). The installer offers the other regions on
    # its first screen; nobody chooses a language in a boot menu.
    _LIVE_REGION_COUNT=0
    _DEFAULT_LOCALE=""; _DEFAULT_TZ=""; _DEFAULT_KBD=""
    while IFS="|" read -r _code _label _tz _kbd _extra; do
        [ -z "$_code" ] && continue
        if [ -z "$_label" ] || [ -z "$_tz" ] || [ -z "$_kbd" ] || [ -n "$_extra" ]; then
            print_error "Invalid Live regional policy entry: $_code"
            exit 1
        fi
        case "$_code:$_tz:$_kbd" in
            *[!A-Za-z0-9_+./:@-]*)
                print_error "Unsafe Live regional policy entry: $_code"
                exit 1
                ;;
        esac
        case "$_label" in
            *\"*|*\\*|*\$*)
                print_error "Unsafe Live GRUB label: $_label"
                exit 1
                ;;
        esac
        _LIVE_REGION_COUNT=$((_LIVE_REGION_COUNT + 1))
        if [ -z "$_DEFAULT_LOCALE" ]; then
            _DEFAULT_LOCALE=$_code; _DEFAULT_TZ=$_tz; _DEFAULT_KBD=$_kbd
        fi
    done <<< "$SUPPORTED_LIVE_REGIONS"
    if [ "$_LIVE_REGION_COUNT" -lt 1 ]; then
        print_error "Live regional policy must contain at least one entry (check regions: in manifest.yml)"
        exit 1
    fi
    DEFAULT_LIVE_ARGS="locale=${_DEFAULT_LOCALE}.UTF-8 timezone=${_DEFAULT_TZ} systemd.timezone=${_DEFAULT_TZ} rd.synos.keyboard=${_DEFAULT_KBD}"

    cat << EOF > image/isolinux/grub.cfg

search --set=root --file /$TARGET_NAME

set gfxmode=1440x900,1280x800,1280x720,1024x768,auto
insmod all_video
insmod gfxterm
insmod font
if loadfont /boot/grub/fonts/synos-unicode-28.pf2 ; then
    terminal_output gfxterm
elif loadfont /isolinux/synos-unicode-28.pf2 ; then
    terminal_output gfxterm
fi
$GRUB_THEME_CFG

set default="0"
set timeout=10

menuentry "$TRY_TEXT" {
    set gfxpayload=auto
    linux   /LiveOS/vmlinuz $LIVE_BOOT_ARGS $DEFAULT_LIVE_ARGS quiet splash ---
    initrd  /LiveOS/initrd
}

submenu "Advanced Options..." {
    menuentry "$TRY_TEXT (Safe Graphics)" {
        set gfxpayload=auto
        linux   /LiveOS/vmlinuz $LIVE_BOOT_ARGS $DEFAULT_LIVE_ARGS nomodeset ---
        initrd  /LiveOS/initrd
    }
    menuentry "$TOGO_TEXT" {
        set gfxpayload=auto
        linux   /LiveOS/vmlinuz root=live:CDLABEL=$TARGET_NAME rd.live.dir=LiveOS rd.live.squashimg=rootfs.squashfs rd.overlay=LABEL=SYNOS-PERSIST rd.live.overlay.cowfs=ext4 rd.synos.live=1 quiet splash ---
        initrd  /LiveOS/initrd
    }
    menuentry "Check installation media for defects (Integrity Check)" {
        set gfxpayload=auto
        linux   /LiveOS/vmlinuz $LIVE_BOOT_ARGS $DEFAULT_LIVE_ARGS rd.live.check=1 quiet splash ---
        initrd  /LiveOS/initrd
    }
}

if [ "\$grub_platform" == "efi" ]; then
    menuentry "Boot from next volume" {
        exit 1
    }
    menuentry "UEFI Firmware Settings" {
        fwsetup
    }
fi
EOF
    judge "Generate grub.cfg"


    # generate manifest
    print_ok "Generating manifest for filesystem..."
    sudo chroot new_building_os dpkg-query -W --showformat='${Package} ${Version}\n' | sudo tee image/LiveOS/filesystem.manifest >/dev/null 2>&1
    judge "Generate manifest for filesystem"
    judge "Generate manifest for filesystem-desktop"

    print_ok "Compressing the single root filesystem as /LiveOS/rootfs.squashfs..."
    sudo mksquashfs new_building_os image/LiveOS/rootfs.squashfs \
        -noappend -no-duplicates -no-recovery \
        -wildcards -b 1M \
        -comp zstd -Xcompression-level "${SQUASHFS_LEVEL:-19}" \
        -e "var/cache/apt/archives/*" \
        -e "tmp/*" \
        -e "tmp/.*" \
        -e "boot/synos-live-initrd.img" \
        -e "swapfile"
    judge "Compress rootfs"
    
    print_ok "Generating filesystem.size on /LiveOS/filesystem.size..."
    filesystem_size=$(sudo du -sx --block-size=1 new_building_os | cut -f1)
    printf '%s\n' "$filesystem_size" > image/LiveOS/filesystem.size
    judge "Generate filesystem.size"

    print_ok "Generating README.diskdefines..."
    cat << EOF > image/README.diskdefines
#define DISKNAME  Try $TARGET_BUSINESS_NAME
#define TYPE  binary
#define TYPEbinary  1
#define ARCH  $TARGET_ARCH
#define ARCH${TARGET_ARCH}  1
#define DISKNUM  1
#define DISKNUM1  1
#define TOTALNUM  0
#define TOTALNUM0  1
EOF
    judge "Generate README.diskdefines"

    DATE=$(TZ="UTC" date +"%y%m%d%H%M")
    cat << EOF > image/README.md
# $TARGET_BUSINESS_NAME $TARGET_BUILD_VERSION

$TARGET_BUSINESS_NAME is an enterprise Linux workstation for organisations moving away from proprietary desktops.

This image is built with the following configurations:

- **Version**: $TARGET_BUILD_VERSION
- **Date**: $DATE

$TARGET_BUSINESS_NAME is distributed under the GPLv3 license. You can find the license at [GPL-v3](https://www.gnu.org/licenses/gpl-3.0.html).

## Please verify the checksum!!!

To verify the integrity of the image, you can calculate the md5sum of the image and compare it with the value in the file \`md5sum.txt\`.

To do this, run the following command in the terminal:

\`\`\`bash
md5sum -c md5sum.txt | grep -v 'OK'
\`\`\`

No output indicates that the image is correct.

## How to use

Press F12 to enter the boot menu when you start your computer. Select the USB drive to boot from.

## More information

For detailed instructions, please visit the [$TARGET_BUSINESS_NAME documentation](https://docs.synos.example/install/system-requirements).
EOF

    pushd image
    print_ok "Creating EFI boot image on /isolinux/efiboot.img..."
    (
        cd isolinux
        dd if=/dev/zero of=efiboot.img bs=1M count=10
        mkfs.vfat efiboot.img

        if [ "$TARGET_ARCH" = arm64 ]; then
            target_root="$SCRIPT_DIR/new_building_os"
            arm64_shim="$target_root/usr/lib/shim/shimaa64.efi.signed.latest"
            arm64_grub="$target_root/usr/lib/grub/arm64-efi-signed/gcdaa64.efi.signed"
            arm64_mok="$target_root/usr/lib/shim/mmaa64.efi"

            # The signed Canonical config-delivery GRUB image already embeds
            # FAT, ISO9660, GPT, search and configfile support. Build the
            # removable-media ESP directly from the completed ARM64 target;
            # this avoids installing a foreign shim package that conflicts
            # with an amd64 build host's own bootloader.
            cat > arm64-grub.cfg <<EOF
search --no-floppy --label --set=synos_iso $TARGET_NAME
set prefix=(\$synos_iso)/boot/grub
configfile \$prefix/grub.cfg
EOF
            printf 'shimaa64.efi,%s,,This is the boot entry for %s\n' \
                "$TARGET_BUSINESS_NAME" "$TARGET_BUSINESS_NAME" \
                | iconv -f UTF-8 -t UTF-16LE > BOOTAA64.CSV

            mmd -i efiboot.img ::/EFI ::/EFI/BOOT
            mcopy -i efiboot.img "$arm64_shim" ::/EFI/BOOT/BOOTAA64.EFI
            mcopy -i efiboot.img "$arm64_grub" ::/EFI/BOOT/grubaa64.efi
            mcopy -i efiboot.img "$arm64_mok" ::/EFI/BOOT/mmaa64.efi
            mcopy -i efiboot.img BOOTAA64.CSV ::/EFI/BOOT/BOOTAA64.CSV
            mcopy -i efiboot.img arm64-grub.cfg ::/EFI/BOOT/grub.cfg
            rm -f BOOTAA64.CSV arm64-grub.cfg
        else
            mkdir efi boot
            sudo mount efiboot.img efi
            if ! sudo grub-install \
                --target="$GRUB_EFI_TARGET" \
                --efi-directory=efi \
                --boot-directory=boot \
                --uefi-secure-boot \
                --removable \
                --no-nvram; then
                sudo umount efi
                print_error "grub-install failed!"
                exit 1
            fi
            # Hand-off to the ISO's menu. Ubuntu's signed GRUB finds removable
            # media by itself; Debian's reads EFI/debian/grub.cfg on the ESP
            # and otherwise drops to a shell. Write the same stub wherever a
            # signed GRUB may look for it.
            cat > efi-handoff.cfg <<HANDOFF
search --no-floppy --label --set=synos_iso $TARGET_NAME
set prefix=(\$synos_iso)/boot/grub
configfile \$prefix/grub.cfg
HANDOFF
            for dir in EFI/BOOT EFI/debian EFI/ubuntu EFI/$BASE_ID; do
                sudo mkdir -p "efi/$dir"
                sudo cp efi-handoff.cfg "efi/$dir/grub.cfg"
            done
            rm -f efi-handoff.cfg
            sudo umount efi
            rm -rf efi
        fi
    )
    judge "Create EFI boot image"

    # Debian's signed GRUB sets its prefix to /EFI/debian on the ISO filesystem
    # itself (not the EFI boot image), so the hand-off stub lives there too.
    print_ok "Placing the GRUB hand-off stub on the ISO filesystem..."
    for dir in EFI/BOOT EFI/debian EFI/ubuntu "EFI/$BASE_ID"; do
        mkdir -p "$dir"
        cat > "$dir/grub.cfg" <<HANDOFF
search --no-floppy --label --set=synos_iso $TARGET_NAME
set prefix=(\$synos_iso)/boot/grub
configfile \$prefix/grub.cfg
HANDOFF
    done
    judge "Place GRUB hand-off stub on the ISO"

    # BIOS boot image — amd64-only.  ARM64 machines are pure UEFI.
    if [ "$TARGET_ARCH" = "amd64" ]; then
        print_ok "Creating BIOS boot image on /isolinux/bios.img..."
        grub-mkstandalone \
            --format=i386-pc \
            --output=isolinux/core.img \
            --install-modules="linux16 linux normal iso9660 biosdisk memdisk search tar ls font gfxterm all_video" \
            --modules="linux16 linux normal iso9660 biosdisk search font gfxterm all_video" \
            --locales="" \
            --fonts="" \
            "boot/grub/grub.cfg=isolinux/grub.cfg"
        judge "Create BIOS boot image"

        print_ok "Creating hybrid boot image on /isolinux/bios.img..."
        cat /usr/lib/grub/i386-pc/cdboot.img isolinux/core.img > isolinux/bios.img
        judge "Create hybrid boot image"
    fi

    print_ok "Creating .disk/info..."
    echo "$TARGET_BUSINESS_NAME $TARGET_BUILD_VERSION $BASE_ID-$TARGET_SUITE - Release $TARGET_ARCH ($(date +%Y%m%d))" | sudo tee .disk/info
    judge "Create .disk/info"

    print_ok "Creating md5sum.txt..."
    if [ "$TARGET_ARCH" = "amd64" ]; then
        sudo /bin/bash -c "(find . -type f -print0 | xargs -0 md5sum | grep -v -e 'md5sum.txt' -e 'bios.img' -e 'efiboot.img' > md5sum.txt)"
    else
        sudo /bin/bash -c "(find . -type f -print0 | xargs -0 md5sum | grep -v -e 'md5sum.txt' -e 'efiboot.img' > md5sum.txt)"
    fi
    judge "Create md5sum.txt"

    print_ok "Creating iso image on $SCRIPT_DIR/$TARGET_NAME.iso (arch: $TARGET_ARCH)..."
    if [ "$TARGET_ARCH" = "amd64" ]; then
        # amd64: hybrid ISO with BIOS (El Torito) + UEFI
        sudo xorriso \
            -as mkisofs \
            -r -J \
            -iso-level 3 \
            -full-iso9660-filenames \
            -volid "$TARGET_NAME" \
            -partition_offset 16 \
            -eltorito-boot boot/grub/bios.img \
                -no-emul-boot \
                -boot-load-size 4 \
                -boot-info-table \
                --eltorito-catalog boot/grub/boot.cat \
                --grub2-boot-info \
                --grub2-mbr /usr/lib/grub/i386-pc/boot_hybrid.img \
            -eltorito-alt-boot \
                -e EFI/efiboot.img \
                -no-emul-boot \
                -append_partition 2 0xef isolinux/efiboot.img \
            -output "$SCRIPT_DIR/$TARGET_NAME.iso" \
            -m "isolinux/efiboot.img" \
            -m "isolinux/bios.img" \
            -graft-points \
                "/EFI/efiboot.img=isolinux/efiboot.img" \
                "/boot/grub/grub.cfg=isolinux/grub.cfg" \
                "/boot/grub/bios.img=isolinux/bios.img" \
                "."
    else
        # arm64: UEFI-only ISO — no BIOS, no El Torito, no hybrid MBR
        sudo xorriso \
            -as mkisofs \
            -r -J \
            -iso-level 3 \
            -full-iso9660-filenames \
            -volid "$TARGET_NAME" \
            -partition_offset 16 \
            -e EFI/efiboot.img \
            -no-emul-boot \
            -append_partition 2 0xef isolinux/efiboot.img \
            -appended_part_as_gpt \
            -output "$SCRIPT_DIR/$TARGET_NAME.iso" \
            -m "isolinux/efiboot.img" \
            -graft-points \
                "/EFI/efiboot.img=isolinux/efiboot.img" \
                "/boot/grub/grub.cfg=isolinux/grub.cfg" \
                "."
    fi

    judge "Create iso image"

    print_ok "Embedding the Dracut rd.live.check media checksum..."
    sudo implantisomd5 --force "$SCRIPT_DIR/$TARGET_NAME.iso"
    judge "Embed ISO media checksum"

    print_ok "Moving iso image to $SCRIPT_DIR/dist/$TARGET_FILE_NAME-$TARGET_BUILD_VERSION-$BASE_ID-$TARGET_SUITE-$DATE-$TARGET_ARCH.iso..."
    mkdir -p "$SCRIPT_DIR/dist"
    mv "$SCRIPT_DIR/$TARGET_NAME.iso" "$SCRIPT_DIR/dist/$TARGET_FILE_NAME-$TARGET_BUILD_VERSION-$BASE_ID-$TARGET_SUITE-$DATE-$TARGET_ARCH.iso"
    judge "Move iso image"

    print_ok "Generating sha256 checksum..."
    write_iso_checksum \
        "$SCRIPT_DIR/dist/$TARGET_FILE_NAME-$TARGET_BUILD_VERSION-$BASE_ID-$TARGET_SUITE-$DATE-$TARGET_ARCH.iso" \
        "$SCRIPT_DIR/dist/$TARGET_FILE_NAME-$TARGET_BUILD_VERSION-$BASE_ID-$TARGET_SUITE-$DATE-$TARGET_ARCH.sha256"
    judge "Generate sha256 checksum"

    print_ok "Writing the package lock and CycloneDX SBOM..."
    python3 "$SCRIPT_DIR/tools/sbom.py" \
        --dpkg-manifest "$SCRIPT_DIR/image/LiveOS/filesystem.manifest" \
        --resolved "$SCRIPT_DIR/.build/resolved.json" \
        --iso "$SCRIPT_DIR/dist/$TARGET_FILE_NAME-$TARGET_BUILD_VERSION-$BASE_ID-$TARGET_SUITE-$DATE-$TARGET_ARCH.iso" \
        --stem "$SCRIPT_DIR/dist/$TARGET_FILE_NAME-$TARGET_BUILD_VERSION-$BASE_ID-$TARGET_SUITE-$DATE-$TARGET_ARCH"
    cp "$SCRIPT_DIR/.build/resolved.json" \
        "$SCRIPT_DIR/dist/$TARGET_FILE_NAME-$TARGET_BUILD_VERSION-$BASE_ID-$TARGET_SUITE-$DATE-$TARGET_ARCH.resolved.json"
    judge "Write package lock and SBOM"

    popd
}

function umount_on_exit() {
    sleep 2
    print_ok "Unmounting filesystems before exit..."
    sudo umount "$SCRIPT_DIR/new_building_os/sys" || sudo umount -lf "$SCRIPT_DIR/new_building_os/sys" || true
    sudo umount "$SCRIPT_DIR/new_building_os/proc" || sudo umount -lf "$SCRIPT_DIR/new_building_os/proc" || true
    sudo umount "$SCRIPT_DIR/new_building_os/var/cache/apt/archives" 2>/dev/null || true
    sudo umount "$SCRIPT_DIR/new_building_os/dev" || sudo umount -lf "$SCRIPT_DIR/new_building_os/dev" || true
    sudo umount "$SCRIPT_DIR/new_building_os/run" || sudo umount -lf "$SCRIPT_DIR/new_building_os/run" || true
    judge "Unmount filesystems before exit"
}

# =============   main  ================
cd "$SCRIPT_DIR"
bind_signal
clean
download_base_system
mount_folders
setup_apt
run_chroot
run_ansible_chroot
umount_folders
prepare_iso_directory
prepare_live_grub_font
build_iso
echo "$0 - Build completed."
