#!/bin/sh
# Builds this configuration bundle into an installable image.
#
#   ./build.sh            build; the ISO and its evidence land in ./dist
#   ./build.sh check      only check that this machine can build (installs nothing)
#   ./build.sh --yes      answer yes to the questions (install the container runtime)
#
# Works on any Linux distribution (Debian, Ubuntu, Fedora, openSUSE, Arch and
# their derivatives), on macOS (build.command, or this script from Terminal)
# and on Windows through WSL 2. It needs a container runtime, podman or
# docker, and offers to install podman when neither is present (on macOS
# through Homebrew, with a Linux virtual machine sized for the build).
# Nothing else is installed: the SynOS build engine runs inside a container
# image published for the exact engine version this bundle was made for.
# 40 GB free are needed for the first build.
#
# Environment: SYNOS_BUILDER_IMAGE (use another image), SYNOS_IMAGE_REPOSITORY
# (another registry, default ghcr.io/synthot/synos-builder), SYNOS_YES=1
# (same as --yes).
set -eu
cd "$(dirname "$0")"

yes_to_all=${SYNOS_YES:-}
command_word=""
for arg in "$@"; do
    case "$arg" in
        --yes|-y) yes_to_all=1 ;;
        check|build) command_word=$arg ;;
        -h|--help) sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) printf 'unknown argument: %s\n' "$arg" >&2; exit 1 ;;
    esac
done

say() { printf '%s\n' "$*"; }
fail() { printf 'error: %s\n' "$1" >&2; exit "${2:-1}"; }
ask() {  # ask "question" -> returns 0 for yes
    [ -n "$yes_to_all" ] && return 0
    [ -t 0 ] || return 1
    printf '%s [y/N] ' "$1"
    read -r answer
    case "$answer" in y|Y|yes|YES|Yes) return 0 ;; *) return 1 ;; esac
}
field() { sed -n "s/^$2:[[:space:]]*\"\{0,1\}\([^\"#]*\)\"\{0,1\}.*/\1/p" "$1" | head -n 1 | sed 's/[[:space:]]*$//'; }

# ---------------------------------------------------------------- the bundle
[ -f bundle.json ] || fail "bundle.json is missing: run this script from the unzipped bundle folder"
manifest=$(sed -n 's/.*"manifest"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' bundle.json | head -n 1)
[ -n "$manifest" ] && [ -f "$manifest" ] || fail "bundle.json names no manifest, or $manifest is missing"
engine=$(sed -n 's/.*"min"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' bundle.json | head -n 1)
base=$(field "$manifest" base)
suite=$(field "$manifest" suite)
arch=$(field "$manifest" arch)
[ -n "$arch" ] || arch=amd64
[ -n "$base" ] && [ -n "$suite" ] || fail "$manifest does not name a base and a suite"

# ---------------------------------------------------------------- the machine
uname_s=$(uname -s)
host_arch=$(uname -m)
case "$host_arch" in x86_64|amd64) host_arch=amd64 ;; aarch64|arm64) host_arch=arm64 ;; esac
macos=""
case "$uname_s" in
    Linux) ;;
    Darwin)
        macos=1
        # Homebrew's bin is not always on the PATH of a Terminal opened by Finder.
        PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"; export PATH
        say "macOS: the build runs inside a Linux virtual machine (podman machine or Docker Desktop)."
        case "$PWD" in "$HOME"/*|/Users/*) ;; *) say "note: keep the bundle under your home folder; podman machine and Docker Desktop share only /Users by default." ;; esac
        ;;
    *) fail "the image build needs a Linux kernel; this is $uname_s. On Windows, use build.cmd (Docker Desktop with WSL 2)." 2 ;;
esac
if grep -qi microsoft /proc/version 2>/dev/null; then
    say "WSL 2 detected: fine. Docker Desktop's WSL integration or podman inside this distribution both work."
fi
if [ "$host_arch" != "$arch" ]; then
    say "this bundle targets $arch and this machine is $host_arch: the build runs under CPU emulation and takes several times longer."
    [ -n "$macos" ] && say "  Docker Desktop: turn on 'Use Rosetta for x86_64/amd64 emulation' in Settings > General. A native build needs the bundle's architecture set to $host_arch (Advanced settings > Build in Studio)."
fi

install_podman() {
    say "No container runtime found. The build runs inside a container, so podman (or docker) is required."
    if command -v apt-get >/dev/null 2>&1; then cmd="sudo apt-get update && sudo apt-get install -y podman"
    elif command -v dnf >/dev/null 2>&1; then cmd="sudo dnf install -y podman"
    elif command -v zypper >/dev/null 2>&1; then cmd="sudo zypper install -y podman"
    elif command -v pacman >/dev/null 2>&1; then cmd="sudo pacman -S --noconfirm podman"
    elif command -v apk >/dev/null 2>&1; then cmd="sudo apk add podman"
    elif [ -n "$macos" ] && command -v brew >/dev/null 2>&1; then cmd="brew install podman"
    elif [ -n "$macos" ]; then
        fail "install Docker Desktop (https://www.docker.com/products/docker-desktop/) or Homebrew (https://brew.sh) then podman, and run this script again" 2
    else
        fail "install podman or docker with your package manager, then run this script again (https://podman.io/docs/installation)" 2
    fi
    say "This will run: $cmd"
    ask "Install podman now?" || fail "install podman or docker, then run this script again: $cmd" 2
    sh -c "$cmd" || fail "the installation failed; install podman or docker by hand, then run this script again" 2
}

runtime=$(command -v podman || command -v docker || true)
if [ -z "$runtime" ]; then
    [ "$command_word" = check ] && fail "podman or docker is required; ./build.sh installs podman for you (Linux)" 2
    install_podman
    runtime=$(command -v podman || command -v docker || true)
    [ -n "$runtime" ] || fail "podman is still not on PATH; open a new terminal and run this script again" 2
fi

# The build mounts filesystems, runs debootstrap and loop-mounts an EFI image:
# it needs a root container runtime. On Linux, rootless podman and a docker
# socket the user cannot reach are used through sudo. On macOS the runtime
# talks to a Linux virtual machine: podman machine is created rootful and
# sized for the build; Docker Desktop must be running with enough resources.
run_as=""
if [ -n "$macos" ]; then
    case "$runtime" in
        *podman)
            if ! "$runtime" machine inspect >/dev/null 2>&1; then
                say "creating the Linux virtual machine for podman (4 CPUs, 8 GB memory, 80 GB disk, rootful)"
                ask "Create it now?" || fail "run: podman machine init --rootful --cpus 4 --memory 8192 --disk-size 80 && podman machine start" 2
                "$runtime" machine init --rootful --cpus 4 --memory 8192 --disk-size 80 || fail "podman machine init failed" 2
            fi
            "$runtime" machine inspect --format '{{.Rootful}}' 2>/dev/null | grep -q true \
                || fail "the podman machine is rootless; the build needs root in the VM: podman machine stop && podman machine set --rootful && podman machine start" 2
            "$runtime" machine inspect --format '{{.State}}' 2>/dev/null | grep -q running || "$runtime" machine start || fail "podman machine start failed" 2
            ;;
        *docker)
            "$runtime" info >/dev/null 2>&1 || fail "Docker Desktop is installed but not running: start it, wait for 'Engine running', then run this script again" 2
            mem_gb=$("$runtime" info --format '{{.MemTotal}}' 2>/dev/null | awk '{printf "%d", $1/1073741824}')
            [ -z "$mem_gb" ] || [ "$mem_gb" -ge 6 ] || say "Docker Desktop has ${mem_gb} GB of memory; give it 8 GB and a disk image of 80 GB or more in Settings > Resources."
            ;;
    esac
else
    case "$runtime" in
        *podman) [ "$(id -u)" -eq 0 ] || run_as="sudo" ;;
        *docker) "$runtime" info >/dev/null 2>&1 || run_as="sudo" ;;
    esac
    if [ -n "$run_as" ]; then
        say "The build needs root inside the container runtime; $runtime will run through sudo."
        sudo -v || fail "sudo is required to run $runtime with root privileges" 2
    fi
fi

free_kb=$(df -Pk . | awk 'NR==2 {print $4}')
[ "$free_kb" -ge 41943040 ] || fail "at least 40 GB free is needed in $(pwd); $((free_kb / 1048576)) GB available" 2

# ---------------------------------------------------------------- the image
repository=${SYNOS_IMAGE_REPOSITORY:-ghcr.io/synthot/synos-builder}
image=${SYNOS_BUILDER_IMAGE:-}
if [ -z "$image" ]; then
    pinned="$repository:$base-$suite${engine:+-v$engine}"
    moving="$repository:$base-$suite"
    say "pulling the build engine $pinned (one-time download, about 1.5 GB)"
    if $run_as "$runtime" pull --platform "linux/$arch" "$pinned" >/dev/null 2>&1; then
        image=$pinned
    else
        say "no image pinned to engine ${engine:-?}; using the current $moving"
        $run_as "$runtime" pull --platform "linux/$arch" "$moving" >/dev/null \
            || fail "cannot pull $moving for linux/$arch: check the network, or set SYNOS_BUILDER_IMAGE" 2
        image=$moving
    fi
fi

if [ "$command_word" = check ]; then
    say "ready: $run_as $runtime, $((free_kb / 1048576)) GB free, engine image $image"
    exit 0
fi

# ---------------------------------------------------------------- the build
mkdir -p dist
say "building $manifest with $image"
say "first build about 40 minutes; the cache volume synos-cache-$base-$suite makes the next ones shorter."
say "the full output is kept in dist/build.log"
set +e
$run_as "$runtime" run --rm --privileged --platform "linux/$arch" \
    -v "$PWD:/bundle:z" \
    -v "synos-cache-$base-$suite:/opt/synos/.build" \
    -v /opt/synos/new_building_os -v /opt/synos/image \
    -e SYNOS_KEYS_DIR=.build/keys \
    -e "SYNOS_UID=$(id -u)" -e "SYNOS_GID=$(id -g)" \
    -e SYNOS_SIGNING_KEY -e SYNOS_SIGNING_KEY_FILE \
    "$image" synos build /bundle --output /bundle/dist --log /bundle/dist/build.log
status=$?
set -e
if [ "$status" -ne 0 ]; then
    say ""
    say "the build did not finish (exit code $status). The complete output is in dist/build.log;"
    say "search it for the first 'FAILED' or 'error:' line. Running ./build.sh again resumes from the cache."
    exit "$status"
fi

iso=$(ls -t dist/*.iso 2>/dev/null | head -n 1 || true)
say ""
say "done. Your image: ${iso:-dist/}"
say "next to it: .sha256 (checksum), .packages.lock, .sbom.cdx.json (what is inside), .resolved.json, build.log"
say ""
say "To install it:"
if [ -n "$macos" ]; then
    say "  USB stick:  write the ISO with balenaEtcher (https://etcher.balena.io)."
    say "  Virtual machine: UTM (https://mac.getutm.app) or VMware Fusion, 4 GB RAM, 40 GB disk, UEFI on;"
    say "              an amd64 image is emulated on Apple Silicon, an arm64 image runs natively."
else
    say "  USB stick:  write the ISO with balenaEtcher, Fedora Media Writer or"
    say "              sudo dd if='$iso' of=/dev/sdX bs=4M status=progress oflag=sync"
    say "  Virtual machine: GNOME Boxes, virt-manager or VirtualBox, 4 GB RAM, 40 GB disk, UEFI on."
fi
say "  Boot it, try the live desktop, then run the installer from the menu."
