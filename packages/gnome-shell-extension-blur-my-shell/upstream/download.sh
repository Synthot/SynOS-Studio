#!/usr/bin/env bash
# Pre-build: downloads extension for each supported suite/GNOME version.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../lib/gnome-versions.sh"

UUID="blur-my-shell@aunetx"

for SUITE in "${!GNOME_TARGETS[@]}"; do
    TARGET=${GNOME_TARGETS[$SUITE]}
    DEPLOY_DIR="deploy/$SUITE/$UUID"

    rm -rf "$DEPLOY_DIR"
    mkdir -p "$DEPLOY_DIR"

    echo "[$SUITE] Resolving $UUID for GNOME $TARGET..."
    python3 "$SCRIPT_DIR/../lib/resolve-gnome-ext.py" "$UUID" --target "$TARGET" --download --out "$DEPLOY_DIR"

    # Dash to Panel panel blur geometry fix (from a maintained fork of the
    # extension). Upstream merged its own version of it (release 74 sizes
    # the blur from a geometry_actor, as the patch does), and the patch no
    # longer applies to that code -- so it is only applied to a release
    # that does not have the fix yet, where it must apply cleanly.
    if grep -q 'geometry_actor' "$DEPLOY_DIR/components/panel.js"; then
        echo "[$SUITE] Dash to Panel panel blur geometry fix: already in this release, not patching."
    else
        echo "[$SUITE] Applying Dash to Panel panel blur geometry fix..."
        patch -d "$DEPLOY_DIR" -p1 --forward < "$SCRIPT_DIR/fix-dtp-panel-blur.patch"
    fi
done

echo "Done."

# Pre-compile GSettings schemas at build time so postinst is unnecessary
for suite_dir in deploy/*/; do
    schema_dir="${suite_dir}blur-my-shell@aunetx/schemas"
    [ -d "$schema_dir" ] && glib-compile-schemas "$schema_dir" || true
done
