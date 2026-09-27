#!/usr/bin/env bash
# packaging/install-test-engine.sh — bootstrap a brand-new machine, or bring
# an already-bootstrapped one up to date, for the catalog conformance test
# service (packaging/catalog-conformance/, driven by
# tools/catalog_conformance.py) and, optionally, its private monitoring
# console behind an authenticated nginx site: checks the machine, installs
# whatever it is missing (including cloning or fetching the engine checkout
# itself), points podman's storage at a disk with room for a build, writes
# /etc/synos/conformance.yml from answers instead of handing over a
# template to edit, installs the systemd units with a dedicated user, and
# then proves the result actually works.
#
# Safe to run again and again, on purpose: create what is missing, update
# what is already there, never destroy anything. An existing checkout is
# fetched and fast-forwarded, never reset or force-checked-out, and is
# refused outright — with the reason — if it has uncommitted changes, a
# different remote, or an unexpected branch, rather than silently forced
# into shape. An existing configuration that already matches these answers
# is left alone; one that differs is reported and left alone too, even
# with -y/--yes, unless --overwrite-config says otherwise — a config that
# may hold real, hand-filled-in credentials is the one thing this script
# must never clobber on a second run. Every run ends with a summary of
# what it created, updated, left alone, and skipped, plus whatever the
# operator still has to do by hand — --dry-run shows all of it and touches
# nothing.
#
# The REQUIREMENTS list below (git, podman, QEMU, xorriso, tesseract,
# Pillow, PyYAML and a headless browser) is also what profiles/bundles.yml's
# "test-engine" group ships into a SynOS image itself, for building a
# machine that runs this service instead of installing it onto one that
# already exists (docs/BUILD_MATRIX.md, "Installing it as a service"; the
# bundle-catalog's "Test Engine Build Machine" entry pairs it with the
# Yocto build host).
#
# Usage:
#   packaging/install-test-engine.sh [options]
#
# Options (the test engine):
#   --engine-root=PATH       the checkout this service runs against.
#                             Cloned from --engine-repo at --engine-ref
#                             when this path does not exist yet (or exists
#                             empty); fetched and fast-forwarded in place
#                             (never reset, never force-checked-out) when
#                             it does. Default: autodetected when this
#                             script still lives under packaging/ of a
#                             checkout (then that same checkout is what
#                             gets updated); otherwise required.
#   --engine-repo=URL        where to clone --engine-root from, when it
#                             does not exist yet (default:
#                             https://github.com/Synthot/SynOS-Studio)
#   --engine-ref=REF          branch or tag to check out and keep it on
#                             (default: main; pin a tag for a real
#                             deployment, leave main for a test rig).
#                             Report shows the commit before and after.
#   --site-url=URL           the real Studio page "build" mode drives
#   --catalog-url=URL        the Studio site's exported catalog JSON
#                             (default: SITE_URL/data/catalog.json)
#   --storage-path=PATH      where podman keeps images/layers/the chroot;
#                             default: the largest filesystem with enough
#                             room, chosen and shown, below
#   --workdir=PATH           the service's own working directory; default:
#                             a "synos-conformance" directory on the same
#                             disk as --storage-path (never the system disk
#                             by default)
#   --entries-per-run=N      max_builds_per_run (default: 5)
#   --jobs=N|auto            worker count (default: derived from this
#                             machine's own disk/memory/CPU numbers)
#   --config=PATH            where to write the configuration (default:
#                             /etc/synos/conformance.yml)
#   --dev-site-root=PATH     a local development copy of the Studio page's
#                             own document root, already deployed with the
#                             Studio repository's own deploy.sh (this
#                             script never deploys it); when given, the
#                             written config's upload: list gains a
#                             `protocol: local` rehearsal destination
#                             copying build-status.json there, and a
#                             commented-out "production" destination
#                             template to fill in by hand (never a real
#                             credential written by this installer). Point
#                             --site-url at this same development site for
#                             the rehearsal's fetch-back check to mean
#                             anything.
#   --dev-site-owner=USER[:GROUP]  chown that copy to this after writing it
#                             (optional; matches whatever user nginx runs
#                             as, e.g. www-data)
#   --dev-site-mode=MODE     chmod that copy to this after writing it
#                             (optional, e.g. 0644)
#   --overwrite-config       replace an existing --config/--monitoring-config
#                             file that differs from what these answers
#                             would produce; without it, a differing file is
#                             reported and left exactly as it is, even with
#                             -y/--yes
#   --service-user=NAME      dedicated system user (default: synos-conformance)
#   --unit-dir=PATH          where systemd unit files go (default:
#                             /etc/systemd/system)
#   --skip-packages          do not detect a distribution family or install
#                             anything; use when packages were installed by
#                             hand on a distribution this script does not
#                             recognize
#
# Options (the monitoring app — a private, separate repository; entirely
# optional, and the test engine above behaves identically without any of
# these given at all):
#   --monitoring-app=PATH    a checkout of the monitoring app: an existing
#                             one to use as-is, or, together with
#                             --monitoring-repo, where to put/keep one
#                             cloned from it. Neither this nor
#                             --monitoring-repo given at all: nothing
#                             monitoring-related happens, silently.
#   --monitoring-repo=URL    clone/fetch --monitoring-app from here (the
#                             private repository has no anonymous access,
#                             so this needs --monitoring-ssh-key too)
#   --monitoring-ssh-key=PATH  the SSH private key for that clone/fetch;
#                             used only for that one git subprocess
#                             (GIT_SSH_COMMAND), never written anywhere,
#                             never logged, never prompted for
#   --monitoring-ref=REF     branch or tag to keep --monitoring-app on
#                             (default: main)
#   --monitoring-config=PATH   where to write the app's own configuration
#                             (default: /etc/synos-forge/forge.toml)
#   --monitoring-service-user=NAME   dedicated system user (default:
#                             synos-forge)
#   --monitoring-port=N      the app's own loopback bind port (default:
#                             8420); it never binds anywhere but 127.0.0.1
#   --monitoring-nginx-port=N   nginx's own listen port for the additive
#                             site this adds in front of it (default: 8421)
#   --monitoring-nginx-location=PATH   the location inside that site's own,
#                             dedicated server block (default: /)
#   --monitoring-allow-from=CIDR   let clients in this address range reach
#                             the site; without it, the site is loopback-
#                             only (127.0.0.1), same as the app itself
#   --monitoring-allow-insecure-http   accept serving the site over plain
#                             HTTP to something other than loopback;
#                             refused without this (or a certificate)
#                             because the app has no authentication of its
#                             own to fall back on
#   --monitoring-tls-cert=PATH / --monitoring-tls-key=PATH   serve the site
#                             over TLS with this certificate/key instead
#   --monitor-auth-file=PATH   an existing htpasswd file for the site's
#                             HTTP basic auth; without it, one is generated
#                             and its plaintext credential is printed once,
#                             to this terminal, and nowhere else — never
#                             into a file, a log, or the systemd unit
#   --monitor-auth-user=NAME   the user named in a generated htpasswd file
#                             (default: admin)
#
# Options (either):
#   -y, --yes                answer yes to every question this script asks
#   --dry-run                print every action this script would take;
#                             perform none of them
#   -h, --help                this text
#
# This script never deploys the Studio page itself (development or
# production) — that stays the Studio repository's own build.py/deploy.sh,
# named again in this script's own closing summary so the two jobs are
# never confused.
#
# Bash, not POSIX sh: uses arrays, [[ ]]-free but bash-only local/printf -v
# and process substitution avoided in favor of portable-within-bash
# constructs. Every path handed to a command is passed as its own argument,
# never interpolated into a string a shell then re-parses.
set -u

# ------------------------------------------------------------------ output
say()  { printf '%s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
fail() {  # message [exit_code]
    printf 'error: %s\n' "$1" >&2
    exit "${2:-1}"
}

ASSUME_YES=${ASSUME_YES:-0}
DRY_RUN=${DRY_RUN:-0}

ask_yes() {  # prompt
    [ "$ASSUME_YES" = "1" ] && return 0
    if [ ! -t 0 ]; then
        return 1
    fi
    local reply
    read -r -p "$1 [y/N] " reply
    case "$reply" in
        y|Y|yes|YES) return 0 ;;
        *) return 1 ;;
    esac
}

# Runs $2.. for real, or just announces $1 and does nothing, depending on
# DRY_RUN — the one seam every mutating action in this script goes through,
# so --dry-run is "prints every action without performing it" by construction
# rather than by remembering to check DRY_RUN at every call site.
maybe() {  # description command [args...]
    local description=$1
    shift
    if [ "$DRY_RUN" = "1" ]; then
        say "[dry-run] would $description"
        return 0
    fi
    "$@"
}

PYTHON_BIN=${SYNOS_PYTHON_BIN:-python3}

# --------------------------------------------------------- the distribution
# Overridable for tests (SYNOS_OS_RELEASE_FILE); real runs read the real
# /etc/os-release. Extracted with sed rather than sourced: this file is
# read on every distribution this host might be, and this script never
# executes shell code out of data it did not itself write.
os_release_file() { printf '%s\n' "${SYNOS_OS_RELEASE_FILE:-/etc/os-release}"; }

os_release_get() {  # KEY
    local file
    file=$(os_release_file)
    [ -r "$file" ] || return 1
    sed -n "s/^$1=//p" "$file" | head -n1 | tr -d '"'
}

# One of debian|fedora|suse|arch|alpine, or nothing (unsupported/undetected)
# on stdout with a non-zero exit — the caller decides what "nothing" means.
detect_distro_family() {
    local id id_like hay
    id=$(os_release_get ID) || return 1
    id_like=$(os_release_get ID_LIKE)
    hay=" $id $id_like "
    case "$hay" in
        *" debian "*|*" ubuntu "*) printf 'debian\n'; return 0 ;;
    esac
    case "$hay" in
        *" fedora "*|*" rhel "*|*" centos "*) printf 'fedora\n'; return 0 ;;
    esac
    case "$hay" in
        *" suse "*|*" opensuse "*|*" sles "*) printf 'suse\n'; return 0 ;;
    esac
    case "$hay" in
        *" arch "*|*" manjaro "*) printf 'arch\n'; return 0 ;;
    esac
    case "$hay" in
        *" alpine "*) printf 'alpine\n'; return 0 ;;
    esac
    return 1
}

SUPPORTED_FAMILIES_MESSAGE='Debian/Ubuntu (apt), Fedora/RHEL/CentOS (dnf), openSUSE/SLES (zypper), Arch/Manjaro (pacman), Alpine (apk)'

require_family() {
    local family
    if family=$(detect_distro_family); then
        printf '%s\n' "$family"
        return 0
    fi
    cat >&2 <<EOF
error: this machine's Linux distribution was not recognized (or $(os_release_file) is missing).
Supported families: $SUPPORTED_FAMILIES_MESSAGE.
Install podman, qemu-system-x86_64, xorriso, tesseract, Pillow (python3) and
a headless Chromium/Chrome by hand, then rerun with --skip-packages.
EOF
    return 3
}

pkg_manager_for() {
    case "$1" in
        debian) printf 'apt-get\n' ;;
        fedora) printf 'dnf\n' ;;
        suse)   printf 'zypper\n' ;;
        arch)   printf 'pacman\n' ;;
        alpine) printf 'apk\n' ;;
        *) return 1 ;;
    esac
}

# Best-effort package names: this project cannot exercise every release of
# every family, so if the install step below names a package your release
# does not have, install the right one by hand and rerun with --skip-packages.
pkg_name() {  # family generic
    case "$1:$2" in
        debian:python3)  printf 'python3\n' ;;
        debian:git)      printf 'git\n' ;;
        debian:pyyaml)   printf 'python3-yaml\n' ;;
        debian:podman)   printf 'podman\n' ;;
        debian:qemu)     printf 'qemu-system-x86\n' ;;
        debian:xorriso)  printf 'xorriso\n' ;;
        debian:tesseract) printf 'tesseract-ocr\n' ;;
        debian:pillow)   printf 'python3-pil\n' ;;
        debian:browser)  printf 'chromium\n' ;;
        fedora:python3)  printf 'python3\n' ;;
        fedora:git)      printf 'git\n' ;;
        fedora:pyyaml)   printf 'python3-pyyaml\n' ;;
        fedora:podman)   printf 'podman\n' ;;
        fedora:qemu)     printf 'qemu-system-x86\n' ;;
        fedora:xorriso)  printf 'xorriso\n' ;;
        fedora:tesseract) printf 'tesseract\n' ;;
        fedora:pillow)   printf 'python3-pillow\n' ;;
        fedora:browser)  printf 'chromium\n' ;;
        suse:python3)    printf 'python3\n' ;;
        suse:git)        printf 'git\n' ;;
        suse:pyyaml)     printf 'python3-PyYAML\n' ;;
        suse:podman)     printf 'podman\n' ;;
        suse:qemu)       printf 'qemu-x86\n' ;;
        suse:xorriso)    printf 'xorriso\n' ;;
        suse:tesseract)  printf 'tesseract-ocr\n' ;;
        suse:pillow)     printf 'python3-Pillow\n' ;;
        suse:browser)    printf 'chromium\n' ;;
        arch:python3)    printf 'python\n' ;;
        arch:git)        printf 'git\n' ;;
        arch:pyyaml)     printf 'python-yaml\n' ;;
        arch:podman)     printf 'podman\n' ;;
        arch:qemu)       printf 'qemu-system-x86_64\n' ;;
        arch:xorriso)    printf 'libisoburn\n' ;;
        arch:tesseract)  printf 'tesseract\n' ;;
        arch:pillow)     printf 'python-pillow\n' ;;
        arch:browser)    printf 'chromium\n' ;;
        alpine:python3)  printf 'python3\n' ;;
        alpine:git)      printf 'git\n' ;;
        alpine:pyyaml)   printf 'py3-yaml\n' ;;
        alpine:podman)   printf 'podman\n' ;;
        alpine:qemu)     printf 'qemu-system-x86_64\n' ;;
        alpine:xorriso)  printf 'xorriso\n' ;;
        alpine:tesseract) printf 'tesseract-ocr\n' ;;
        alpine:pillow)   printf 'py3-pillow\n' ;;
        alpine:browser)  printf 'chromium\n' ;;
        *) return 1 ;;
    esac
}

REQUIREMENTS='python3 git pyyaml podman qemu xorriso tesseract pillow browser'

requirement_present() {  # generic
    case "$1" in
        python3)  command -v "$PYTHON_BIN" >/dev/null 2>&1 ;;
        git)      command -v git >/dev/null 2>&1 ;;
        pyyaml)   "$PYTHON_BIN" -c 'import yaml' >/dev/null 2>&1 ;;
        podman)   command -v podman >/dev/null 2>&1 || command -v docker >/dev/null 2>&1 ;;
        qemu)     command -v qemu-system-x86_64 >/dev/null 2>&1 ;;
        xorriso)  command -v xorriso >/dev/null 2>&1 ;;
        tesseract) command -v tesseract >/dev/null 2>&1 ;;
        pillow)   "$PYTHON_BIN" -c 'import PIL' >/dev/null 2>&1 ;;
        browser)  command -v chromium >/dev/null 2>&1 || command -v chromium-browser >/dev/null 2>&1 \
                       || command -v google-chrome >/dev/null 2>&1 || command -v google-chrome-stable >/dev/null 2>&1 ;;
        *) return 1 ;;
    esac
}

requirement_description() {  # generic
    case "$1" in
        python3)  printf 'runs every tool this service uses\n' ;;
        git)      printf 'clones/updates the engine checkout itself, on a machine that does not have one yet\n' ;;
        pyyaml)   printf 'reads conformance.yml and bundle manifests (tools/catalog_conformance.py)\n' ;;
        podman)   printf 'the container runtime each catalog entry actually builds in (tools/bundle_launcher.sh)\n' ;;
        qemu)     printf 'boots the built ISO for the smoke test (tools/smoke_test.py)\n' ;;
        xorriso)  printf 'pulls the kernel/initrd out of the built ISO for the smoke test (tools/smoke_test.py)\n' ;;
        tesseract) printf 'reads text off the graphical boot screenshot (tools/smoke_test.py)\n' ;;
        pillow)   printf 'converts the QEMU screenshot to PNG for the OCR check (tools/smoke_test.py)\n' ;;
        browser)  printf 'drives the real Studio page under headless Chrome to download a bundle (tools/devtools_browser.py)\n' ;;
        *) printf '\n' ;;
    esac
}

missing_requirements() {
    local generic
    for generic in $REQUIREMENTS; do
        requirement_present "$generic" || printf '%s\n' "$generic"
    done
}

run_package_manager() {  # family package...
    local family=$1
    shift
    case "$family" in
        debian) sudo apt-get update && sudo apt-get install -y "$@" ;;
        fedora) sudo dnf install -y "$@" ;;
        suse)   sudo zypper --non-interactive install "$@" ;;
        arch)   sudo pacman -Sy --noconfirm "$@" ;;
        alpine) sudo apk add "$@" ;;
        *) return 1 ;;
    esac
}

install_requirements() {  # family
    local family=$1 generic missing packages=()
    missing=$(missing_requirements)
    if [ -z "$missing" ]; then
        say "every dependency is already present."
        return 0
    fi
    say "missing, and what each is for:"
    for generic in $missing; do
        local name
        name=$(pkg_name "$family" "$generic") || fail "no package name known for '$generic' on '$family'" 4
        say "  - $name ($(requirement_description "$generic"))"
        packages+=("$name")
    done
    if [ "$DRY_RUN" = "1" ]; then
        say "[dry-run] would install: ${packages[*]}"
        return 0
    fi
    ask_yes "Install the ${#packages[@]} missing package(s) now?" || fail "install them yourself, then rerun this installer" 4
    run_package_manager "$family" "${packages[@]}" || fail "package installation failed" 4
    missing=$(missing_requirements)
    if [ -n "$missing" ]; then
        fail "still missing after the installation attempt: $(printf '%s ' $missing)" 4
    fi
    say "installed."
}

# -------------------------------------------------------------- git checkouts
# Bootstraps a fresh machine (clone) and keeps an already-bootstrapped one
# up to date (fetch + fast-forward) the same safe way every time: never a
# `git reset --hard`, never a `git checkout -f`, never anything that could
# discard work sitting in a checkout by hand. A checkout that is not safe
# to touch — uncommitted changes, a different remote than asked for, a
# branch other than the one asked for — is refused outright, with exactly
# which of the three and why, never forced into shape.
git_rev() { git -C "$1" rev-parse --short HEAD 2>/dev/null; }
git_branch() { git -C "$1" symbolic-ref -q --short HEAD 2>/dev/null; }  # empty when detached (e.g. a tag)
git_is_clean() { [ -z "$(git -C "$1" status --porcelain 2>/dev/null)" ]; }
git_remote_url() { git -C "$1" remote get-url "${2:-origin}" 2>/dev/null; }

# sync_git_checkout root url ref [ssh_key] label
#
# Prints, on stdout, exactly one of: created | updated | unchanged |
# left-alone (a real, non-empty directory that is not a git checkout at
# all — used exactly as found, never touched) | would-create | would-update
# (both --dry-run only) -- and only that, so a caller can capture it with
# $(...) the same way select_storage_path's own callers do. Every
# diagnostic line (the before/after commit, what it is doing) goes to
# stderr instead, for the same reason select_storage_path's own numbers
# do: a $(...) capture swallows every line of stdout, not only the last
# one. Returns nonzero, via `fail` (so it also exits, same as every other
# hard prerequisite in this script), on every refusal.
sync_git_checkout() {  # root url ref ssh_key(optional) label
    local root=$1 url=$2 ref=$3 ssh_key=${4:-} label=$5
    local -a git_env=()
    if [ -n "$ssh_key" ]; then
        git_env=(env "GIT_SSH_COMMAND=ssh -i $ssh_key -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new")
    fi

    if [ ! -e "$root" ] || { [ -d "$root" ] && [ -z "$(ls -A "$root" 2>/dev/null)" ]; }; then
        printf '%s: not present yet at %s\n' "$label" "$root" >&2
        if [ "$DRY_RUN" = "1" ]; then
            printf '  [dry-run] would clone %s (%s) into %s\n' "$url" "$ref" "$root" >&2
            printf 'would-create\n'
            return 0
        fi
        mkdir -p "$(dirname "$root")" 2>/dev/null
        "${git_env[@]}" git clone --branch "$ref" --quiet "$url" "$root" \
            || fail "$label: could not clone $url ($ref) into $root" 7
        printf '  cloned %s (%s) -> %s (%s)\n' "$url" "$ref" "$(git_rev "$root")" "$(git_branch "$root")" >&2
        printf 'created\n'
        return 0
    fi

    # git -C ... rev-parse, not `[ -d "$root/.git" ]`: a git *worktree*'s
    # .git is a plain file (a gitdir pointer), not a directory, and is
    # every bit as real a checkout to fetch/fast-forward as a plain clone.
    if ! git -C "$root" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        printf '%s: %s already exists and is not a git checkout; using it exactly as it is (managing it is up to you -- pass a matching --*-repo only when you want this script to keep it updated).\n' "$label" "$root" >&2
        printf 'left-alone\n'
        return 0
    fi

    local before_rev before_branch
    before_rev=$(git_rev "$root")
    before_branch=$(git_branch "$root")
    printf '%s: %s, currently %s (%s)\n' "$label" "$root" "${before_rev:-unknown}" "${before_branch:-detached}" >&2

    local actual_url
    actual_url=$(git_remote_url "$root")
    if [ -n "$actual_url" ] && [ "$actual_url" != "$url" ]; then
        fail "$label: $root's remote 'origin' is $actual_url, not $url -- refusing to touch a checkout that may be pointed somewhere deliberately. Pass a matching URL, or point this script at a different path." 7
    fi

    git_is_clean "$root" || fail "$label: $root has uncommitted changes -- refusing to touch it. Commit or discard them yourself (never a job for this script), then rerun." 7

    if [ -n "$before_branch" ] && [ "$before_branch" != "$ref" ]; then
        fail "$label: $root is on branch '$before_branch', not '$ref' -- refusing to switch it for you. Check out $ref yourself, or match it with the --*-ref flag, then rerun." 7
    fi

    if [ "$DRY_RUN" = "1" ]; then
        printf '  [dry-run] would fetch %s and fast-forward to %s\n' "$url" "$ref" >&2
        printf 'would-update\n'
        return 0
    fi

    "${git_env[@]}" git -C "$root" fetch --quiet --tags origin "$ref" \
        || fail "$label: could not fetch $ref from $url" 7
    if [ -n "$before_branch" ]; then
        "${git_env[@]}" git -C "$root" merge --ff-only --quiet "origin/$ref" \
            || fail "$label: $root and origin/$ref have diverged -- refusing to force it. Resolve this by hand, then rerun." 7
    else
        git -C "$root" checkout --quiet "$ref" 2>/dev/null \
            || fail "$label: could not check out $ref in $root" 7
    fi
    local after_rev
    after_rev=$(git_rev "$root")
    if [ "$after_rev" = "$before_rev" ]; then
        printf '  already up to date at %s\n' "$after_rev" >&2
        printf 'unchanged\n'
    else
        printf '  updated %s -> %s\n' "$before_rev" "$after_rev" >&2
        printf 'updated\n'
    fi
}

# --------------------------------------------------------------- the runtime
detect_runtime() { command -v podman 2>/dev/null || command -v docker 2>/dev/null; }
is_podman() { case "$1" in */podman|podman) return 0 ;; *) return 1 ;; esac; }
runtime_group() { is_podman "$1" && printf 'podman\n' || printf 'docker\n'; }

docker_storage_note() {
    cat <<'EOF'
docker's storage (images, layers, the chroot) is one setting for the whole
daemon; there is no per-build or per-service location, so this installer
does not attempt to change it. One time, by hand:
  plain install:  add "data-root": "<path>" to /etc/docker/daemon.json,
                   then: sudo systemctl restart docker
  snap install:   the same key in /var/snap/docker/current/config/daemon.json,
                   then: sudo snap restart docker
  or: install podman instead, which this installer configures per service,
      no root, no daemon restart.
EOF
}

# -------------------------------------------------------------- the storage
MIN_BUILD_GB=${SYNOS_MIN_BUILD_GB:-40}
MIN_STORE_GB=${SYNOS_MIN_STORE_GB:-30}

filesystem_type() { stat -f -c %T "$1" 2>/dev/null; }

overlay_capable() {  # fstype
    case "$1" in
        vfat|msdos|exfat|ntfs|fuseblk|cifs|smb2|nfs|nfs4|9p) return 1 ;;
        *) return 0 ;;
    esac
}

# A non-empty string of only digits. Every free-space number this script
# compares with -lt/-gt/-ge is checked against this first: df or stat
# failing, a field split going wrong, or (item 131/133) an invalid flag
# combination df itself refuses must be skipped with a printed reason, and
# must never reach a numeric test — `[ x -lt y ]` on a non-numeric operand
# is a shell diagnostic, not something a person can act on.
is_number() {
    case "$1" in
        ''|*[!0-9]*) return 1 ;;
        *) return 0 ;;
    esac
}

# "mountpoint avail_kb fstype", one per real (non-pseudo, non-network)
# filesystem df knows about. `-k --output=target,avail,fstype`, deliberately
# without `-P`: GNU coreutils df refuses `-P` together with `--output`
# ("options -P and --output are mutually exclusive") — `-P` exists only to
# force df's traditional one-line-per-entry layout, which `--output`
# already guarantees by naming exactly the fields wanted, so it adds
# nothing here and is never combined with it. `-k` alone still fixes the
# unit (1024-byte blocks) regardless.
list_candidate_filesystems() {
    df -k --output=target,avail,fstype 2>/dev/null | tail -n +2 | while read -r target avail fstype; do
        case "$fstype" in
            tmpfs|devtmpfs|proc|sysfs|cgroup|cgroup2|overlay|squashfs|autofs|binfmt_misc|none| \
            nfs|nfs4|cifs|smb2|efivarfs|pstore|tracefs|debugfs|mqueue|hugetlbfs|fuse.*|rpc_pipefs) continue ;;
        esac
        printf '%s %s %s\n' "$target" "$avail" "$fstype"
    done
}

# Picks podman's storage location: an explicit path (validated) or, absent
# one, the largest suitable filesystem with enough room. Diagnostic numbers
# go to stderr as they are found (item 2's "showing the numbers it used");
# the winning "path avail_kb" goes to stdout, alone, on success.
select_storage_path() {  # min_gb [explicit_path] [create: 1 (default) or 0]
    local min_gb=$1 explicit=${2:-} create=${3:-1} min_kb
    min_kb=$((min_gb * 1024 * 1024))
    if [ -n "$explicit" ]; then
        local check_path=$explicit
        if [ ! -d "$explicit" ]; then
            if [ "$create" = "1" ]; then
                mkdir -p "$explicit" 2>/dev/null || { printf 'could not create %s\n' "$explicit" >&2; return 1; }
            else
                # create=0 (this installer's own --dry-run): mkdir -p would
                # perform a real action, which --dry-run promises never to
                # do. Measure the nearest existing ancestor instead — still
                # honest about the numbers, never a mutation.
                while [ ! -d "$check_path" ] && [ "$check_path" != "/" ] && [ -n "$check_path" ]; do
                    check_path=$(dirname "$check_path")
                done
                [ -d "$check_path" ] || { printf 'could not find any existing ancestor of %s to check\n' "$explicit" >&2; return 1; }
                printf 'note: %s does not exist yet; would be created (checked its nearest existing ancestor, %s, instead)\n' \
                    "$explicit" "$check_path" >&2
            fi
        fi
        local fstype avail
        fstype=$(filesystem_type "$check_path")
        if ! overlay_capable "$fstype"; then
            printf '%s is %s, which cannot back a container overlay filesystem; choose a disk formatted ext4, xfs, btrfs or similar\n' \
                "$check_path" "$fstype" >&2
            return 1
        fi
        avail=$(df -Pk "$check_path" 2>/dev/null | awk 'NR==2 {print $4}')
        if ! is_number "$avail"; then
            printf 'could not read free space at %s (df gave %s, not a number)\n' "$check_path" "${avail:-nothing}" >&2
            return 1
        fi
        printf 'chosen: %s (%d GB free, %s)\n' "$explicit" "$((avail / 1048576))" "$fstype" >&2
        if [ "$avail" -lt "$min_kb" ]; then
            printf 'note: %s has only %d GB free; a build needs about %d GB.\n' "$explicit" "$((avail / 1048576))" "$min_gb" >&2
        fi
        printf '%s %s\n' "$explicit" "$avail"
        return 0
    fi

    local best_target='' best_avail=0 considered=0 skipped=0 target avail fstype
    # Process substitution, not a heredoc around a command substitution:
    # when list_candidate_filesystems prints nothing at all, a heredoc
    # built from `$(...)` still feeds the loop exactly one blank line (the
    # newline around the now-empty substitution survives), so `read`
    # succeeds once with every field empty — which is what actually
    # crashed this function (item 131/133: "considered:  (0 GB free, )"
    # then `[ : -lt ... ]`) once df's own error above left it with zero
    # real candidates. Process substitution has no such surrounding text:
    # zero lines of output is zero iterations of this loop.
    while read -r target avail fstype; do
        if [ -z "$target" ] && [ -z "$avail" ] && [ -z "$fstype" ]; then
            continue  # a stray blank line, never a real filesystem
        fi
        if ! is_number "$avail"; then
            skipped=$((skipped + 1))
            printf 'skipping %s: could not read its free space (df gave %s, not a number)\n' "${target:-?}" "${avail:-nothing}" >&2
            continue
        fi
        considered=$((considered + 1))
        printf 'considered: %s (%d GB free, %s)\n' "$target" "$((avail / 1048576))" "$fstype" >&2
        overlay_capable "$fstype" || continue
        [ "$avail" -gt "$best_avail" ] || continue
        best_target=$target
        best_avail=$avail
    done < <(list_candidate_filesystems)

    if [ -z "$best_target" ]; then
        printf 'no filesystem capable of backing a container overlay was found among %d candidate(s) (%d skipped, unreadable)\n' \
            "$considered" "$skipped" >&2
        return 1
    fi
    printf 'chosen: %s (%d GB free, the largest of %d candidate(s) considered)\n' "$best_target" "$((best_avail / 1048576))" "$considered" >&2
    if [ "$best_avail" -lt "$min_kb" ]; then
        printf 'note: the largest available filesystem, %s, has only %d GB free; a build needs about %d GB.\n' \
            "$best_target" "$((best_avail / 1048576))" "$min_gb" >&2
    fi
    printf '%s %s\n' "$best_target" "$best_avail"
}

# Free space at PATH, or its nearest existing ancestor when PATH does not
# exist yet (a --dry-run preview described a location without creating it;
# select_storage_path's own dry-run note above explains why). "" when even
# the ancestor walk finds nothing readable.
free_kb_at() {  # path
    local path=$1
    while [ ! -d "$path" ] && [ "$path" != "/" ] && [ -n "$path" ]; do
        path=$(dirname "$path")
    done
    [ -d "$path" ] || return 1
    df -Pk "$path" 2>/dev/null | awk 'NR==2 {print $4}'
}

# Idempotent write of podman's rootless storage.conf graphroot: unchanged
# when the exact same value is already there, updated in place inside an
# existing [storage] section, or appended with a new section otherwise.
# The value is always passed to awk as a variable (-v), never spliced into
# an awk/sed *program* string, so a path containing regex metacharacters
# cannot change what this does.
write_storage_conf() {  # conf_path graphroot
    local conf=$1 graphroot=$2 tmp
    mkdir -p "$(dirname "$conf")" || return 1
    if [ -f "$conf" ] && awk -v g="$graphroot" '
            $0 ~ /^[[:space:]]*graphroot[[:space:]]*=/ {
                line = $0
                sub(/^[[:space:]]*graphroot[[:space:]]*=[[:space:]]*"/, "", line)
                sub(/".*$/, "", line)
                if (line == g) { found = 1 }
            }
            END { exit !found }
        ' "$conf"; then
        return 0
    fi
    tmp="$conf.new.$$"
    if [ -f "$conf" ] && grep -q '^\[storage\]' "$conf"; then
        awk -v g="$graphroot" '
            BEGIN { in_storage = 0; wrote = 0 }
            /^\[storage\]/ { in_storage = 1; print; next }
            /^\[/ {
                if (in_storage && !wrote) { print "graphroot = \"" g "\""; wrote = 1 }
                in_storage = 0; print; next
            }
            in_storage && $0 ~ /^[[:space:]]*graphroot[[:space:]]*=/ {
                print "graphroot = \"" g "\""; wrote = 1; next
            }
            { print }
            END { if (in_storage && !wrote) print "graphroot = \"" g "\"" }
        ' "$conf" > "$tmp" || { rm -f "$tmp"; return 1; }
    else
        {
            [ -f "$conf" ] && cat "$conf"
            printf '\n[storage]\ndriver = "overlay"\ngraphroot = "%s"\n' "$graphroot"
        } > "$tmp"
    fi
    mv "$tmp" "$conf"
}

configure_podman_storage() {  # service_home storage_path
    local home=$1 storage=$2 conf
    conf="$home/.config/containers/storage.conf"
    mkdir -p "$storage" || fail "could not create $storage" 5
    local fstype
    fstype=$(filesystem_type "$storage")
    overlay_capable "$fstype" || fail "$storage is $fstype, which cannot back a container overlay filesystem" 5
    write_storage_conf "$conf" "$storage" || fail "could not write $conf" 5
    chown -R "$SERVICE_USER:$SERVICE_GROUP" "$storage" "$home/.config" 2>/dev/null \
        || warn "could not chown $storage and $home/.config to $SERVICE_USER:$SERVICE_GROUP; fix ownership by hand"
    say "podman storage.conf ($conf): graphroot = \"$storage\""
}

# --------------------------------------------------------------- the jobs
MIN_MEMORY_GB_PER_JOB=${SYNOS_MIN_MEMORY_GB_PER_JOB:-4}
CPUS_PER_JOB=${SYNOS_CPUS_PER_JOB:-2}

derive_jobs() {  # checkout_root store_path
    local checkout=$1 store=$2
    local free_checkout_kb free_store_kb mem_kb cpu_count
    free_checkout_kb=$(free_kb_at "$checkout")
    free_store_kb=$(free_kb_at "$store")
    mem_kb=$(awk '/^MemTotal:/{print $2}' "${SYNOS_MEMINFO_FILE:-/proc/meminfo}" 2>/dev/null)
    cpu_count=$(nproc 2>/dev/null || printf '1\n')
    # Every one of these feeds an arithmetic/-lt comparison below; a df or
    # nproc that failed or returned garbage must count as "unknown", never
    # crash the comparison (item 133 applied here too, not only in the
    # storage scan).
    is_number "$free_checkout_kb" || { warn "could not read free space at $checkout; treating it as 0"; free_checkout_kb=0; }
    is_number "$free_store_kb" || { warn "could not read free space at $store; treating it as 0"; free_store_kb=0; }
    is_number "$mem_kb" || mem_kb=""
    is_number "$cpu_count" || cpu_count=1

    local disk_checkout_jobs disk_store_jobs disk_jobs memory_jobs cpu_jobs safe
    disk_checkout_jobs=$((free_checkout_kb / (MIN_BUILD_GB * 1048576)))
    disk_store_jobs=$((free_store_kb / (MIN_STORE_GB * 1048576)))
    disk_jobs=$disk_checkout_jobs
    [ "$disk_store_jobs" -lt "$disk_jobs" ] && disk_jobs=$disk_store_jobs
    if [ -n "$mem_kb" ]; then
        memory_jobs=$((mem_kb / 1048576 / MIN_MEMORY_GB_PER_JOB))
    else
        memory_jobs=$disk_jobs
    fi
    cpu_jobs=$((cpu_count / CPUS_PER_JOB))
    [ "$cpu_jobs" -lt 1 ] && cpu_jobs=1
    safe=$disk_jobs
    [ "$memory_jobs" -lt "$safe" ] && safe=$memory_jobs
    [ "$cpu_jobs" -lt "$safe" ] && safe=$cpu_jobs

    printf 'disk allows %d (checkout %d GB free / %d per build, store %d GB free / %d per build); memory allows %s; CPUs allow %d (%d cores / %d per build)\n' \
        "$disk_jobs" "$((free_checkout_kb / 1048576))" "$MIN_BUILD_GB" "$((free_store_kb / 1048576))" "$MIN_STORE_GB" \
        "$memory_jobs" "$cpu_jobs" "$cpu_count" "$CPUS_PER_JOB" >&2
    if [ "$safe" -lt 1 ]; then
        printf 'note: this machine cannot safely feed even one build at once; the service will still be installed with jobs "1", but tools/catalog_conformance.py itself will refuse to run a build until more room is free.\n' >&2
        safe=1
    fi
    printf '%s\n' "$safe"
}

# ------------------------------------------------------------- the config
yaml_scalar() {  # value -- double-quoted YAML scalar, quotes/backslashes escaped
    local v=$1
    v=${v//\\/\\\\}
    v=${v//\"/\\\"}
    printf '"%s"' "$v"
}

# Writes the configuration from answers, never from a template left for a
# person to edit blind, and never with a credential in it: the upload
# section is always fully commented out, naming where the real secret goes
# and what permissions it needs, never a value read from anywhere.
write_config() {  # catalog_url site_url workdir container_root max_builds jobs service_user [dev_site_root] [dev_site_owner] [dev_site_mode]
    local catalog_url=$1 site_url=$2 workdir=$3 container_root=$4 max_builds=$5 jobs=$6 service_user=$7
    local dev_site_root=${8:-} dev_site_owner=${9:-} dev_site_mode=${10:-}
    printf '# Written by packaging/install-test-engine.sh from the answers given at install\n'
    printf '# time. Safe to edit by hand; rerunning the installer only touches the keys it\n'
    printf '# itself asks about.\n\n'
    printf 'catalog_url: %s\n' "$(yaml_scalar "$catalog_url")"
    if [ -n "$site_url" ]; then
        printf 'site_url: %s\n' "$(yaml_scalar "$site_url")"
    else
        printf '# site_url: <fill in before running "build" mode; "check" mode does not need it>\n'
    fi
    printf 'workdir: %s\n' "$(yaml_scalar "$workdir")"
    printf 'min_free_gb: %s\n' "$MIN_BUILD_GB"
    printf 'min_store_gb: %s\n' "$MIN_STORE_GB"
    printf 'max_builds_per_run: %s\n' "$max_builds"
    printf 'build_timeout_minutes: 90\n'
    printf 'jobs: %s\n' "$(yaml_scalar "$jobs")"
    printf 'smoke: true\n'
    printf 'image: null\n'
    printf 'engine_source: null\n'
    if [ -n "$container_root" ]; then
        printf 'container_root: %s\n' "$(yaml_scalar "$container_root")"
    else
        printf 'container_root: null\n'
    fi
    printf 'container_runroot: null\n'
    printf 'report_url: null\n'
    printf 'cleanup: true\n'
    printf '\n'
    if [ -n "$dev_site_root" ]; then
        printf '# Rehearsal-then-production upload list (tools/status_uploader.py,\n'
        printf '# docs/BUILD_MATRIX.md'"'"'s "Publishing the build-status badge"): "dev-site" below is a\n'
        printf '# plain file copy to this machine'"'"'s own development Studio site -- not a network\n'
        printf '# call at all -- published, then fetched back from site_url above and checked to\n'
        printf '# actually be valid *as served* before anything further down this list is even\n'
        printf '# attempted. A rehearsal failure is reported and never touches production.\n'
        printf '#\n'
        printf '# Fill in a real "production" destination yourself, outside any checkout, the same\n'
        printf '# way the commented-out example below shows -- the actual secret (a password, or\n'
        printf '# better, an unencrypted ssh private key) goes in its own file, owned by %s,\n' "$service_user"
        printf '# mode 600 -- never in this file, and never written by this installer.\n'
        printf 'upload:\n'
        printf '  - name: dev-site\n'
        printf '    protocol: local\n'
        printf '    remote_path: %s\n' "$(yaml_scalar "${dev_site_root%/}/data/build-status.json")"
        if [ -n "$dev_site_owner" ]; then
            printf '    owner: %s\n' "$(yaml_scalar "$dev_site_owner")"
        fi
        if [ -n "$dev_site_mode" ]; then
            printf '    mode: %s\n' "$(yaml_scalar "$dev_site_mode")"
        fi
        printf '  # - name: production\n'
        printf '  #   protocol: sftp\n'
        printf '  #   host: <upload host>\n'
        printf '  #   port: 22\n'
        printf '  #   username: <upload username>\n'
        printf '  #   key_path: /etc/synos/conformance-upload-key   # mode 600, owned by %s; the secret goes ONLY here\n' "$service_user"
        printf '  #   remote_path: <remote path to build-status.json>\n'
        printf '  #   allow_insecure_ftp: false\n'
        printf '  #   retries: 3\n'
        printf '  #   retry_backoff_s: 5\n'
    else
        printf '# Upload credentials are NEVER written by this installer. To enable uploading\n'
        printf '# the build-status badge, uncomment below and fill in the destination, then put\n'
        printf '# the actual secret (a password, or better, an unencrypted ssh private key) in\n'
        printf '# its own file, owned by %s, mode 600 (chmod 600, chown %s) — never in this file.\n' "$service_user" "$service_user"
        printf '#\n'
        printf '# upload:\n'
        printf '#   protocol: sftp\n'
        printf '#   host: <upload host>\n'
        printf '#   port: 22\n'
        printf '#   username: <upload username>\n'
        printf '#   key_path: /etc/synos/conformance-upload-key   # mode 600, owned by %s; the secret goes ONLY here\n' "$service_user"
        printf '#   remote_path: <remote path to build-status.json>\n'
        printf '#   allow_insecure_ftp: false\n'
        printf '#   retries: 3\n'
        printf '#   retry_backoff_s: 5\n'
    fi
}

# Writes `rendered` to `path`, or reports and leaves an existing, differing
# file exactly alone -- never overwritten automatically, even with
# -y/--yes, unless `overwrite` is "1" (--overwrite-config): a config file
# may hold real, hand-filled-in credentials or edits, and this is the one
# seam every config-writing call in this script goes through, so that
# guarantee holds everywhere, not only where someone remembered to check.
# Appends one summary line to the SUMMARY array (declared in main) --
# created, updated (overwritten), or left alone (and why) -- so the
# closing report says what actually happened, not just what was asked for.
write_or_skip_config() {  # description path rendered overwrite
    local desc=$1 path=$2 rendered=$3 overwrite=$4
    if [ "$DRY_RUN" = "1" ]; then
        say "[dry-run] would write $path:"
        printf '%s\n' "$rendered" | sed 's/^/  /'
        SUMMARY+=("$desc: would write $path")
        return 0
    fi
    if [ -f "$path" ] && [ "$rendered" = "$(cat "$path")" ]; then
        say "$path already matches these answers; leaving it alone."
        SUMMARY+=("$desc: left alone (already up to date)")
        return 0
    fi
    if [ -f "$path" ]; then
        if [ "$overwrite" = "1" ]; then
            printf '%s\n' "$rendered" > "$path.new.$$" && mv "$path.new.$$" "$path"
            say "overwrote $path (--overwrite-config)"
            SUMMARY+=("$desc: overwritten (--overwrite-config)")
        else
            warn "$path already exists and differs from what these answers would produce; leaving it unchanged (pass --overwrite-config to replace it -- never done automatically, even with -y/--yes, in case it holds real credentials or edits made by hand)."
            SUMMARY+=("$desc: left alone (differs from these answers; rerun with --overwrite-config to replace it)")
        fi
        return 0
    fi
    mkdir -p "$(dirname "$path")" 2>/dev/null
    printf '%s\n' "$rendered" > "$path.new.$$" && mv "$path.new.$$" "$path"
    say "wrote $path"
    SUMMARY+=("$desc: created")
}

# -------------------------------------------------------------- the service
SERVICE_USER=${SERVICE_USER:-synos-conformance}
SERVICE_GROUP=${SERVICE_GROUP:-synos-conformance}

# create_service_user user group home runtime -- generalized so both the
# conformance service user and the monitoring app's own (an independent
# name, home and group) go through exactly one, already-proven path
# (item 134's own --home-dir fix included) instead of two copies drifting
# apart. The two callers in main() each still default their own
# user/group from SERVICE_USER/MONITORING_SERVICE_USER.
create_service_user() {  # user group home runtime
    local user=$1 group=$2 home=$3 runtime=$4
    if id "$user" >/dev/null 2>&1; then
        say "service user $user already exists."
    else
        ask_yes "Create system user $user (home $home)?" \
            || fail "a dedicated system user is required; create $user yourself, then rerun this installer" 5
        maybe "create system user $user (home $home)" \
            sudo useradd --system --home-dir "$home" --create-home --shell /usr/sbin/nologin "$user" \
            || fail "could not create user $user" 5
    fi
    local runtime_grp
    runtime_grp=$(runtime_group "$runtime")
    if getent group "$runtime_grp" >/dev/null 2>&1; then
        maybe "add $user to group $runtime_grp" sudo usermod -aG "$runtime_grp" "$user"
    fi
}

# Renders one unit file: WorkingDirectory= and the engine path inside
# ExecStart= point at $engine_root, and (only for the two .service files)
# ExecStart='s interpreter is $python_bin. Values are passed to awk as
# variables (-v), never spliced into the awk program text.
render_unit_file() {  # src dest engine_root python_bin
    awk -v root="$3" -v py="$4" '
        /^WorkingDirectory=/ { print "WorkingDirectory=" root; next }
        /^ExecStart=/ {
            line = $0
            sub(/^ExecStart=[^ ]+ /, "ExecStart=" py " ", line)
            sub(/\/opt\/synos-engine/, root, line)
            print line
            next
        }
        { print }
    ' "$1" > "$2"
}

install_service_units() {  # src_dir dest_dir engine_root python_bin
    local src_dir=$1 dest_dir=$2 engine_root=$3 python_bin=$4 unit
    [ "$DRY_RUN" = "1" ] || mkdir -p "$dest_dir" 2>/dev/null || true
    for unit in synos-conformance-build.service synos-conformance-build.timer \
                synos-conformance-check.service synos-conformance-check.timer; do
        [ -f "$src_dir/$unit" ] || fail "$src_dir/$unit is missing" 6
        if [ "$DRY_RUN" = "1" ]; then
            say "[dry-run] would install $dest_dir/$unit (WorkingDirectory/ExecStart pointed at $engine_root)"
            continue
        fi
        local tmp="$dest_dir/$unit.new.$$"
        case "$unit" in
            *.service) render_unit_file "$src_dir/$unit" "$tmp" "$engine_root" "$python_bin" ;;
            *)         cp "$src_dir/$unit" "$tmp" ;;
        esac
        mv "$tmp" "$dest_dir/$unit"
        say "installed $dest_dir/$unit"
    done
    if [ "$DRY_RUN" = "1" ]; then
        say ""
        say "Once installed, the timers would NOT be enabled automatically. Afterwards:"
    else
        sudo systemctl daemon-reload 2>/dev/null || warn "systemctl daemon-reload failed; is systemd running?"
        say ""
        say "Timers are installed but NOT enabled. When you are ready:"
    fi
    say "  start (once, to watch it work):  sudo systemctl start synos-conformance-check.service"
    say "  watch:                           sudo journalctl -u synos-conformance-check.service -f"
    say "  stop:                            sudo systemctl stop synos-conformance-check.service"
    say "  enable the timers:               sudo systemctl enable --now synos-conformance-check.timer synos-conformance-build.timer"
}

# ------------------------------------------------------------- verification
run_check() {  # description command [args...]
    local description=$1
    shift
    if "$@"; then
        say "  PASS  $description"
        return 0
    fi
    say "  FAIL  $description"
    return 1
}

verify_runtime() { "$1" info >/dev/null 2>&1; }

verify_storage_location() {  # runtime storage
    local actual
    actual=$("$1" --root "$2" info --format '{{.Store.GraphRoot}}' 2>/dev/null)
    [ "$actual" = "$2" ]
}

# Exit 2: no candidate binary on PATH at all (distinguished from a binary
# that exists but fails to launch headless, exit 1, so the caller can print
# a precise reason for the first case instead of a generic FAIL).
verify_browser() {
    local bin
    bin=$(command -v chromium || command -v chromium-browser || command -v google-chrome || command -v google-chrome-stable) || return 2
    timeout 20 "$bin" --headless=new --no-sandbox --disable-gpu --dump-dom about:blank >/dev/null 2>&1
}

verify_qemu() { command -v qemu-system-x86_64 >/dev/null 2>&1 && qemu-system-x86_64 --version >/dev/null 2>&1; }

verify_ocr() {
    command -v tesseract >/dev/null 2>&1 || return 1
    "$PYTHON_BIN" -c 'import PIL' >/dev/null 2>&1 || return 1
    local tmp rc
    tmp=$(mktemp -d) || return 1
    "$PYTHON_BIN" - "$tmp/ocr.png" <<'PY'
import sys
from PIL import Image, ImageDraw
img = Image.new("RGB", (240, 60), "white")
ImageDraw.Draw(img).text((10, 10), "SYNOS OK", fill="black")
img.save(sys.argv[1])
PY
    tesseract "$tmp/ocr.png" stdout 2>/dev/null | grep -qi "SYNOS"
    rc=$?
    rm -rf "$tmp"
    return "$rc"
}

verify_free_space() {  # path min_gb
    local avail_kb min_kb
    avail_kb=$(df -Pk "$1" 2>/dev/null | awk 'NR==2{print $4}')
    is_number "$avail_kb" || return 1
    min_kb=$(($2 * 1048576))
    [ "$avail_kb" -ge "$min_kb" ]
}

verify_service_dry_run() {  # engine_root config python_bin
    "$3" "$1/tools/catalog_conformance.py" check --config "$2" --dry-run >/dev/null 2>&1
}

verify_installation() {  # runtime storage engine_root config python_bin min_store_gb
    local runtime=$1 storage=$2 engine_root=$3 config=$4 python_bin=$5 min_store_gb=$6 failed=0
    say "verification:"
    run_check "container runtime ($runtime) answers"        verify_runtime "$runtime"                      || failed=1
    if is_podman "$runtime"; then
        run_check "podman storage is at $storage"            verify_storage_location "$runtime" "$storage"  || failed=1
    fi
    local browser_status
    verify_browser; browser_status=$?
    if [ "$browser_status" -eq 0 ]; then
        say "  PASS  headless browser starts"
    else
        say "  FAIL  headless browser starts"
        failed=1
        if [ "$browser_status" -eq 2 ]; then
            say "        no chromium, chromium-browser, google-chrome or google-chrome-stable on PATH."
            say "        This installer ships neither browser itself — install one yourself and rerun"
            say "        with --skip-packages. chromium is the first thing to try: a real package on"
            say "        Debian, but on Ubuntu \"chromium\"/\"chromium-browser\" only installs the"
            say "        Chromium *snap* (Pre-Depends: snapd), so it will not help there. On Ubuntu,"
            say "        install Google Chrome yourself instead, from Google's own apt repository"
            say "        (https://dl.google.com/linux/chrome/deb/) under Google's own terms — this"
            say "        project does not redistribute it. On a SynOS-built machine, the \"test-engine\""
            say "        bundle (profiles/bundles.yml) already ships a real chromium on Debian; on"
            say "        Ubuntu it refuses to render at all rather than build one short"
            say "        (tools/render_manifest.py, bases/ubuntu/packages.map)."
        fi
    fi
    run_check "qemu-system-x86_64 runs"                       verify_qemu                                    || failed=1
    run_check "tesseract reads a generated test image"        verify_ocr                                     || failed=1
    run_check "free space at $storage is at least ${min_store_gb} GB" verify_free_space "$storage" "$min_store_gb" || failed=1
    run_check "the service's own dry run lists catalog entries" verify_service_dry_run "$engine_root" "$config" "$python_bin" || failed=1
    if [ "$failed" -ne 0 ]; then
        printf 'error: one or more verification checks failed; fix the item marked FAIL above, then rerun this installer.\n' >&2
        return 1
    fi
    say "all checks passed."
}

# Bare metal genuinely has /dev/kvm; a nested/virtualized build host often
# does not. Purely informational -- never a run_check PASS/FAIL, never a
# reason to refuse anything -- because a missing /dev/kvm does not make the
# boot check wrong, only much slower (minutes instead of seconds each), and
# silently eating that cost with no explanation is worse than a plain note.
kvm_status() {
    if [ ! -e /dev/kvm ]; then
        printf 'not present -- the boot check will run unaccelerated (minutes, not seconds, per boot); on bare metal this usually means virtualization is disabled in firmware or the kvm module is not loaded\n'
    elif [ -r /dev/kvm ] && [ -w /dev/kvm ]; then
        printf 'present and accessible -- the boot check will run accelerated\n'
    else
        printf 'present but not accessible (permissions on /dev/kvm) -- the boot check will run unaccelerated; add the service user to the kvm group\n'
    fi
}

# ------------------------------------------------------- the monitoring app
# Entirely optional (packaging/install-test-engine.sh's own header
# comment): every function below only ever runs when --monitoring-app or
# --monitoring-repo is given, and none of it is reachable, or even
# mentioned, otherwise. Installs a private, separate repository's own
# checkout (never any of its code copied into this one) behind an nginx
# site that is always either authenticated or refused outright -- see the
# nginx/auth functions below for the one rule this half of the script
# never bends: the app itself keeps binding 127.0.0.1 only, and nothing
# unauthenticated is ever put on a network beyond loopback.
MONITORING_SERVICE_USER=${MONITORING_SERVICE_USER:-synos-forge}

# python3 -m venv needs its own package on Debian/Ubuntu (python3-venv);
# every other family this script knows already bundles it into python3
# itself, so this is only ever missing on that one family.
ensure_python_venv_available() {  # family
    local family=$1
    "$PYTHON_BIN" -m venv --help >/dev/null 2>&1 && return 0
    [ "$family" = "debian" ] || fail "python3's own venv module is missing and this is not a distribution family this installer knows a package name for; install it yourself and rerun with --skip-packages" 8
    if [ "$DRY_RUN" = "1" ]; then
        say "[dry-run] would install python3-venv (python3 -m venv is missing)"
        return 0
    fi
    ask_yes "Install python3-venv (needed to give the monitoring app its own virtualenv)?" \
        || fail "python3-venv is required for the monitoring app; install it yourself, then rerun" 8
    run_package_manager "$family" python3-venv || fail "could not install python3-venv" 8
}

# Structural sanity check: this is what stands in for "a real checkout of
# the monitoring app" everywhere below, the same role
# tools/catalog_conformance.py's own presence plays for --engine-root.
looks_like_monitoring_app_checkout() { [ -f "$1/forge/app.py" ] && [ -f "$1/packaging/synos-forge.service" ]; }

_install_forge_venv() {  # app_root
    "$PYTHON_BIN" -m venv "$1/.venv" && "$1/.venv/bin/pip" install --quiet "$1"
}

# Renders the monitoring app's own config/forge.example.toml (read from its
# checkout, never copied into this repository) into a real
# /etc/synos-forge/forge.toml, wiring the one thing this installer is
# actually responsible for connecting: [conformance].status_dir pointed at
# the *same* workdir the conformance service it just configured writes
# build-status.json/build-report.json into, and [engine].checkout pointed
# at the same --engine-root. Forge reads those two files straight off disk,
# read-only, on its own poll (forge/ingest.py in its checkout) -- there is
# no upload/HTTP step to wire on this side at all.
render_forge_config() {  # engine_root conformance_config_path conformance_workdir port service_user
    local engine_root=$1 conformance_config=$2 workdir=$3 port=$4 service_user=$5
    printf '# Written by packaging/install-test-engine.sh --monitoring-app. Safe to edit by\n'
    printf '# hand; rerunning the installer only touches the keys it itself asks about.\n\n'
    printf '[service]\n'
    printf 'host = "127.0.0.1"\n'
    printf 'port = %s\n' "$port"
    printf 'owner_id = "local"\n\n'
    printf '[engine]\n'
    printf 'checkout = %s\n' "$(yaml_scalar "$engine_root")"
    printf 'python = %s\n' "$(yaml_scalar "$PYTHON_BIN")"
    printf 'pull = false\n\n'
    printf '[queue]\n'
    printf 'workers = 2\n'
    printf 'max_attempts = 2\n'
    printf 'poll_interval_s = 2.0\n\n'
    printf '[artifacts]\n'
    printf 'root = %s\n' "$(yaml_scalar "/var/lib/$service_user/artifacts")"
    printf 'retention_days = 14\n\n'
    printf '[database]\n'
    printf 'path = %s\n\n' "$(yaml_scalar "/var/lib/$service_user/forge.db")"
    printf '[conformance]\n'
    printf '# Wired to the conformance service this same installer run configured --\n'
    printf '# tools/catalog_conformance.py build writes build-status.json/build-report.json\n'
    printf '# under its own workdir:, and this points Forge at the same place, read-only.\n'
    printf 'config_path = %s\n' "$(yaml_scalar "$conformance_config")"
    printf 'status_dir = %s\n' "$(yaml_scalar "$workdir")"
    printf '\n# No [[schedule]] entries: this installer wires the read-only ingest path\n'
    printf '# above only. Add [[schedule]] entries yourself (see this checkout'"'"'s own\n'
    printf '# config/forge.example.toml) to have the app queue and run builds of its own.\n'
}

render_forge_unit_file() {  # src dest app_root config_path service_user port
    awk -v root="$3" -v cfg="$4" -v user="$5" -v port="$6" '
        /^WorkingDirectory=/ { print "WorkingDirectory=" root; next }
        /^User=/ { print "User=" user; next }
        /^Group=/ { print "Group=" user; next }
        /^Environment=SYNOS_FORGE_CONFIG=/ { print "Environment=SYNOS_FORGE_CONFIG=" cfg; next }
        /^ExecStart=/ {
            line = $0
            sub(/^ExecStart=[^ ]+/, "ExecStart=" root "/.venv/bin/uvicorn", line)
            sub(/--port [0-9]+/, "--port " port, line)
            print line
            next
        }
        /^ReadWritePaths=/ { print "ReadWritePaths=/var/lib/" user; next }
        { print }
    ' "$1" > "$2"
}

# --------------------------------------------------- monitoring auth/nginx
# The one rule every path below is built around: the app keeps binding
# 127.0.0.1 only (its own systemd unit, rendered above, never changes
# that); the nginx site in front of it is the only thing that ever reaches
# a wider address, and it is always either authenticated or refused
# outright -- never a default that quietly publishes an unauthenticated
# console (the app's own README: "no login ... a real deployment ... needs
# a reverse proxy with real authentication in front of this, which does
# not exist yet").
htpasswd_hash() {  # password -- an apr1 hash, via openssl (the honest
                     # minimum this installer can generate without assuming
                     # apache2-utils' own htpasswd binary is present)
    openssl passwd -apr1 "$1"
}

# Writes an htpasswd file for a freshly generated credential. Never called
# under --dry-run (the caller only announces what it would do); never
# writes the plaintext password anywhere -- only its apr1 hash, and only to
# `path`. Returns the plaintext password on stdout so the caller can show
# it to the operator's terminal exactly once and never again.
generate_monitor_credential() {  # path user
    local path=$1 user=$2 password hash
    command -v openssl >/dev/null 2>&1 \
        || fail "no --monitor-auth-file given and 'openssl' is not on PATH to generate one; install openssl (or apache2-utils' htpasswd) or pass --monitor-auth-file=PATH -- this installer will not publish the monitoring console without authentication" 8
    password=$(openssl rand -base64 18 | tr -dc 'A-Za-z0-9' | cut -c1-20)
    [ -n "$password" ] || fail "could not generate a random password" 8
    hash=$(htpasswd_hash "$password") || fail "could not hash the generated password" 8
    mkdir -p "$(dirname "$path")" 2>/dev/null
    printf '%s:%s\n' "$user" "$hash" > "$path.new.$$" \
        && chmod 640 "$path.new.$$" && mv "$path.new.$$" "$path" \
        || fail "could not write $path" 8
    printf '%s\n' "$password"
}

# The nginx site: a separate, dedicated server block on its own port,
# never a location spliced into a site the operator wrote (this script
# never edits an existing site file, only ever writes its own, new one).
# `location` inside that block can still be whatever path the operator
# names (--monitoring-nginx-location) -- it is a location *of this new
# block*, never a second location of theirs. TLS when a cert/key is given;
# otherwise plain HTTP, `allow`/`deny`-restricted to loopback unless
# --monitoring-allow-from named a wider range (main() already refused that
# combination outright unless TLS or --monitoring-allow-insecure-http
# also said so).
render_monitor_nginx_site() {  # listen_addr nginx_port location auth_file upstream_port allow_cidr tls_cert tls_key
    local addr=$1 port=$2 loc=$3 authfile=$4 upstream=$5 allow=$6 cert=$7 key=$8
    printf '# Managed by packaging/install-test-engine.sh --monitoring-app.\n'
    printf '# Additive only: a new, separate server block on its own port -- never an edit\n'
    printf '# to any other site. Safe to remove this file on its own.\n'
    printf 'server {\n'
    if [ -n "$cert" ]; then
        printf '    listen %s:%s ssl;\n' "$addr" "$port"
        printf '    ssl_certificate %s;\n' "$cert"
        printf '    ssl_certificate_key %s;\n' "$key"
    else
        printf '    listen %s:%s;\n' "$addr" "$port"
    fi
    printf '    server_name _;\n'
    if [ -n "$allow" ]; then
        printf '    allow %s;\n' "$allow"
        printf '    allow 127.0.0.1;\n'
        printf '    allow ::1;\n'
        printf '    deny all;\n'
    fi
    printf '    location %s {\n' "$loc"
    printf '        auth_basic "SynOS Forge";\n'
    printf '        auth_basic_user_file %s;\n' "$authfile"
    printf '        proxy_pass http://127.0.0.1:%s/;\n' "$upstream"
    printf '        proxy_http_version 1.1;\n'
    printf '        proxy_set_header Host $host;\n'
    printf '        proxy_set_header X-Real-IP $remote_addr;\n'
    printf '        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n'
    printf '        proxy_set_header X-Forwarded-Proto %s;\n' "$([ -n "$cert" ] && printf 'https' || printf 'http')"
    printf '        proxy_set_header Connection "";\n'
    printf '        proxy_buffering off;\n'  # /api/v1/runs/{id}/log/stream is Server-Sent Events; buffering it defeats the point
    printf '    }\n'
    printf '}\n'
}

# SYNOS_NGINX_ROOT overrides /etc/nginx -- test-only (same convention as
# SYNOS_OS_RELEASE_FILE/SYNOS_MEMINFO_FILE above): never set for a real
# install, and the one thing that lets a test prove the site-writing path
# for real without ever touching this machine's actual nginx.
NGINX_ROOT=${SYNOS_NGINX_ROOT:-/etc/nginx}

nginx_site_dir() { [ -d "$NGINX_ROOT/sites-available" ] && printf 'sites-available\n' || printf 'conf.d\n'; }

nginx_site_path() {  # name
    if [ "$(nginx_site_dir)" = "sites-available" ]; then
        printf '%s/sites-available/%s\n' "$NGINX_ROOT" "$1"
    else
        printf '%s/conf.d/%s.conf\n' "$NGINX_ROOT" "$1"
    fi
}

# Writes the site, validates the *whole* nginx configuration with it in
# place, and only reloads on a pass -- on a failure, removes exactly the
# file (and sites-enabled symlink) this call itself just added and leaves
# every other site precisely as it was: "must not disturb an existing
# site" has to survive a bad certificate path or a typo here too, not only
# a script that never writes anything wrong.
write_and_validate_nginx_site() {  # name rendered
    local name=$1 rendered=$2 path tlog
    path=$(nginx_site_path "$name")
    tlog=$(mktemp)
    printf '%s\n' "$rendered" | sudo tee "$path.new.$$" >/dev/null && sudo mv "$path.new.$$" "$path"
    if [ "$(nginx_site_dir)" = "sites-available" ]; then
        sudo ln -sf "$path" "$NGINX_ROOT/sites-enabled/$name"
    fi
    if sudo nginx -t >"$tlog" 2>&1; then
        rm -f "$tlog"
        if command -v systemctl >/dev/null 2>&1; then
            sudo systemctl reload nginx 2>/dev/null || sudo nginx -s reload 2>/dev/null || warn "wrote $path but could not reload nginx; reload it yourself"
        else
            sudo nginx -s reload 2>/dev/null || warn "wrote $path but could not reload nginx; reload it yourself"
        fi
        say "wrote $path and reloaded nginx"
        return 0
    fi
    local reason
    reason=$(cat "$tlog" 2>/dev/null)
    rm -f "$tlog"
    sudo rm -f "$path" "$NGINX_ROOT/sites-enabled/$name" 2>/dev/null
    fail "nginx -t rejected the new site ($name); removed it again and reloaded nothing, so every other site is untouched. nginx said: $reason" 8
}

# ------------------------------------------------------------------- main
default_engine_root() {
    local here
    here=$(cd "$(dirname "$0")/.." 2>/dev/null && pwd) || return 1
    [ -f "$here/tools/catalog_conformance.py" ] || return 1
    printf '%s\n' "$here"
}

usage() { sed -n '2,161p' "$0" | sed 's/^# \{0,1\}//'; }

main() {
    local engine_root='' engine_repo=${SYNOS_ENGINE_REPO:-https://github.com/Synthot/SynOS-Studio} engine_ref=main \
          site_url='' catalog_url='' storage_path='' workdir='' \
          entries_per_run=5 jobs='' config_path=/etc/synos/conformance.yml overwrite_config=0 \
          dev_site_root='' dev_site_owner='' dev_site_mode='' \
          unit_dir=/etc/systemd/system skip_packages=0 arg
    local monitoring_app='' monitoring_repo='' monitoring_ssh_key='' monitoring_ref=main \
          monitoring_config=/etc/synos-forge/forge.toml monitoring_port=8420 monitoring_nginx_port=8421 \
          monitoring_nginx_location=/ monitoring_allow_from='' monitoring_allow_insecure_http=0 \
          monitoring_tls_cert='' monitoring_tls_key='' monitor_auth_file='' monitor_auth_user=admin
    local -a SUMMARY=()

    for arg in "$@"; do
        case "$arg" in
            --engine-root=*)     engine_root=${arg#*=} ;;
            --engine-repo=*)     engine_repo=${arg#*=} ;;
            --engine-ref=*)      engine_ref=${arg#*=} ;;
            --site-url=*)        site_url=${arg#*=} ;;
            --catalog-url=*)     catalog_url=${arg#*=} ;;
            --storage-path=*)    storage_path=${arg#*=} ;;
            --workdir=*)         workdir=${arg#*=} ;;
            --entries-per-run=*) entries_per_run=${arg#*=} ;;
            --jobs=*)            jobs=${arg#*=} ;;
            --config=*)          config_path=${arg#*=} ;;
            --overwrite-config)  overwrite_config=1 ;;
            --dev-site-root=*)   dev_site_root=${arg#*=} ;;
            --dev-site-owner=*)  dev_site_owner=${arg#*=} ;;
            --dev-site-mode=*)   dev_site_mode=${arg#*=} ;;
            --service-user=*)    SERVICE_USER=${arg#*=}; SERVICE_GROUP=${arg#*=} ;;
            --unit-dir=*)        unit_dir=${arg#*=} ;;
            --skip-packages)     skip_packages=1 ;;
            --monitoring-app=*)              monitoring_app=${arg#*=} ;;
            --monitoring-repo=*)             monitoring_repo=${arg#*=} ;;
            --monitoring-ssh-key=*)          monitoring_ssh_key=${arg#*=} ;;
            --monitoring-ref=*)              monitoring_ref=${arg#*=} ;;
            --monitoring-config=*)           monitoring_config=${arg#*=} ;;
            --monitoring-service-user=*)     MONITORING_SERVICE_USER=${arg#*=} ;;
            --monitoring-port=*)             monitoring_port=${arg#*=} ;;
            --monitoring-nginx-port=*)       monitoring_nginx_port=${arg#*=} ;;
            --monitoring-nginx-location=*)   monitoring_nginx_location=${arg#*=} ;;
            --monitoring-allow-from=*)       monitoring_allow_from=${arg#*=} ;;
            --monitoring-allow-insecure-http) monitoring_allow_insecure_http=1 ;;
            --monitoring-tls-cert=*)         monitoring_tls_cert=${arg#*=} ;;
            --monitoring-tls-key=*)          monitoring_tls_key=${arg#*=} ;;
            --monitor-auth-file=*)           monitor_auth_file=${arg#*=} ;;
            --monitor-auth-user=*)           monitor_auth_user=${arg#*=} ;;
            -y|--yes)             ASSUME_YES=1 ;;
            --dry-run)             DRY_RUN=1 ;;
            -h|--help)             usage; return 0 ;;
            *) fail "unrecognized option: $arg (--help for usage)" 2 ;;
        esac
    done

    [ "$(uname -s)" = "Linux" ] || fail "this installs a systemd service; run it on Linux" 2

    say "boot acceleration: $(kvm_status)"

    local family=''
    if [ "$skip_packages" = "1" ]; then
        say "--skip-packages: not detecting a distribution or installing anything."
    else
        family=$(require_family) || exit $?
        say "distribution family: $family ($(pkg_manager_for "$family"))"
        install_requirements "$family"
    fi
    command -v git >/dev/null 2>&1 \
        || fail "git is required to create or update the engine checkout but is not on PATH (pass --skip-packages only when every dependency, including git, is already installed)" 7

    if [ -z "$engine_root" ]; then
        engine_root=$(default_engine_root) \
            || fail "pass --engine-root=/path/to/clone/or/an/existing/checkout (this script was not found under a checkout's packaging/ directory, so it cannot infer one)" 2
    fi
    local engine_state
    engine_state=$(sync_git_checkout "$engine_root" "$engine_repo" "$engine_ref" "" "engine checkout") || exit $?
    [ -f "$engine_root/tools/catalog_conformance.py" ] \
        || fail "$engine_root does not look like a checkout of this engine (no tools/catalog_conformance.py)" 2
    say "engine checkout: $engine_root ($engine_state)"
    SUMMARY+=("engine checkout ($engine_root): $engine_state")

    local runtime
    runtime=$(detect_runtime)
    [ -n "$runtime" ] || fail "no podman or docker on PATH even after the install step; install one and rerun" 4
    say "container runtime: $runtime"

    local service_home="/var/lib/$SERVICE_USER"
    if [ "$DRY_RUN" = "1" ]; then
        say "[dry-run] would create system user $SERVICE_USER (home $service_home) and add it to the $(runtime_group "$runtime") group"
    else
        create_service_user "$SERVICE_USER" "$SERVICE_GROUP" "$service_home" "$runtime"
    fi
    maybe "create /etc/synos" sudo install -d /etc/synos

    # select_storage_path's own mkdir -p, for an explicit path that does not
    # exist yet, is a real filesystem write — under --dry-run that must not
    # happen (item 131/"prints every action without performing it"), so a
    # dry run measures the nearest existing ancestor instead (create=0)
    # rather than actually creating anything.
    local create_storage=1
    [ "$DRY_RUN" = "1" ] && create_storage=0

    if is_podman "$runtime"; then
        if [ -z "$storage_path" ]; then
            local picked
            picked=$(select_storage_path "$MIN_BUILD_GB" "" "$create_storage") || fail "could not find anywhere to put podman's storage" 5
            storage_path=${picked% *}
        else
            select_storage_path "$MIN_BUILD_GB" "$storage_path" "$create_storage" >/dev/null \
                || fail "$storage_path is not usable for podman's storage" 5
        fi
        if [ "$DRY_RUN" = "1" ]; then
            say "[dry-run] would configure podman's rootless storage.conf (graphroot) at $storage_path, owned by $SERVICE_USER"
        else
            configure_podman_storage "$service_home" "$storage_path"
        fi
    else
        docker_storage_note
        # Unlike podman's own storage (above), this pick is only ever used
        # to place the *working directory* on the biggest disk available —
        # docker's own image storage is untouched either way (docker_storage_note
        # already said so). So a person on docker still ends up with a
        # working, if imperfectly placed, configuration even when nothing
        # qualifies: a warning and a fallback to the service user's home,
        # never a refusal (item 135) — a podman install still fails outright
        # on the same case, above, because there storage_path is essential,
        # not merely a placement preference.
        if [ -z "$storage_path" ]; then
            local picked_for_workdir
            if picked_for_workdir=$(select_storage_path "$MIN_BUILD_GB" "" "$create_storage"); then
                storage_path=${picked_for_workdir% *}
            else
                warn "could not find a disk with enough room for the working directory; using $service_home instead (pass --workdir to choose one yourself)"
                storage_path=$service_home
            fi
        else
            select_storage_path "$MIN_BUILD_GB" "$storage_path" "$create_storage" >/dev/null \
                || warn "could not validate $storage_path; using it anyway (only the working directory's placement depends on it under docker)"
        fi
    fi

    # container_root in the written config is meaningful for podman only —
    # tools/bundle_launcher.sh refuses it outright under docker (one
    # daemon-wide storage setting, docker_storage_note above) — so it is
    # never written when docker is the runtime, even though storage_path
    # itself is still used, above and below, to place the working directory
    # on the biggest disk available.
    local config_container_root=""
    is_podman "$runtime" && config_container_root=$storage_path

    if [ -z "$workdir" ]; then
        workdir="$storage_path/synos-conformance"
    fi
    if [ -z "$site_url" ]; then
        say "no --site-url given; \"build\" mode will need one filled into $config_path before it can run (\"check\" mode does not need it)."
    fi
    if [ -z "$catalog_url" ] && [ -n "$site_url" ]; then
        catalog_url="${site_url%/}/data/catalog.json"
    fi
    [ -n "$catalog_url" ] || fail "pass --catalog-url (or --site-url, which one is derived from) — the service cannot run without one" 2

    if [ -z "$jobs" ]; then
        jobs=$(derive_jobs "$engine_root" "$storage_path")
        say "derived worker count: $jobs"
    fi

    local rendered
    rendered=$(write_config "$catalog_url" "$site_url" "$workdir" "$config_container_root" "$entries_per_run" "$jobs" \
                            "$SERVICE_USER" "$dev_site_root" "$dev_site_owner" "$dev_site_mode")
    write_or_skip_config "conformance config" "$config_path" "$rendered" "$overwrite_config"
    if [ "$DRY_RUN" != "1" ]; then
        sudo install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$workdir" || warn "could not create/chown $workdir"
    fi

    install_service_units "$engine_root/packaging/catalog-conformance" "$unit_dir" "$engine_root" "$PYTHON_BIN"
    SUMMARY+=("conformance systemd units: installed under $unit_dir (not enabled)")

    # ------------------------------------------------------- monitoring app
    # Entirely optional (this script's own header): only reachable when the
    # operator actually named a checkout or a repository for it. Neither
    # given at all -- the normal path for the test engine on its own --
    # means nothing below runs and nothing below is even mentioned.
    if [ -n "$monitoring_app" ] || [ -n "$monitoring_repo$monitoring_ssh_key" ]; then
        if [ -z "$monitoring_app" ]; then
            say "monitoring app: --monitoring-repo/--monitoring-ssh-key given without --monitoring-app=PATH (where to put or find the checkout); skipping. Add --monitoring-app=PATH to enable it."
            SUMMARY+=("monitoring app: skipped (no --monitoring-app path given)")
        else
            install_monitoring_app "$monitoring_app" "$monitoring_repo" "$monitoring_ssh_key" "$monitoring_ref" \
                "$engine_root" "$config_path" "$workdir" "$monitoring_config" "$monitoring_port" \
                "$monitoring_nginx_port" "$monitoring_nginx_location" "$monitoring_allow_from" \
                "$monitoring_allow_insecure_http" "$monitoring_tls_cert" "$monitoring_tls_key" \
                "$monitor_auth_file" "$monitor_auth_user" "$runtime" "$family"
        fi
    fi

    say ""
    say "summary:"
    local line
    for line in "${SUMMARY[@]}"; do
        say "  - $line"
    done
    say ""
    say "not done by this script, on purpose:"
    say "  - the Studio page itself (development or production) is deployed with the"
    say "    Studio repository's own build.py/deploy.sh, never from here."
    if [ -z "$site_url" ]; then
        say "  - fill in site_url in $config_path before running \"build\" mode (\"check\" mode does not need it)."
    fi
    if [ -n "$dev_site_root" ]; then
        say "  - fill in a real \"production\" upload destination in $config_path yourself (commented"
        say "    out, credentials never written by this installer)."
    fi

    if [ "$DRY_RUN" = "1" ]; then
        say ""
        say "[dry-run] skipping verification (nothing was actually installed)."
        return 0
    fi
    verify_installation "$runtime" "$storage_path" "$engine_root" "$config_path" "$PYTHON_BIN" "$MIN_STORE_GB"
}

# install_monitoring_app app_path app_repo app_ssh_key app_ref engine_root
#                        conformance_config conformance_workdir monitoring_config
#                        port nginx_port nginx_location allow_from allow_insecure_http
#                        tls_cert tls_key auth_file auth_user runtime family
#
# The whole optional half: sync-or-use the checkout, install it into its
# own virtualenv, wire its config at the same conformance workdir/config
# this same run just set up, install its systemd unit (loopback only,
# always), and -- only when nginx is actually on this machine -- put an
# authenticated site in front of it. Every refusal in here (a bad
# checkout, no way to authenticate the site, plain HTTP beyond loopback
# without the acknowledgement) uses exit code 8, distinct from every
# other section's own codes above.
install_monitoring_app() {
    local app_path=$1 app_repo=$2 app_ssh_key=$3 app_ref=$4 engine_root=$5 \
          conformance_config=$6 conformance_workdir=$7 monitoring_config=$8 port=$9 \
          nginx_port=${10} nginx_location=${11} allow_from=${12} allow_insecure_http=${13} \
          tls_cert=${14} tls_key=${15} auth_file=${16} auth_user=${17} runtime=${18} family=${19}

    local app_root=$app_path
    if [ -n "$app_repo" ]; then
        if [ -z "$app_ssh_key" ]; then
            if [ ! -e "$app_root" ]; then
                fail "--monitoring-repo was given without --monitoring-ssh-key, and $app_root does not exist locally either -- a private repository needs a credential this installer will not prompt for. Pass --monitoring-ssh-key=PATH, or point --monitoring-app at an existing checkout." 8
            fi
            warn "monitoring app: --monitoring-repo was given without --monitoring-ssh-key; not syncing it (a private repository needs a credential this installer will not prompt for). Using $app_root as found."
            SUMMARY+=("monitoring app ($app_root): not synced (repo given without ssh key)")
        else
            local app_state
            app_state=$(sync_git_checkout "$app_root" "$app_repo" "$app_ref" "$app_ssh_key" "monitoring app") || exit $?
            say "monitoring app: $app_root ($app_state)"
            SUMMARY+=("monitoring app ($app_root): $app_state")
        fi
    elif [ ! -e "$app_root" ]; then
        fail "--monitoring-app=$app_root does not exist and no --monitoring-repo was given to create it from" 8
    else
        say "monitoring app: using existing checkout at $app_root (pass --monitoring-repo to have this script keep it updated)."
        SUMMARY+=("monitoring app ($app_root): used as-is (no --monitoring-repo given)")
    fi

    looks_like_monitoring_app_checkout "$app_root" \
        || fail "$app_root does not look like a checkout of the monitoring app (no forge/app.py)" 8

    if [ "$skip_packages" != "1" ]; then
        ensure_python_venv_available "$family"
    fi

    local monitoring_home="/var/lib/$MONITORING_SERVICE_USER"
    if [ "$DRY_RUN" = "1" ]; then
        say "[dry-run] would create system user $MONITORING_SERVICE_USER (home $monitoring_home) and add it to the $(runtime_group "$runtime") group"
        say "[dry-run] would create a virtualenv at $app_root/.venv and install the monitoring app into it"
    else
        create_service_user "$MONITORING_SERVICE_USER" "$MONITORING_SERVICE_USER" "$monitoring_home" "$runtime"
        sudo install -d -o "$MONITORING_SERVICE_USER" -g "$MONITORING_SERVICE_USER" "$monitoring_home" \
            || warn "could not create/chown $monitoring_home"
        maybe "create a virtualenv at $app_root/.venv and install the monitoring app into it" \
            _install_forge_venv "$app_root"
    fi
    SUMMARY+=("monitoring app virtualenv ($app_root/.venv): $([ "$DRY_RUN" = "1" ] && printf 'would create' || printf 'created/updated')")

    local forge_rendered
    forge_rendered=$(render_forge_config "$engine_root" "$conformance_config" "$conformance_workdir" "$port" "$MONITORING_SERVICE_USER")
    write_or_skip_config "monitoring app config" "$monitoring_config" "$forge_rendered" "$overwrite_config"

    if [ "$DRY_RUN" = "1" ]; then
        say "[dry-run] would install $unit_dir/synos-forge.service (WorkingDirectory=$app_root, SYNOS_FORGE_CONFIG=$monitoring_config, port $port, loopback only)"
        SUMMARY+=("monitoring app systemd unit: would install")
    else
        local forge_unit_tmp="$unit_dir/synos-forge.service.new.$$"
        render_forge_unit_file "$app_root/packaging/synos-forge.service" "$forge_unit_tmp" \
            "$app_root" "$monitoring_config" "$MONITORING_SERVICE_USER" "$port"
        mv "$forge_unit_tmp" "$unit_dir/synos-forge.service"
        sudo systemctl daemon-reload 2>/dev/null || warn "systemctl daemon-reload failed; is systemd running?"
        say "installed $unit_dir/synos-forge.service (127.0.0.1:$port only; not enabled -- sudo systemctl enable --now synos-forge.service when ready)"
        SUMMARY+=("monitoring app systemd unit: installed under $unit_dir (not enabled)")
    fi

    # ---- nginx: additive site, or a clean, honest "not now" ----
    if ! command -v nginx >/dev/null 2>&1; then
        say "monitoring app: nginx is not installed; the app is reachable only on 127.0.0.1:$port (an SSH tunnel, same as its own README's \"Exposing it safely\")."
        if [ -n "$family" ]; then
            say "  install nginx later, then rerun this installer with the same --monitoring-* flags to add the site: sudo $(run_package_manager_preview "$family") install nginx"
        else
            say "  install nginx for your distribution, then rerun this installer with the same --monitoring-* flags to add the site."
        fi
        SUMMARY+=("monitoring nginx site: skipped (nginx not installed)")
        return 0
    fi

    local auth_user_file=''
    if [ -n "$auth_file" ]; then
        [ -f "$auth_file" ] || fail "--monitor-auth-file=$auth_file does not exist" 8
        auth_user_file=$auth_file
        say "monitoring nginx site: using the given htpasswd file ($auth_file)"
        SUMMARY+=("monitoring auth: using existing file ($auth_file)")
    else
        # Whether this installer even *can* authenticate the site is a
        # hard prerequisite, checked here regardless of --dry-run, the
        # same as --catalog-url or an unrecognized distribution above --
        # never only discovered on a real run.
        command -v openssl >/dev/null 2>&1 \
            || fail "no --monitor-auth-file given and 'openssl' is not on PATH to generate one; install openssl (or apache2-utils' htpasswd) or pass --monitor-auth-file=PATH -- this installer will not publish the monitoring console without authentication" 8
        auth_user_file=${SYNOS_MONITOR_HTPASSWD_PATH:-/etc/synos/monitor.htpasswd}
        if [ "$DRY_RUN" = "1" ]; then
            say "[dry-run] would generate a random password for '$auth_user' and write its htpasswd hash to $auth_user_file (the plaintext password is shown once, on the terminal, at install time -- never written to a file, a log, or the systemd unit)"
            SUMMARY+=("monitoring auth: would generate a credential")
        else
            local password
            password=$(generate_monitor_credential "$auth_user_file" "$auth_user")
            say ""
            say "monitoring console credentials (shown once -- never written to a log or file besides the hash above):"
            say "  user:     $auth_user"
            say "  password: $password"
            say ""
            SUMMARY+=("monitoring auth: generated a credential for '$auth_user' ($auth_user_file)")
        fi
    fi

    local listen_addr=127.0.0.1 allow_cidr=''
    if [ -n "$allow_from" ]; then
        listen_addr=0.0.0.0
        allow_cidr=$allow_from
    fi
    local tls=0
    if [ -n "$tls_cert" ] || [ -n "$tls_key" ]; then
        [ -n "$tls_cert" ] && [ -n "$tls_key" ] || fail "both --monitoring-tls-cert and --monitoring-tls-key are required together" 8
        [ -f "$tls_cert" ] || fail "--monitoring-tls-cert=$tls_cert does not exist" 8
        [ -f "$tls_key" ] || fail "--monitoring-tls-key=$tls_key does not exist" 8
        tls=1
    fi
    if [ "$listen_addr" != "127.0.0.1" ] && [ "$tls" != "1" ] && [ "$allow_insecure_http" != "1" ]; then
        fail "--monitoring-allow-from=$allow_from would serve the monitoring console over plain HTTP beyond loopback; pass --monitoring-allow-insecure-http to accept that, or provide --monitoring-tls-cert and --monitoring-tls-key for TLS instead" 8
    fi

    local site_rendered
    site_rendered=$(render_monitor_nginx_site "$listen_addr" "$nginx_port" "$nginx_location" "$auth_user_file" "$port" \
                                              "$allow_cidr" "$([ "$tls" = 1 ] && printf '%s' "$tls_cert")" "$tls_key")
    if [ "$DRY_RUN" = "1" ]; then
        say "[dry-run] would write $(nginx_site_path synos-monitor):"
        printf '%s\n' "$site_rendered" | sed 's/^/  /'
        say "[dry-run] would validate with 'nginx -t' and reload nginx only on a pass"
        SUMMARY+=("monitoring nginx site: would write $(nginx_site_path synos-monitor)")
    else
        write_and_validate_nginx_site synos-monitor "$site_rendered"
        SUMMARY+=("monitoring nginx site: $(nginx_site_path synos-monitor) ($([ "$tls" = 1 ] && printf https || printf http), $([ -n "$allow_cidr" ] && printf "$allow_cidr" || printf loopback))")
    fi
}

# Preview text only (never actually run): names the package manager
# invocation the nginx-not-installed message shows so the operator has
# the one real command to run later, without this function itself
# installing anything.
run_package_manager_preview() { pkg_manager_for "$1" 2>/dev/null; }

# Only runs main when executed, never when sourced (as every test in
# tests/unit/ does, to call the functions above directly against fakes).
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
    main "$@"
fi
