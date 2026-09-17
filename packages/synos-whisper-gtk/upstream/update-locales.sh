#!/usr/bin/env bash
# Extract GTK and Shell-extension messages and merge them into catalogs.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
pot="$root/po/synos-whisper-gtk.pot"

xgettext --language=Python --keyword=_ --from-code=UTF-8 \
    --package-name=synos-whisper-gtk --output="$pot" \
    "$root"/src/synos_whisper_gtk/*.py
xgettext --join-existing --language=JavaScript --keyword=_ --from-code=UTF-8 \
    --output="$pot" "$root/data/voice-typing@synos.com/extension.js"

shopt -s nullglob
for catalog in "$root"/po/*.po; do
    msgmerge --quiet --update --backup=none "$catalog" "$pot"
done
