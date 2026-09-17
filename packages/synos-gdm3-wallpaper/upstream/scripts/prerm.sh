if [ "$1" = "remove" ] || [ "$1" = "deconfigure" ]; then
    OUT_DIR="/var/lib/synos-gdm3-wallpaper"
    OUT_FILE="${OUT_DIR}/synos-theme.gresource"

    # 1. Remove the alternative
    update-alternatives --remove gdm-theme.gresource "$OUT_FILE" || true

    # 2. Clean up dynamically generated files
    rm -rf "$OUT_DIR" || true
fi
