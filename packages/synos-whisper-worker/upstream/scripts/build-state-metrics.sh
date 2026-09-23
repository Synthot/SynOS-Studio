#!/bin/bash
# Private whisper.cpp library; distro GGML remains a separately updated backend.
set -euo pipefail
cd "$(dirname "$0")/.."
architecture="${1:?Expected amd64 or arm64}"
mkdir -p obj/state-metrics obj/downloads "obj/$architecture/synos-whisper"
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
fetch() {
    local relative="$1" digest="$2" destination="obj/state-metrics/${1##*/}"
    if ! test -f "$destination" || ! echo "$digest  $destination" | sha256sum --check --status 2>/dev/null; then
        fetch_url "$relative" "$destination.part" \
            "https://raw.githubusercontent.com/ggml-org/whisper.cpp/v1.8.3/$relative" \
            "https://github.com/ggml-org/whisper.cpp/raw/v1.8.3/$relative"
        echo "$digest  $destination.part" | sha256sum --check --status
        mv "$destination.part" "$destination"
    fi
}
fetch src/whisper.cpp d1e041704c12e20b8eb9596632db3b3b31046348dbb0e493f8a5ab8edd5f4376
fetch src/whisper-arch.h f35cf51d54c4df7e1c504ba8ef3f06012ba483459ddf2a5cba533deb7b85c239
fetch LICENSE e562a2ddfaf8280537795ac5ecd34e3012b6582a147ef69ba6a6a5c08c84757d

fetch_deb() {
    local relative="$1" digest="$2" destination="obj/downloads/${1##*/}"
    if ! test -f "$destination" || ! echo "$digest  $destination" | sha256sum --check --status 2>/dev/null; then
        # shellcheck disable=SC2046
        fetch_url "$relative" "$destination.part" $(ubuntu_urls "$relative")
        echo "$digest  $destination.part" | sha256sum --check --status
        mv "$destination.part" "$destination"
    fi
    dpkg-deb --extract "$destination" "$toolchain"
}
# The whole GCC 15 toolchain (driver, C++ frontend and its fixed includes) is
# pinned and fetched here rather than assumed from the host: the suite this
# recipe builds under (its own Containerfile's cargo/gcc are apt packages
# from that suite's archive) may still ship GCC 13. GCC 15 emits assembly
# (Ubuntu's package-metadata notes, a ".base64 ..." pseudo-op) that a suite's
# own binutils can be too old to assemble, so a matching binutils is pinned
# and fetched too; builds never apt-install anything else.
test "$(uname -m)" = x86_64 || { echo 'This cross-build recipe requires an amd64 build host' >&2; exit 1; }
toolchain="$PWD/obj/cxx-$architecture"
case "$architecture" in
    amd64)
        triple=x86_64-linux-gnu
        fetch_deb pool/main/g/gcc-15/gcc-15-x86-64-linux-gnu_15.2.0-16ubuntu1_amd64.deb \
            df0e5375c9e981b18e396c2e9ec1e359f226576a9b94a311bf07603f3ca6023a
        fetch_deb pool/main/g/gcc-15/g++-15-x86-64-linux-gnu_15.2.0-16ubuntu1_amd64.deb \
            8673a23519f570b4a5b4a663a9ce3b0e2f0be50c7250ce7fdae19eb064c3d6aa
        fetch_deb pool/main/g/gcc-15/libgcc-15-dev_15.2.0-16ubuntu1_amd64.deb \
            32fa5bde1891f46fd4f422e466c863fb78e834b634e1aa08211c8eea68870af5
        fetch_deb pool/main/g/gcc-15/libstdc++-15-dev_15.2.0-16ubuntu1_amd64.deb \
            9a400597b1db45cf51238c9eb556e211ec092d363cfb7ebebac627750ba56433
        fetch_deb pool/universe/g/ggml/libggml0_0.9.11-1_amd64.deb \
            871c666330f0597aff3a23eb60e1f29cb8aa6ea70c35f0683f30c7405cf82c9f
        fetch_deb pool/main/b/binutils/binutils-x86-64-linux-gnu_2.44-3ubuntu1.3_amd64.deb \
            bab4b7f76496ff8a83ed2552a97aa996b77f38f5f2cc8e802d7b32fbf0c484ad
        fetch_deb pool/main/b/binutils/libbinutils_2.44-3ubuntu1.3_amd64.deb \
            20c0f5fc6696ae015897a58d6fcc587857892a1470af5f9a8145a25279119062
        fetch_deb pool/main/b/binutils/libsframe1_2.44-3ubuntu1.3_amd64.deb \
            6d5646fba6f601fc8e055cf366b24a9e5a7da7dddcc223bdfbeeaa0a2cfceb71
        driver="$toolchain/usr/bin/$triple-gcc-15"
        frontend="$toolchain/usr/libexec/gcc/$triple/15/"
        includes=("$toolchain/usr/include/c++/15" "$toolchain/usr/include/$triple/c++/15" "$toolchain/usr/include/c++/15/backward")
        as_dir="$toolchain/usr/bin"
        as_libdir="$toolchain/usr/lib/$triple"
        ;;
    arm64)
        triple=aarch64-linux-gnu
        fetch_deb pool/main/g/gcc-15-cross/gcc-15-aarch64-linux-gnu_15.2.0-16ubuntu1cross1_amd64.deb \
            72bec95928dcd2a954ec9f5aa4a35fad9b43a0798e1dc2d515f9c864c9908d89
        fetch_deb pool/main/g/gcc-15-cross/g++-15-aarch64-linux-gnu_15.2.0-16ubuntu1cross1_amd64.deb \
            988221ff508e6150282f535b404ebeeb2601875daf1acf5814bc66d3ff66e10e
        fetch_deb pool/main/g/gcc-15-cross/libgcc-15-dev-arm64-cross_15.2.0-16ubuntu1cross1_all.deb \
            b5064dd69c4ca98f319084186530265f0cfbf4b97dabcebab2bb003928562dbf
        fetch_deb pool/main/g/gcc-15-cross/libstdc++-15-dev-arm64-cross_15.2.0-16ubuntu1cross1_all.deb \
            a88bc813f2bb86ca21b57018c405510bd3cc3d93202ae16d44561b619e16d66d
        fetch_deb pool/universe/g/ggml/libggml0_0.9.11-1_arm64.deb \
            55dbd75060b34c8926f3e5e1d52351ad6ec295cab0a30f269748a664609b13e8
        fetch_deb pool/main/b/binutils/binutils-aarch64-linux-gnu_2.44-3ubuntu1.3_amd64.deb \
            7a9ddd65038b1762a7ee01182a8957aafc844fdce30d85535d0dd233d9be01fd
        fetch_deb pool/main/b/binutils/libsframe1_2.44-3ubuntu1.3_amd64.deb \
            6d5646fba6f601fc8e055cf366b24a9e5a7da7dddcc223bdfbeeaa0a2cfceb71
        driver="$toolchain/usr/bin/$triple-gcc-15"
        frontend="$toolchain/usr/libexec/gcc-cross/$triple/15/"
        includes=("$toolchain/usr/$triple/include/c++/15" "$toolchain/usr/$triple/include/c++/15/$triple" "$toolchain/usr/$triple/include/c++/15/backward")
        as_dir="$toolchain/usr/bin"
        as_libdir="$toolchain/usr/lib/x86_64-linux-gnu"   # the cross as/ld run on the amd64 build host
        ;;
    *) echo 'Expected amd64 or arm64' >&2; exit 2 ;;
esac
test -x "$driver" || { echo "pinned GCC 15 driver missing at $driver" >&2; exit 1; }
test "$("$driver" -dumpversion)" = 15 || { echo 'pinned GCC 15 driver reports an unexpected version' >&2; exit 1; }
test -x "$as_dir/$triple-as" || { echo "pinned binutils assembler missing at $as_dir/$triple-as" >&2; exit 1; }
compiler=("$driver" -x c++ -B "$frontend")
for include in "${includes[@]}"; do compiler+=(-isystem "$include"); done
# GCC's own -B search does not override the assembler it was configured
# with (Ubuntu's gcc-15 hardcodes /usr/bin/$triple-as): PATH is the only
# reliable way to make it pick up the pinned, newer binutils instead of
# whatever the base image's suite happens to carry.
PATH="$as_dir:$PATH" LD_LIBRARY_PATH="$as_libdir${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
"${compiler[@]}" -std=c++11 -O2 -fPIC -shared -pthread \
    -fstack-protector-strong -D_FORTIFY_SOURCE=2 \
    -DWHISPER_VERSION='"1.8.3"' -DWHISPER_SHARED -DWHISPER_BUILD \
    -I obj/state-metrics -I obj/headers/usr/include src/state-metrics.cpp \
    -L "$toolchain/usr/lib/$triple" -Wl,-z,defs,-z,relro,-z,now,-soname,libsynos-whisper.so.1 \
    -l:libggml.so.0 -l:libggml-base.so.0 -l:libstdc++.so.6 -lm \
    -o "obj/$architecture/synos-whisper/libsynos-whisper.so.1"
