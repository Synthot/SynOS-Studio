#!/bin/sh
set -e
if [ "$1" = "configure" ]; then
    flatpak remote-add --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo || \
        echo "Warning: Failed to add Flathub remote. You may need to add it manually." >&2
fi
#DEBHELPER#
exit 0
