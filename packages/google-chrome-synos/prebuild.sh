#!/bin/bash
# Runs inside upstream/ at build time (tools/build_packages.py), after
# fork.json has fetched and unpacked the real google-chrome-stable .deb into
# obj/$ARCH; ARCH is set. suppress_scripts (fork.json) already drops the
# upstream postinst/prerm/postrm, which is where a real Chrome install would
# normally register /etc/cron.daily/google-chrome — but that cron entry is
# also shipped as a *payload* file (a symlink to
# /opt/google/chrome/cron/google-chrome, dpkg-deb -x'd regardless of
# suppress_scripts), so it survives unless removed here too. Left in place
# it would run daily and try to repair/re-add Chrome's own apt source
# (/etc/apt/sources.list.d/google-chrome.list) — exactly the unreviewed,
# phone-home apt source this fork exists to avoid. Nothing else in the
# payload self-activates (no systemd unit, no dbus service, no autostart
# entry, no polkit rule; the .desktop files and the apparmor profile under
# /opt/google/chrome/apparmor.d/ are inert without the postinst that would
# register them).
set -euo pipefail
cd "$(dirname "$0")/upstream"
rm -rf "obj/$ARCH/etc"
