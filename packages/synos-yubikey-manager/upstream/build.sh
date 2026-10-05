#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/../lib/build-guards.sh"
ARCH="${1:-amd64}"

need_cmd msgfmt
bash "$SCRIPT_DIR/compile-locales.sh"

mkdir -p obj
if [ "$ARCH" = "arm64" ]; then
    need_cmd cargo
    need_cmd aarch64-linux-gnu-gcc
    # The cross target is installed here, not declared in
    # rust-toolchain.toml: declaring it there makes every build on every
    # architecture fetch its rust-std before anything checks whether the
    # build needs it, and two recipes doing that at once raced over one
    # file in $HOME/.rustup. need_cmd first, so a host without rustup says
    # which tool to install rather than "command not found" mid-build.
    need_cmd rustup
    rustup target add aarch64-unknown-linux-gnu
    export PKG_CONFIG_ALLOW_CROSS=1
    export PKG_CONFIG_PATH=/usr/lib/aarch64-linux-gnu/pkgconfig:/usr/share/pkgconfig
    export CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_LINKER=aarch64-linux-gnu-gcc
    cargo build --release --target aarch64-unknown-linux-gnu
    cp target/aarch64-unknown-linux-gnu/release/synos-yubikey-manager obj/
else
    need_cmd cargo
    cargo build --release
    cp target/release/synos-yubikey-manager obj/
fi
