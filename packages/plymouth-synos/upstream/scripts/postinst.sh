#!/bin/sh
set -e

if [ "$1" = "configure" ]; then
    # 1. Register and set graphical splash theme
    update-alternatives --install \
        /usr/share/plymouth/themes/default.plymouth \
        default.plymouth \
        /usr/share/plymouth/themes/synos/synos.plymouth \
        150
    update-alternatives --set \
        default.plymouth \
        /usr/share/plymouth/themes/synos/synos.plymouth || true

    # 2. Register and set text fallback theme
    update-alternatives --install \
        /usr/share/plymouth/themes/text.plymouth \
        text.plymouth \
        /usr/share/plymouth/themes/synos-text/synos-text.plymouth \
        150
    update-alternatives --set \
        text.plymouth \
        /usr/share/plymouth/themes/synos-text/synos-text.plymouth || true

    # 3. Rebuild all images through SynOS's staged writer. Never report a
    # successful package transaction after silently losing the boot splash or
    # producing an unverified initrd.
    if [ -x /usr/libexec/synos-dracut-verify ]; then
        /usr/libexec/synos-dracut-verify --rebuild
    else
        dracut --force --regenerate-all
    fi
fi
