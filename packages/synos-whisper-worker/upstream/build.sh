#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
case "${1:-}" in
    amd64) compiler=gcc ;;
    arm64) compiler=aarch64-linux-gnu-gcc ;;
    *) echo 'Expected amd64 or arm64' >&2; exit 2 ;;
esac
command -v "$compiler" >/dev/null
mkdir -p obj/headers obj/downloads "obj/$1"
# Only compile-time C declarations are taken from these packages. No foreign
# architecture libraries or binaries are executed or shipped. Pin both inputs
# so host distribution upgrades cannot silently change the worker ABI.
# Pinned inputs are fetched from the first mirror that serves them; the digest
# check below makes the source irrelevant to the result. SYNOS_UBUNTU_MIRROR
# (a base URL ending in /ubuntu) goes first when set.
fetch_url() {  # fetch_url <relative path> <destination.part> <url>...
    local relative="$1" destination="$2" url; shift 2
    for url in "$@"; do
        curl --fail --location --silent --show-error --retry 2 --connect-timeout 20 --max-time 180 \
            "$url" -o "$destination" && return 0
        echo "  $url: not available, trying the next mirror" >&2
    done
    echo "none of the mirrors served $relative" >&2
    return 22
}
ubuntu_urls() {  # ubuntu_urls <pool path> -> one URL per line
    local relative="$1"
    [ -n "${SYNOS_UBUNTU_MIRROR:-}" ] && echo "${SYNOS_UBUNTU_MIRROR%/}/$relative"
    echo "https://archive.ubuntu.com/ubuntu/$relative"
    echo "https://ports.ubuntu.com/ubuntu-ports/$relative"
    echo "https://mirror.aiursoft.com/ubuntu/$relative"
}
fetch_headers() {
    local path="$1" digest="$2" destination="obj/downloads/${1##*/}"
    if ! echo "$digest  $destination" | sha256sum --check --status 2>/dev/null; then
        # shellcheck disable=SC2046
        fetch_url "$path" "$destination.part" $(ubuntu_urls "$path")
        echo "$digest  $destination.part" | sha256sum --check --status
        mv "$destination.part" "$destination"
    fi
    dpkg-deb --extract "$destination" obj/headers
}
fetch_headers 'pool/universe/w/whisper.cpp/libwhisper-dev_1.8.3+dfsg-2_amd64.deb' \
    '9e45250f34bc58a418efce6b5458dd828879387707133dbef3a5a2866629dd40'
fetch_headers 'pool/universe/g/ggml/libggml-dev_0.9.11-1_amd64.deb' \
    '68d92c54a94794b4867f992a182aa7f860b4a6196d3ef76c44b380b5b3973581'
"$compiler" -std=gnu11 -O2 -Wall -Wextra -Werror -fstack-protector-strong \
    -D_FORTIFY_SOURCE=2 -Wl,-z,relro,-z,now -I obj/headers/usr/include \
    src/worker.c -ldl -o "obj/$1/synos-whisper-worker"
bash scripts/build-state-metrics.sh "$1"
