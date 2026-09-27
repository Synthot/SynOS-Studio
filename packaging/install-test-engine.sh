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
# Running it with no arguments at all, on a terminal:
#   an interactive menu, in this same script -- one thing to run, one thing
#   to maintain. Each entry says where that thing stands on this machine
#   (installed, out of date, half installed, not installed, not
#   applicable), all of it read off the machine itself -- the unit files
#   actually in --unit-dir, what git says about the checkout, whether the
#   configuration's keys are filled in, the nginx site on disk, what
#   systemctl says about the timers -- and never from a marker file this
#   script wrote. Choosing an entry asks only for what that entry needs and
#   does only that entry's work. One entry, "everything for a staging
#   server", runs the sensible set in order; one entry does the single
#   action no flag has ever had, enabling the timers.
#
#   Plain shell on purpose: a numbered list, a prompt, and a loop until you
#   choose to finish. No whiptail, no dialog, no colour, no cursor tricks,
#   so it reads the same over a slow ssh link as on a console and the only
#   path that exists is the path that is tested.
#
#   The menu relaxes nothing. It collects answers and calls the same
#   functions the flags call, so the never-overwrite rule for a filled-in
#   configuration (still --overwrite-config only), the refusal to touch a
#   dirty or unexpected checkout, "nginx -t" before any reload, and every
#   exit code hold identically from either side. A secret typed at its one
#   secret prompt is not echoed, never logged, never written into a systemd
#   unit or a checkout (a configuration inside a git checkout is refused a
#   password outright), and the configuration file it lands in keeps the
#   mode it already had; decline it and the destination is written
#   commented out with the one line to edit named in the closing report.
#
#   --dry-run on its own still opens the menu, and every choice then prints
#   what it would do and changes nothing; so does --overwrite-config on its
#   own, which is how a configuration that has since been edited gets
#   replaced from here (and only then). -y/--yes, no terminal at all, or
#   any other flag: no menu, exactly the behaviour this script always had.
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

# The production upload destination, rendered from the PROD_* answers the
# menu collects (no flag sets them, so a flag-driven run renders exactly
# the commented-out template it always did). `prefix` is "# " when no
# credential was given: every answer is still written down, in full, as a
# comment, so the one thing left to do by hand is the secret itself.
#
# PROD_PASSWORD, when there is one, is printed straight into the YAML this
# returns -- never passed to sed, awk or any other process, so it cannot
# appear in `ps`, and never anywhere but the configuration file
# write_or_skip_config then writes.
render_production_destination() {  # prefix
    local prefix=${1:-}
    printf '%s  - name: %s\n' "$prefix" "$PROD_NAME"
    printf '%s    protocol: %s\n' "$prefix" "$PROD_PROTOCOL"
    printf '%s    host: %s\n' "$prefix" "$(yaml_scalar "$PROD_HOST")"
    [ -n "$PROD_PORT" ] && printf '%s    port: %s\n' "$prefix" "$PROD_PORT"
    [ -n "$PROD_USERNAME" ] && printf '%s    username: %s\n' "$prefix" "$(yaml_scalar "$PROD_USERNAME")"
    printf '%s    remote_path: %s\n' "$prefix" "$(yaml_scalar "$PROD_REMOTE_PATH")"
    [ -n "$PROD_KEY_PATH" ] && printf '%s    key_path: %s   # mode 600, owned by the service user\n' "$prefix" "$(yaml_scalar "$PROD_KEY_PATH")"
    [ -n "$PROD_PASSWORD" ] && printf '%s    password: %s\n' "$prefix" "$(yaml_scalar "$PROD_PASSWORD")"
    printf '%s    retries: 3\n' "$prefix"
    printf '%s    retry_backoff_s: 5\n' "$prefix"
    return 0
}

# Writes the configuration from answers, never from a template left for a
# person to edit blind. A credential reaches this file from one place only
# -- the menu's own secret prompt, which never echoes it -- and never from
# a flag, an environment variable or a file read here: with no production
# answers given, the upload section is the same fully commented-out
# template it has always been, naming where a real secret goes and what
# permissions it needs.
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
    if [ -n "$dev_site_root" ] || [ -n "$PROD_HOST" ]; then
        if [ -n "$dev_site_root" ]; then
            printf '# Rehearsal-then-production upload list (tools/status_uploader.py,\n'
            printf '# docs/BUILD_MATRIX.md'"'"'s "Publishing the build-status badge"): "dev-site" below is a\n'
            printf '# plain file copy to this machine'"'"'s own development Studio site -- not a network\n'
            printf '# call at all -- published, then fetched back from site_url above and checked to\n'
            printf '# actually be valid *as served* before anything further down this list is even\n'
            printf '# attempted. A rehearsal failure is reported and never touches production.\n'
            printf '#\n'
        else
            printf '# Upload list (tools/status_uploader.py). No rehearsal destination: name this\n'
            printf '# machine'"'"'s own development site (--dev-site-root, or the menu'"'"'s choice 4) to have\n'
            printf '# every publish rehearsed and fetched back there before production is touched.\n'
            printf '#\n'
        fi
        if [ -n "$PROD_HOST" ]; then
            if [ "$PROD_CREDENTIAL_GIVEN" = "1" ]; then
                printf '# The "production" destination below was answered at install time. Its secret --\n'
                printf '# a password here, or the key file named here -- is the one credential this\n'
                printf '# installer ever writes, and only because it was typed at its own prompt:\n'
                printf '# keep this file mode 600 and owned by %s.\n' "$service_user"
            else
                printf '# The "production" destination below was answered at install time, except for its\n'
                printf '# credential, which was declined: uncomment it and add key_path: (a private key,\n'
                printf '# mode 600, owned by %s) or password:. Nothing publishes to it until then.\n' "$service_user"
            fi
        else
            printf '# Fill in a real "production" destination yourself, outside any checkout, the same\n'
            printf '# way the commented-out example below shows -- the actual secret (a password, or\n'
            printf '# better, an unencrypted ssh private key) goes in its own file, owned by %s,\n' "$service_user"
            printf '# mode 600 -- never in this file, and never written by this installer.\n'
        fi
        printf 'upload:\n'
        if [ -n "$dev_site_root" ]; then
            printf '  - name: dev-site\n'
            printf '    protocol: local\n'
            printf '    remote_path: %s\n' "$(yaml_scalar "${dev_site_root%/}/data/build-status.json")"
            if [ -n "$dev_site_owner" ]; then
                printf '    owner: %s\n' "$(yaml_scalar "$dev_site_owner")"
            fi
            if [ -n "$dev_site_mode" ]; then
                printf '    mode: %s\n' "$(yaml_scalar "$dev_site_mode")"
            fi
        fi
        if [ -n "$PROD_HOST" ]; then
            if [ "$PROD_CREDENTIAL_GIVEN" = "1" ]; then
                render_production_destination ''
            else
                render_production_destination '# '
            fi
        else
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
        fi
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
        # A dry run prints the file it would write, so any password line in
        # it is blanked here rather than echoed to a terminal (or a tee, or
        # an ssh scrollback). The menu never even asks for a secret under
        # --dry-run; this is the second lock on the same door.
        printf '%s\n' "$rendered" \
            | sed 's/^\([[:space:]]*#\{0,1\}[[:space:]]*password:\).*/\1 <not shown>/' \
            | sed 's/^/  /'
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
            # Written beside it and moved into place (never truncated in
            # place, so a crash mid-write cannot leave a half-file), with
            # the mode the file already has carried over: replacing a
            # configuration must never loosen one an operator tightened to
            # 600 because it holds a credential.
            printf '%s\n' "$rendered" > "$path.new.$$" || { rm -f "$path.new.$$"; warn "could not write $path"; return 1; }
            chmod --reference="$path" "$path.new.$$" 2>/dev/null \
                || warn "could not copy $path's own mode onto its replacement; check it with 'ls -l $path'"
            mv "$path.new.$$" "$path"
            say "overwrote $path (--overwrite-config, keeping its mode)"
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

# The header comment above, up to the first line that is not a comment --
# never a hardcoded line range, which silently truncates --help the moment
# anything is added to that header (it did).
usage() { sed -n '2,/^[^#]/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; }

# ------------------------------------------------------------- the answers
# Every answer this script works from, in one place, because there are now
# two ways to give them -- the flags (parse_args below) and the interactive
# menu (further down) -- and exactly one set of step functions carries them
# out. A step reads these; it never reads a flag or a prompt itself, so the
# two front ends cannot drift apart in what they actually do.
#
# The SYNOS_* overrides are the same test-only convention as
# SYNOS_NGINX_ROOT/SYNOS_OS_RELEASE_FILE above (a real install passes
# --config/--unit-dir instead, and the menu asks for nothing it can read
# off the machine): never set for a real run.
ENGINE_ROOT=${SYNOS_ENGINE_ROOT:-}
ENGINE_REPO=${SYNOS_ENGINE_REPO:-https://github.com/Synthot/SynOS-Studio}
ENGINE_REF=main
SITE_URL=''
CATALOG_URL=''
STORAGE_PATH=''
CONFIG_CONTAINER_ROOT=''
WORKDIR=''
ENTRIES_PER_RUN=5
JOBS=''
CONFIG_PATH=${SYNOS_CONFIG_PATH:-/etc/synos/conformance.yml}
OVERWRITE_CONFIG=0
DEV_SITE_ROOT=''
DEV_SITE_OWNER=''
DEV_SITE_MODE=''
UNIT_DIR=${SYNOS_UNIT_DIR:-/etc/systemd/system}
SKIP_PACKAGES=0
FAMILY=''
RUNTIME=''
SERVICE_HOME=''

MONITORING_APP=''
MONITORING_REPO=''
MONITORING_SSH_KEY=''
MONITORING_REF=main
MONITORING_CONFIG=${SYNOS_MONITORING_CONFIG:-/etc/synos-forge/forge.toml}
MONITORING_PORT=8420
MONITORING_NGINX_PORT=8421
MONITORING_NGINX_LOCATION=/
MONITORING_ALLOW_FROM=''
MONITORING_ALLOW_INSECURE_HTTP=0
MONITORING_TLS_CERT=''
MONITORING_TLS_KEY=''
MONITOR_AUTH_FILE=''
MONITOR_AUTH_USER=admin

# The production upload destination: menu-only answers. No flag has ever
# written a real destination and none does now -- write_config still emits
# a commented-out template when these are empty, exactly as before.
#
# PROD_PASSWORD is the one secret this script ever holds. It is read with
# `read -s` (never echoed), lives in this variable and in the rendered
# configuration and nowhere else: never a command line (so never visible in
# `ps`), never a log, never a systemd unit, never printed back, and never
# accepted at all for a configuration that sits inside a git checkout.
PROD_NAME=production
PROD_PROTOCOL=sftp
PROD_HOST=''
PROD_PORT=22
PROD_USERNAME=''
PROD_REMOTE_PATH=''
PROD_KEY_PATH=''
PROD_PASSWORD=''
PROD_CREDENTIAL_GIVEN=0
PROD_PASSWORD_ALREADY_IN_CONFIG=0

# What the closing report says happened, and what is left to do by hand.
SUMMARY=()
HANDWORK=()
MENU_VERIFY=0

parse_args() {
    local arg
    for arg in "$@"; do
        case "$arg" in
            --engine-root=*)     ENGINE_ROOT=${arg#*=} ;;
            --engine-repo=*)     ENGINE_REPO=${arg#*=} ;;
            --engine-ref=*)      ENGINE_REF=${arg#*=} ;;
            --site-url=*)        SITE_URL=${arg#*=} ;;
            --catalog-url=*)     CATALOG_URL=${arg#*=} ;;
            --storage-path=*)    STORAGE_PATH=${arg#*=} ;;
            --workdir=*)         WORKDIR=${arg#*=} ;;
            --entries-per-run=*) ENTRIES_PER_RUN=${arg#*=} ;;
            --jobs=*)            JOBS=${arg#*=} ;;
            --config=*)          CONFIG_PATH=${arg#*=} ;;
            --overwrite-config)  OVERWRITE_CONFIG=1 ;;
            --dev-site-root=*)   DEV_SITE_ROOT=${arg#*=} ;;
            --dev-site-owner=*)  DEV_SITE_OWNER=${arg#*=} ;;
            --dev-site-mode=*)   DEV_SITE_MODE=${arg#*=} ;;
            --service-user=*)    SERVICE_USER=${arg#*=}; SERVICE_GROUP=${arg#*=} ;;
            --unit-dir=*)        UNIT_DIR=${arg#*=} ;;
            --skip-packages)     SKIP_PACKAGES=1 ;;
            --monitoring-app=*)              MONITORING_APP=${arg#*=} ;;
            --monitoring-repo=*)             MONITORING_REPO=${arg#*=} ;;
            --monitoring-ssh-key=*)          MONITORING_SSH_KEY=${arg#*=} ;;
            --monitoring-ref=*)              MONITORING_REF=${arg#*=} ;;
            --monitoring-config=*)           MONITORING_CONFIG=${arg#*=} ;;
            --monitoring-service-user=*)     MONITORING_SERVICE_USER=${arg#*=} ;;
            --monitoring-port=*)             MONITORING_PORT=${arg#*=} ;;
            --monitoring-nginx-port=*)       MONITORING_NGINX_PORT=${arg#*=} ;;
            --monitoring-nginx-location=*)   MONITORING_NGINX_LOCATION=${arg#*=} ;;
            --monitoring-allow-from=*)       MONITORING_ALLOW_FROM=${arg#*=} ;;
            --monitoring-allow-insecure-http) MONITORING_ALLOW_INSECURE_HTTP=1 ;;
            --monitoring-tls-cert=*)         MONITORING_TLS_CERT=${arg#*=} ;;
            --monitoring-tls-key=*)          MONITORING_TLS_KEY=${arg#*=} ;;
            --monitor-auth-file=*)           MONITOR_AUTH_FILE=${arg#*=} ;;
            --monitor-auth-user=*)           MONITOR_AUTH_USER=${arg#*=} ;;
            -y|--yes)             ASSUME_YES=1 ;;
            --dry-run)             DRY_RUN=1 ;;
            -h|--help)             usage; exit 0 ;;
            *) fail "unrecognized option: $arg (--help for usage)" 2 ;;
        esac
    done
}

# ------------------------------------------------------------- the steps
# main()'s old body, cut along the seams it already had, so that one item
# of the menu can run exactly one of them and the flags still run all of
# them in the same order as before. Every one of them reads the answers
# above and appends to SUMMARY; none of them prompts for anything (that is
# the menu's job) and none of them relaxes a guard (`fail` still exits with
# the same code from either front end).
step_report_kvm() { say "boot acceleration: $(kvm_status)"; }

step_packages() {
    if [ "$SKIP_PACKAGES" = "1" ]; then
        say "--skip-packages: not detecting a distribution or installing anything."
    else
        FAMILY=$(require_family) || exit $?
        say "distribution family: $FAMILY ($(pkg_manager_for "$FAMILY"))"
        install_requirements "$FAMILY"
    fi
    command -v git >/dev/null 2>&1 \
        || fail "git is required to create or update the engine checkout but is not on PATH (pass --skip-packages only when every dependency, including git, is already installed)" 7
}

step_engine_checkout() {
    if [ -z "$ENGINE_ROOT" ]; then
        ENGINE_ROOT=$(default_engine_root) \
            || fail "pass --engine-root=/path/to/clone/or/an/existing/checkout (this script was not found under a checkout's packaging/ directory, so it cannot infer one)" 2
    fi
    local engine_state
    engine_state=$(sync_git_checkout "$ENGINE_ROOT" "$ENGINE_REPO" "$ENGINE_REF" "" "engine checkout") || exit $?
    [ -f "$ENGINE_ROOT/tools/catalog_conformance.py" ] \
        || fail "$ENGINE_ROOT does not look like a checkout of this engine (no tools/catalog_conformance.py)" 2
    say "engine checkout: $ENGINE_ROOT ($engine_state)"
    SUMMARY+=("engine checkout ($ENGINE_ROOT): $engine_state")
}

step_runtime() {
    RUNTIME=$(detect_runtime)
    [ -n "$RUNTIME" ] || fail "no podman or docker on PATH even after the install step; install one and rerun" 4
    say "container runtime: $RUNTIME"
}

step_service_user() {
    SERVICE_HOME="/var/lib/$SERVICE_USER"
    if [ "$DRY_RUN" = "1" ]; then
        say "[dry-run] would create system user $SERVICE_USER (home $SERVICE_HOME) and add it to the $(runtime_group "$RUNTIME") group"
    else
        create_service_user "$SERVICE_USER" "$SERVICE_GROUP" "$SERVICE_HOME" "$RUNTIME"
    fi
    maybe "create /etc/synos" sudo install -d /etc/synos
}

step_storage() {
    # select_storage_path's own mkdir -p, for an explicit path that does not
    # exist yet, is a real filesystem write — under --dry-run that must not
    # happen (item 131/"prints every action without performing it"), so a
    # dry run measures the nearest existing ancestor instead (create=0)
    # rather than actually creating anything.
    local create_storage=1
    [ "$DRY_RUN" = "1" ] && create_storage=0

    if is_podman "$RUNTIME"; then
        if [ -z "$STORAGE_PATH" ]; then
            local picked
            picked=$(select_storage_path "$MIN_BUILD_GB" "" "$create_storage") || fail "could not find anywhere to put podman's storage" 5
            STORAGE_PATH=${picked% *}
        else
            select_storage_path "$MIN_BUILD_GB" "$STORAGE_PATH" "$create_storage" >/dev/null \
                || fail "$STORAGE_PATH is not usable for podman's storage" 5
        fi
        if [ "$DRY_RUN" = "1" ]; then
            say "[dry-run] would configure podman's rootless storage.conf (graphroot) at $STORAGE_PATH, owned by $SERVICE_USER"
        else
            configure_podman_storage "$SERVICE_HOME" "$STORAGE_PATH"
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
        # on the same case, above, because there STORAGE_PATH is essential,
        # not merely a placement preference.
        if [ -z "$STORAGE_PATH" ]; then
            local picked_for_workdir
            if picked_for_workdir=$(select_storage_path "$MIN_BUILD_GB" "" "$create_storage"); then
                STORAGE_PATH=${picked_for_workdir% *}
            else
                warn "could not find a disk with enough room for the working directory; using $SERVICE_HOME instead (pass --workdir to choose one yourself)"
                STORAGE_PATH=$SERVICE_HOME
            fi
        else
            select_storage_path "$MIN_BUILD_GB" "$STORAGE_PATH" "$create_storage" >/dev/null \
                || warn "could not validate $STORAGE_PATH; using it anyway (only the working directory's placement depends on it under docker)"
        fi
    fi

    # container_root in the written config is meaningful for podman only —
    # tools/bundle_launcher.sh refuses it outright under docker (one
    # daemon-wide storage setting, docker_storage_note above) — so it is
    # never written when docker is the runtime, even though STORAGE_PATH
    # itself is still used, above and below, to place the working directory
    # on the biggest disk available.
    CONFIG_CONTAINER_ROOT=''
    is_podman "$RUNTIME" && CONFIG_CONTAINER_ROOT=$STORAGE_PATH
    return 0
}

step_derive_answers() {
    if [ -z "$WORKDIR" ]; then
        WORKDIR="$STORAGE_PATH/synos-conformance"
    fi
    if [ -z "$SITE_URL" ]; then
        say "no --site-url given; \"build\" mode will need one filled into $CONFIG_PATH before it can run (\"check\" mode does not need it)."
    fi
    if [ -z "$CATALOG_URL" ] && [ -n "$SITE_URL" ]; then
        CATALOG_URL="${SITE_URL%/}/data/catalog.json"
    fi
    [ -n "$CATALOG_URL" ] || fail "pass --catalog-url (or --site-url, which one is derived from) — the service cannot run without one" 2

    if [ -z "$JOBS" ]; then
        JOBS=$(derive_jobs "$ENGINE_ROOT" "$STORAGE_PATH")
        say "derived worker count: $JOBS"
    fi
}

step_write_config() {
    local rendered
    rendered=$(write_config "$CATALOG_URL" "$SITE_URL" "$WORKDIR" "$CONFIG_CONTAINER_ROOT" "$ENTRIES_PER_RUN" "$JOBS" \
                            "$SERVICE_USER" "$DEV_SITE_ROOT" "$DEV_SITE_OWNER" "$DEV_SITE_MODE")
    write_or_skip_config "conformance config" "$CONFIG_PATH" "$rendered" "$OVERWRITE_CONFIG"
    if [ "$DRY_RUN" != "1" ]; then
        sudo install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$WORKDIR" || warn "could not create/chown $WORKDIR"
    fi
}

step_units() {
    install_service_units "$ENGINE_ROOT/packaging/catalog-conformance" "$UNIT_DIR" "$ENGINE_ROOT" "$PYTHON_BIN"
    SUMMARY+=("conformance systemd units: installed under $UNIT_DIR (not enabled)")
}

# Entirely optional (this script's own header): only reachable when the
# operator actually named a checkout or a repository for it. Neither given
# at all -- the normal path for the test engine on its own -- means nothing
# below runs and nothing below is even mentioned.
step_monitoring() {
    if [ -n "$MONITORING_APP" ] || [ -n "$MONITORING_REPO$MONITORING_SSH_KEY" ]; then
        if [ -z "$MONITORING_APP" ]; then
            say "monitoring app: --monitoring-repo/--monitoring-ssh-key given without --monitoring-app=PATH (where to put or find the checkout); skipping. Add --monitoring-app=PATH to enable it."
            SUMMARY+=("monitoring app: skipped (no --monitoring-app path given)")
        else
            install_monitoring_app "$MONITORING_APP" "$MONITORING_REPO" "$MONITORING_SSH_KEY" "$MONITORING_REF" \
                "$ENGINE_ROOT" "$CONFIG_PATH" "$WORKDIR" "$MONITORING_CONFIG" "$MONITORING_PORT" \
                "$MONITORING_NGINX_PORT" "$MONITORING_NGINX_LOCATION" "$MONITORING_ALLOW_FROM" \
                "$MONITORING_ALLOW_INSECURE_HTTP" "$MONITORING_TLS_CERT" "$MONITORING_TLS_KEY" \
                "$MONITOR_AUTH_FILE" "$MONITOR_AUTH_USER" "$RUNTIME" "$FAMILY"
        fi
    fi
}

step_summary() {
    say ""
    say "summary:"
    local line
    if [ "${#SUMMARY[@]}" -eq 0 ]; then
        say "  - nothing was created, updated or changed."
    else
        for line in "${SUMMARY[@]}"; do
            say "  - $line"
        done
    fi
    say ""
    say "not done by this script, on purpose:"
    say "  - the Studio page itself (development or production) is deployed with the"
    say "    Studio repository's own build.py/deploy.sh, never from here."
    if [ -z "$SITE_URL" ]; then
        say "  - fill in site_url in $CONFIG_PATH before running \"build\" mode (\"check\" mode does not need it)."
    fi
    # Named here only when no production destination was answered at all; a
    # destination given without its credential gets the one precise line
    # HANDWORK carries instead of this general one.
    if [ -n "$DEV_SITE_ROOT" ] && [ -z "$PROD_HOST" ]; then
        say "  - fill in a real \"production\" upload destination in $CONFIG_PATH yourself (commented"
        say "    out, credentials never written by this installer)."
    fi
    for line in ${HANDWORK[@]+"${HANDWORK[@]}"}; do
        say "  - $line"
    done
}

step_verify() {
    if [ "$DRY_RUN" = "1" ]; then
        say ""
        say "[dry-run] skipping verification (nothing was actually installed)."
        return 0
    fi
    verify_installation "$RUNTIME" "$STORAGE_PATH" "$ENGINE_ROOT" "$CONFIG_PATH" "$PYTHON_BIN" "$MIN_STORE_GB"
}

# The flag-driven run, unchanged in order and in output from what this
# script did before the menu existed.
run_full_install() {
    step_report_kvm
    step_packages
    step_engine_checkout
    step_runtime
    step_service_user
    step_storage
    step_derive_answers
    step_write_config
    step_units
    step_monitoring
    step_summary
    step_verify
}

# =================================================================== menu
# What happens when nobody passed any flags: a numbered list that says
# where each thing stands on *this* machine, a prompt, and a loop until the
# operator chooses to finish. Deliberately plain shell -- no whiptail, no
# dialog, no colour, no cursor addressing -- so the one path that exists is
# the one that is tested, and it reads the same over a slow ssh link as it
# does on a console.
#
# The menu only ever collects answers and calls the step functions above.
# It cannot relax a guard, because it does not contain one: the
# never-overwrite rule for a filled-in config, the refusal to touch a
# dirty or unexpected checkout, `nginx -t` before any reload and every
# exit code all live in the functions both front ends call.

# ---------------------------------------------------------- reading state
# Everything the menu reports is derived from the machine itself: a unit
# file that is really in UNIT_DIR, a checkout git itself answers for, a
# configuration with its keys actually filled in, an nginx site on disk.
# Never a marker file this script wrote -- a marker stays true long after
# the thing it claims has been removed by hand.
CONFORMANCE_UNITS='synos-conformance-build.service synos-conformance-build.timer synos-conformance-check.service synos-conformance-check.timer'
CONFORMANCE_TIMERS='synos-conformance-check.timer synos-conformance-build.timer'

# A top-level scalar out of the configuration this script writes. The key
# is always a literal name from a call site below, never anything read off
# the machine, so it is safe as part of sed's own program text.
config_value() {  # key path
    [ -f "$2" ] || return 1
    sed -n "s/^$1:[[:space:]]*//p" "$2" | head -n1 | sed 's/^"//; s/"$//'
}

# One field of the `protocol: local` (rehearsal) destination, or of the
# first destination that is not local (production), reading only
# uncommented lines -- the shape write_config itself produces. A config
# hand-rewritten into some other shape simply reads as "not configured",
# which is honest: this is a report about what is there, never an edit.
config_destination_field() {  # path field local|remote
    [ -f "$1" ] || return 1
    awk -v want="$2" -v which="$3" '
        /^[[:space:]]*#/ { next }
        /^[[:space:]]*-[[:space:]]*name:/ { inside = 0 }
        /^[[:space:]]*protocol:[[:space:]]*local[[:space:]]*$/ { inside = (which == "local"); next }
        /^[[:space:]]*protocol:[[:space:]]*(sftp|scp|ftps|ftp)[[:space:]]*$/ { inside = (which == "remote"); next }
        inside {
            key = $0
            sub(/^[[:space:]]+/, "", key)
            if (index(key, want ":") == 1) {
                val = substr(key, length(want) + 2)
                sub(/^[[:space:]]+/, "", val)
                gsub(/^"|"$/, "", val)
                print val
                exit
            }
        }
    ' "$1"
}

config_has_destination() {  # path local|remote
    [ -n "$(config_destination_field "$1" protocol "$2")$(config_destination_field "$1" remote_path "$2")" ]
}

unit_installed() { [ -f "$UNIT_DIR/$1" ]; }
unit_working_dir() { sed -n 's/^WorkingDirectory=//p' "$UNIT_DIR/$1" 2>/dev/null | head -n1; }
unit_enabled() { command -v systemctl >/dev/null 2>&1 && systemctl is-enabled --quiet "$1" 2>/dev/null; }

conformance_units_installed() {  # -> how many of the four are there
    local unit count=0
    for unit in $CONFORMANCE_UNITS; do
        unit_installed "$unit" && count=$((count + 1))
    done
    printf '%s\n' "$count"
}

state_prerequisites() {
    local missing runtime user='no service user yet'
    missing=$(missing_requirements)
    runtime=$(detect_runtime)
    id "$SERVICE_USER" >/dev/null 2>&1 && user="service user $SERVICE_USER present"
    if [ -z "$missing" ] && [ -n "$runtime" ]; then
        printf 'installed -- every dependency present, runtime %s, %s\n' "$runtime" "$user"
        return 0
    fi
    if [ -z "$missing" ]; then
        printf 'out of date -- dependencies present but no container runtime on PATH; %s\n' "$user"
        return 0
    fi
    printf 'not installed -- missing: %s(%s)\n' "$(printf '%s ' $missing)" "$user"
}

state_engine() {
    local root=$ENGINE_ROOT
    [ -n "$root" ] || root=$(default_engine_root 2>/dev/null) || root=''
    if [ -z "$root" ]; then
        printf 'not installed -- no checkout named yet\n'
        return 0
    fi
    if [ ! -e "$root" ] || { [ -d "$root" ] && [ -z "$(ls -A "$root" 2>/dev/null)" ]; }; then
        printf 'not installed -- nothing at %s yet (it would be cloned from %s)\n' "$root" "$ENGINE_REPO"
        return 0
    fi
    if ! git -C "$root" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        printf 'not applicable -- %s exists but is not a git checkout; used exactly as it is\n' "$root"
        return 0
    fi
    local rev branch behind
    rev=$(git_rev "$root")
    branch=$(git_branch "$root")
    if ! git_is_clean "$root"; then
        printf 'installed, untouchable -- %s (%s) has uncommitted changes; this script refuses to touch it\n' "$root" "${rev:-unknown}"
        return 0
    fi
    behind=$(git -C "$root" rev-list --count "HEAD..origin/$ENGINE_REF" 2>/dev/null)
    if is_number "$behind" && [ "$behind" -gt 0 ]; then
        printf 'out of date -- %s at %s (%s), %s commit(s) behind origin/%s\n' \
            "$root" "${rev:-unknown}" "${branch:-detached}" "$behind" "$ENGINE_REF"
        return 0
    fi
    printf 'installed -- %s at %s (%s)\n' "$root" "${rev:-unknown}" "${branch:-detached}"
}

state_config_and_units() {
    local units config='no' filled='' root
    units=$(conformance_units_installed)
    [ -f "$CONFIG_PATH" ] && config='yes'
    if [ "$config" = "no" ] && [ "$units" = "0" ]; then
        printf 'not installed -- no %s, none of the 4 units in %s\n' "$CONFIG_PATH" "$UNIT_DIR"
        return 0
    fi
    if [ "$config" = "yes" ] && [ -z "$(config_value catalog_url "$CONFIG_PATH")" ]; then
        filled=', not filled in (no catalog_url)'
    fi
    if [ "$config" = "no" ] || [ "$units" != "4" ]; then
        printf 'half installed -- configuration: %s%s, %s of 4 units in %s\n' "$config" "$filled" "$units" "$UNIT_DIR"
        return 0
    fi
    root=$(unit_working_dir synos-conformance-check.service)
    if [ -n "$ENGINE_ROOT" ] && [ -n "$root" ] && [ "$root" != "$ENGINE_ROOT" ]; then
        printf 'out of date -- 4 units in %s still point at %s, not %s\n' "$UNIT_DIR" "$root" "$ENGINE_ROOT"
        return 0
    fi
    printf 'installed%s -- %s and 4 units in %s\n' "$filled" "$CONFIG_PATH" "$UNIT_DIR"
}

state_dev_site() {
    local remote root
    [ -f "$CONFIG_PATH" ] || { printf 'not applicable -- no configuration yet\n'; return 0; }
    remote=$(config_destination_field "$CONFIG_PATH" remote_path local)
    [ -n "$remote" ] || { printf 'not configured -- the configuration has no rehearsal ("protocol: local") destination\n'; return 0; }
    root=${remote%/data/build-status.json}
    if [ ! -d "$root" ]; then
        printf 'out of date -- configured at %s, which does not exist on this machine yet\n' "$root"
        return 0
    fi
    printf 'installed -- %s (nginx serves it; deploy.sh fills it)\n' "$root"
}

state_publish() {
    local rehearsal production
    [ -f "$CONFIG_PATH" ] || { printf 'not applicable -- no configuration yet\n'; return 0; }
    rehearsal=$(config_destination_field "$CONFIG_PATH" remote_path local)
    production=$(config_destination_field "$CONFIG_PATH" host remote)
    if [ -n "$rehearsal" ] && [ -n "$production" ]; then
        printf 'installed -- rehearsal onto this box, then production at %s\n' "$production"
        return 0
    fi
    if [ -n "$rehearsal" ]; then
        printf 'half installed -- rehearsal only; nothing is published to a production server yet\n'
        return 0
    fi
    if [ -n "$production" ]; then
        printf 'half installed -- production at %s with no rehearsal to publish first\n' "$production"
        return 0
    fi
    printf 'not installed -- the build-status badge is published nowhere\n'
}

state_monitoring() {
    local unit='no' config='no' site='no' enabled='' path
    unit_installed synos-forge.service && unit='yes'
    [ -f "$MONITORING_CONFIG" ] && config='yes'
    path=$(nginx_site_path synos-monitor)
    [ -f "$path" ] && site='yes'
    if [ "$unit" = "no" ] && [ "$config" = "no" ] && [ "$site" = "no" ]; then
        printf 'not installed -- no unit, no configuration, no nginx site\n'
        return 0
    fi
    unit_enabled synos-forge.service && enabled=', enabled'
    if [ "$unit" = "yes" ] && [ "$config" = "yes" ] && [ "$site" = "yes" ]; then
        printf 'installed%s -- 127.0.0.1:%s behind %s\n' "$enabled" "$MONITORING_PORT" "$path"
        return 0
    fi
    printf 'half installed -- unit: %s%s, configuration: %s, nginx site: %s\n' "$unit" "$enabled" "$config" "$site"
}

state_timers() {
    local timer enabled=0 total=0
    if [ "$(conformance_units_installed)" != "4" ]; then
        printf 'not applicable -- the units are not installed yet\n'
        return 0
    fi
    for timer in $CONFORMANCE_TIMERS; do
        total=$((total + 1))
        unit_enabled "$timer" && enabled=$((enabled + 1))
    done
    if [ "$enabled" = "$total" ]; then
        printf 'installed -- both timers enabled\n'
        return 0
    fi
    if [ "$enabled" = "0" ]; then
        printf 'not installed -- units in place, both timers still disabled\n'
        return 0
    fi
    printf 'half installed -- %s of %s timers enabled\n' "$enabled" "$total"
}

# ------------------------------------------------------------- the prompts
# Every prompt in the menu goes through one of these five, so "shows the
# default it would use", "never echoes a secret" and "end of input means
# finish, never an endless loop" are each true in exactly one place.
#
# The prompt text is printed with printf to stderr rather than with
# `read -p`, for two reasons: `read -p` prints nothing at all when input is
# not a terminal (so a piped or scripted run, and every test of this menu,
# would show a bare answer with no question), and stderr keeps the prompt
# out of the $(...) capture these return their answer through.
menu_prompt() { printf '%s' "$*" >&2; }

menu_ask() {  # prompt default -> the answer (the default when nothing is typed)
    local prompt=$1 default=${2:-} reply
    while :; do
        menu_prompt "  $prompt [$default]: "
        read -r reply || { printf '\n' >&2; printf '%s\n' "$default"; return 0; }
        [ -n "$reply" ] || reply=$default
        if [ -z "$reply" ]; then
            printf '  a value is needed here.\n' >&2
            continue
        fi
        printf '%s\n' "$reply"
        return 0
    done
}

menu_ask_optional() {  # prompt default -> the answer, or nothing at all
    local prompt=$1 default=${2:-} reply
    menu_prompt "  $prompt [${default:-none}] (- for none): "
    read -r reply || { printf '\n' >&2; reply=''; }
    [ -n "$reply" ] || reply=$default
    [ "$reply" = "-" ] && reply=''
    printf '%s\n' "$reply"
}

menu_ask_number() {  # prompt default -> digits only
    local prompt=$1 default=${2:-} reply
    while :; do
        reply=$(menu_ask "$prompt" "$default")
        if is_number "$reply"; then
            printf '%s\n' "$reply"
            return 0
        fi
        printf '  that needs to be a number.\n' >&2
        [ -t 0 ] || { printf '%s\n' "$default"; return 0; }
    done
}

menu_ask_yes() {  # prompt -- end of input is "no", never a hang
    local reply
    menu_prompt "  $1 [y/N] "
    read -r reply || { printf '\n' >&2; return 1; }
    case "$reply" in
        y|Y|yes|YES) return 0 ;;
        *) return 1 ;;
    esac
}

# The one function in this script that reads a secret. `read -s`: nothing
# appears on the terminal, nothing reaches the scrollback of the ssh
# session, and the value is only ever assigned to a variable -- never
# passed as an argument to a command, where `ps` would show it.
menu_ask_secret() {  # prompt -> the secret on stdout, never on the terminal
    local reply
    menu_prompt "  $1: "
    read -r -s reply || reply=''
    printf '\n' >&2
    printf '%s\n' "$reply"
}

# A configuration inside a git checkout must never be handed a secret: the
# next `git status` there would show it and one `git add -A` would publish
# it. Checked before the password is even typed, not after it is written.
path_inside_checkout() {  # path
    local dir
    dir=$(dirname "$1")
    while [ -n "$dir" ] && [ "$dir" != "/" ] && [ "$dir" != "." ]; do
        [ -e "$dir/.git" ] && return 0
        dir=$(dirname "$dir")
    done
    return 1
}

# ------------------------------------------------------- prefilling answers
# A second run is someone managing a machine, so the prompts start from
# what is actually on it: the configuration already there, read back into
# the same answers a first run wrote it from. A password already in that
# file is deliberately *not* read back -- its presence is noted, so the
# menu can leave the file alone instead of rewriting it without one.
menu_load_config_answers() {
    [ -f "$CONFIG_PATH" ] || return 0
    local value
    value=$(config_value catalog_url "$CONFIG_PATH");       [ -n "$value" ] && [ -z "$CATALOG_URL" ] && CATALOG_URL=$value
    value=$(config_value site_url "$CONFIG_PATH");           [ -n "$value" ] && [ -z "$SITE_URL" ] && SITE_URL=$value
    value=$(config_value workdir "$CONFIG_PATH");            [ -n "$value" ] && [ -z "$WORKDIR" ] && WORKDIR=$value
    value=$(config_value jobs "$CONFIG_PATH");               [ -n "$value" ] && [ -z "$JOBS" ] && JOBS=$value
    value=$(config_value max_builds_per_run "$CONFIG_PATH"); [ -n "$value" ] && ENTRIES_PER_RUN=$value
    value=$(config_value container_root "$CONFIG_PATH")
    if [ -n "$value" ] && [ "$value" != "null" ] && [ -z "$STORAGE_PATH" ]; then
        STORAGE_PATH=$value
    fi
    value=$(config_destination_field "$CONFIG_PATH" remote_path local)
    case "$value" in
        */data/build-status.json) [ -n "$DEV_SITE_ROOT" ] || DEV_SITE_ROOT=${value%/data/build-status.json} ;;
    esac
    value=$(config_destination_field "$CONFIG_PATH" owner local);  [ -n "$value" ] && [ -z "$DEV_SITE_OWNER" ] && DEV_SITE_OWNER=$value
    value=$(config_destination_field "$CONFIG_PATH" mode local);   [ -n "$value" ] && [ -z "$DEV_SITE_MODE" ] && DEV_SITE_MODE=$value

    value=$(config_destination_field "$CONFIG_PATH" host remote);        [ -n "$value" ] && [ -z "$PROD_HOST" ] && PROD_HOST=$value
    value=$(config_destination_field "$CONFIG_PATH" protocol remote);    [ -n "$value" ] && PROD_PROTOCOL=$value
    value=$(config_destination_field "$CONFIG_PATH" port remote);        [ -n "$value" ] && PROD_PORT=$value
    value=$(config_destination_field "$CONFIG_PATH" username remote);    [ -n "$value" ] && [ -z "$PROD_USERNAME" ] && PROD_USERNAME=$value
    value=$(config_destination_field "$CONFIG_PATH" remote_path remote); [ -n "$value" ] && [ -z "$PROD_REMOTE_PATH" ] && PROD_REMOTE_PATH=$value
    value=$(config_destination_field "$CONFIG_PATH" key_path remote)
    if [ -n "$value" ] && [ -z "$PROD_KEY_PATH" ]; then
        PROD_KEY_PATH=$value
        PROD_CREDENTIAL_GIVEN=1
    fi
    [ -n "$(config_destination_field "$CONFIG_PATH" password remote)" ] && PROD_PASSWORD_ALREADY_IN_CONFIG=1
    return 0
}

# ------------------------------------------------------------- the display
menu_rule() { say '----------------------------------------------------------------------'; }

menu_print() {
    say ""
    menu_rule
    say " SynOS catalog test engine -- installer"
    say " machine: $(uname -n)   configuration: $CONFIG_PATH"
    if [ "$DRY_RUN" = "1" ]; then
        say " --dry-run: every choice below prints what it would do and does nothing"
    fi
    menu_rule
    say " 1) Prerequisites, container runtime, service user, runtime storage"
    say "      $(state_prerequisites)"
    say " 2) Engine checkout (clone or update, at a branch or tag)"
    say "      $(state_engine)"
    say " 3) Test engine configuration and systemd units"
    say "      $(state_config_and_units)"
    say " 4) Local development site nginx serves (the rehearsal copy)"
    say "      $(state_dev_site)"
    say " 5) Publish destinations (rehearsal here, then production)"
    say "      $(state_publish)"
    say " 6) Monitoring console (a private, separate repository)"
    say "      $(state_monitoring)"
    say " 7) Enable the timers"
    say "      $(state_timers)"
    say " 8) Everything for a staging server (1, 2, 4, 5, 3, then 6 and 7 if"
    say "    you want them, then verification)"
    say " 9) Show this again"
    say " q) Finish"
    menu_rule
}

# ------------------------------------------------------------- the choices
# Soft prerequisites: a choice that needs the runtime or the storage
# location works them out quietly if an earlier choice already did, and
# otherwise says which choice to make first instead of failing the whole
# menu.
menu_need_runtime() {
    [ -n "$RUNTIME" ] && return 0
    RUNTIME=$(detect_runtime)
    if [ -z "$RUNTIME" ]; then
        warn "no podman or docker on PATH yet; choose 1 first -- it installs the container runtime."
        return 1
    fi
    say "container runtime: $RUNTIME"
    [ -n "$SERVICE_HOME" ] || SERVICE_HOME="/var/lib/$SERVICE_USER"
    return 0
}

menu_need_storage() {
    menu_need_runtime || return 1
    [ -n "$STORAGE_PATH" ] && { is_podman "$RUNTIME" && CONFIG_CONTAINER_ROOT=$STORAGE_PATH; return 0; }
    step_storage
}

menu_need_engine_root() {
    [ -n "$ENGINE_ROOT" ] && return 0
    ENGINE_ROOT=$(default_engine_root 2>/dev/null) || ENGINE_ROOT=''
    [ -n "$ENGINE_ROOT" ] && return 0
    warn "no engine checkout named yet; choose 2 first."
    return 1
}

item_prerequisites() {
    say ""
    say "The dependencies this service needs, the container runtime, the dedicated"
    say "system user it runs as, and where the runtime keeps its images and chroot."
    STORAGE_PATH=$(menu_ask_optional "Runtime storage location (empty: the largest disk with room)" "$STORAGE_PATH")
    step_packages
    step_runtime
    step_service_user
    step_storage
    SUMMARY+=("prerequisites: dependencies checked, runtime $RUNTIME, storage $STORAGE_PATH")
}

item_engine() {
    say ""
    say "The checkout this service runs against: cloned when it is not there yet,"
    say "fetched and fast-forwarded when it is. Never reset, never force-checked-out,"
    say "and refused outright if it has uncommitted changes or sits on another branch."
    ENGINE_ROOT=$(menu_ask "Engine checkout path" "${ENGINE_ROOT:-$(default_engine_root 2>/dev/null)}")
    ENGINE_REPO=$(menu_ask "Clone or fetch it from" "$ENGINE_REPO")
    ENGINE_REF=$(menu_ask "Branch or tag to keep it on" "$ENGINE_REF")
    command -v git >/dev/null 2>&1 || { warn "git is not on PATH; choose 1 first."; return 1; }
    step_engine_checkout
}

item_config_and_units() {
    say ""
    say "The service's own configuration and its four systemd units. An existing"
    say "configuration that differs from these answers is reported and left exactly"
    say "as it is -- rerun with --overwrite-config to replace one on purpose."
    SITE_URL=$(menu_ask_optional "Studio page this rig drives (the development copy on this box)" "$SITE_URL")
    CATALOG_URL=$(menu_ask_optional "Catalog JSON URL (empty: derived from the page above)" "$CATALOG_URL")
    ENTRIES_PER_RUN=$(menu_ask_number "Catalog entries per build run" "$ENTRIES_PER_RUN")
    JOBS=$(menu_ask_optional "Worker count (empty: derived from this machine's disk, memory and CPUs)" "$JOBS")
    WORKDIR=$(menu_ask_optional "Service working directory (empty: alongside the runtime storage)" "$WORKDIR")
    if [ -z "$SITE_URL" ] && [ -z "$CATALOG_URL" ]; then
        # step_derive_answers would refuse this outright, with exit 2 and a
        # message about flags -- from the menu that means losing the whole
        # session over one unanswered question.
        warn "the service cannot run without the catalog JSON URL (or the Studio page it is derived from); nothing was written."
        return 1
    fi
    menu_need_engine_root || return 1
    menu_need_storage || return 1
    step_derive_answers
    step_write_config
    step_units
}

item_dev_site() {  # apply|defer
    say ""
    say "The development copy of the Studio page: nginx serves it, and the Studio"
    say "repository's own build.py/deploy.sh puts the page there -- this installer"
    say "never deploys it. Naming its document root is what makes the rehearsal"
    say "publish (a plain file copy into its data/ directory) possible."
    DEV_SITE_ROOT=$(menu_ask_optional "Development site document root" "${DEV_SITE_ROOT:-/var/www/synos-studio-dev}")
    if [ -n "$DEV_SITE_ROOT" ]; then
        [ -d "$DEV_SITE_ROOT" ] \
            || warn "$DEV_SITE_ROOT does not exist yet -- deploy the page there with the Studio repository's own deploy.sh, and point nginx at it."
        DEV_SITE_OWNER=$(menu_ask_optional "Owner for the copied badge (empty: leave ownership alone)" "${DEV_SITE_OWNER:-www-data:www-data}")
        DEV_SITE_MODE=$(menu_ask_optional "Mode for the copied badge (empty: leave the mode alone)" "${DEV_SITE_MODE:-0644}")
    fi
    [ "${1:-defer}" = "apply" ] && menu_apply_config
    return 0
}

item_publish() {  # apply|defer
    say ""
    say "Two destinations, in this order: the rehearsal copy onto this machine's own"
    say "development site, then the real production server -- and production is only"
    say "attempted once the rehearsal has been published and fetched back, valid, as"
    say "actually served."
    if [ -n "$DEV_SITE_ROOT" ]; then
        say "  rehearsal: ${DEV_SITE_ROOT%/}/data/build-status.json"
    else
        say "  rehearsal: none yet -- choose 4 to name the development site's root."
    fi
    if menu_ask_yes "Set up the production destination now?"; then
        while :; do
            PROD_PROTOCOL=$(menu_ask "Protocol (sftp, scp or ftps)" "$PROD_PROTOCOL")
            case "$PROD_PROTOCOL" in
                sftp|scp|ftps) break ;;
            esac
            warn "tools/status_uploader.py takes sftp, scp or ftps here (plain ftp travels in the clear and is refused outright unless a configuration says allow_insecure_ftp by hand)."
            [ -t 0 ] || { PROD_PROTOCOL=sftp; break; }
        done
        PROD_HOST=$(menu_ask "Host" "$PROD_HOST")
        PROD_PORT=$(menu_ask_number "Port" "$PROD_PORT")
        PROD_USERNAME=$(menu_ask "Username" "$PROD_USERNAME")
        PROD_REMOTE_PATH=$(menu_ask "Remote path to build-status.json" "${PROD_REMOTE_PATH:-/var/www/studio/data/build-status.json}")
        menu_ask_credential
    elif [ -n "$PROD_HOST" ]; then
        say "  production: leaving $PROD_HOST exactly as the configuration has it."
    fi
    [ "${1:-defer}" = "apply" ] && menu_apply_config
    return 0
}

# The only place a secret is ever asked for. An ssh key file is offered
# first and is the default, because it keeps every secret out of the
# configuration entirely; a typed password is never echoed, never logged,
# never put in a unit, and is refused outright for a configuration that
# lives inside a git checkout. Declining is a first-class answer: the
# destination is written commented out, with the one thing to edit named.
menu_ask_credential() {
    PROD_KEY_PATH=''
    PROD_PASSWORD=''
    PROD_CREDENTIAL_GIVEN=0
    say ""
    say "  How does this machine authenticate to $PROD_HOST?"
    say "    1) an ssh private key already on this machine (recommended: no secret"
    say "       is written into $CONFIG_PATH at all)"
    say "    2) a password, typed now -- not echoed, and stored in $CONFIG_PATH"
    say "    3) not now -- leave the destination commented out and tell me what to edit"
    local choice
    choice=$(menu_ask "Choose 1, 2 or 3" 3)
    case "$choice" in
        1)
            PROD_KEY_PATH=$(menu_ask "Path to the private key" "${PROD_KEY_PATH:-/etc/synos/conformance-upload-key}")
            if [ -n "$PROD_KEY_PATH" ]; then
                PROD_CREDENTIAL_GIVEN=1
                [ -f "$PROD_KEY_PATH" ] \
                    || HANDWORK+=("put the upload private key at $PROD_KEY_PATH, owned by $SERVICE_USER, mode 600 (this installer never creates one)")
            fi
            ;;
        2)
            if [ "$DRY_RUN" = "1" ]; then
                say "  [dry-run] would ask for that password here, without echoing it; nothing is typed, and nothing is written, on a dry run."
                return 0
            fi
            if path_inside_checkout "$CONFIG_PATH"; then
                warn "$CONFIG_PATH is inside a git checkout; refusing to put a password there (one 'git add -A' would publish it). Use an ssh key file outside the checkout, or point --config somewhere outside it."
                return 0
            fi
            local password confirm
            password=$(menu_ask_secret "Password for $PROD_USERNAME@$PROD_HOST (not echoed)")
            if [ -z "$password" ]; then
                warn "nothing typed; leaving the production destination commented out."
                return 0
            fi
            confirm=$(menu_ask_secret "The same again")
            if [ "$password" != "$confirm" ]; then
                warn "those two did not match; leaving the production destination commented out."
                return 0
            fi
            PROD_PASSWORD=$password
            PROD_CREDENTIAL_GIVEN=1
            say "  password accepted; it goes into $CONFIG_PATH and nowhere else."
            HANDWORK+=("$CONFIG_PATH now holds an upload password: chmod 600 and chown $SERVICE_USER on it (this installer does not change that file's mode)")
            ;;
        *)
            say "  leaving the production destination commented out."
            ;;
    esac
    if [ "$PROD_CREDENTIAL_GIVEN" != "1" ] && [ -n "$PROD_HOST" ]; then
        HANDWORK+=("uncomment the \"production\" destination in $CONFIG_PATH and give it key_path: (a private key, mode 600, owned by $SERVICE_USER) or password: -- this installer never invents a credential")
    fi
    return 0
}

# Writes the configuration from whatever answers are known, for the two
# choices whose whole job is one part of that file. Through
# write_or_skip_config like every other config write in this script, so
# "never clobber a filled-in config" holds from the menu too.
menu_apply_config() {
    menu_load_config_answers
    if [ -z "$CATALOG_URL" ] && [ -z "$SITE_URL" ]; then
        warn "no Studio page or catalog URL known yet, so there is nothing to write a configuration from. Your answers are remembered -- choose 3 (or 8) and they are used there."
        return 1
    fi
    if [ "$PROD_PASSWORD_ALREADY_IN_CONFIG" = "1" ] && [ -z "$PROD_PASSWORD" ]; then
        say "$CONFIG_PATH already holds this destination's password; leaving that file exactly as it is (type a new password in choice 5 to replace it)."
        return 0
    fi
    menu_need_engine_root || return 1
    menu_need_storage || return 1
    step_derive_answers
    step_write_config
}

item_monitoring() {
    say ""
    say "The monitoring console is a private, separate repository. It always binds"
    say "127.0.0.1 only; the nginx site in front of it is always behind HTTP basic"
    say "auth, and serving it beyond loopback over plain HTTP is refused unless you"
    say "say so outright."
    MONITORING_APP=$(menu_ask_optional "Checkout path for the monitoring app" "${MONITORING_APP:-/opt/synos-forge}")
    if [ -z "$MONITORING_APP" ]; then
        say "  skipped."
        return 0
    fi
    MONITORING_REPO=$(menu_ask_optional "Clone or fetch it from (empty: use the checkout as found)" "$MONITORING_REPO")
    if [ -n "$MONITORING_REPO" ]; then
        MONITORING_SSH_KEY=$(menu_ask_optional "SSH private key for that private repository" "$MONITORING_SSH_KEY")
        MONITORING_REF=$(menu_ask "Branch or tag to keep it on" "$MONITORING_REF")
    fi
    MONITORING_PORT=$(menu_ask_number "Loopback port the app itself binds" "$MONITORING_PORT")
    MONITORING_NGINX_PORT=$(menu_ask_number "Port nginx publishes it on" "$MONITORING_NGINX_PORT")
    MONITORING_ALLOW_FROM=$(menu_ask_optional "Address range allowed to reach it (empty: loopback only)" "$MONITORING_ALLOW_FROM")
    if [ -n "$MONITORING_ALLOW_FROM" ]; then
        MONITORING_TLS_CERT=$(menu_ask_optional "TLS certificate for that site (empty: none)" "$MONITORING_TLS_CERT")
        if [ -n "$MONITORING_TLS_CERT" ]; then
            MONITORING_TLS_KEY=$(menu_ask "TLS private key" "$MONITORING_TLS_KEY")
        elif menu_ask_yes "Without a certificate that console is served over plain HTTP beyond loopback. Accept that?"; then
            MONITORING_ALLOW_INSECURE_HTTP=1
        else
            say "  keeping it loopback-only."
            MONITORING_ALLOW_FROM=''
        fi
    fi
    MONITOR_AUTH_FILE=$(menu_ask_optional "Existing htpasswd file (empty: generate one and show the password once)" "$MONITOR_AUTH_FILE")
    if [ -z "$MONITOR_AUTH_FILE" ]; then
        MONITOR_AUTH_USER=$(menu_ask "User name for the generated credential" "$MONITOR_AUTH_USER")
    fi
    menu_need_engine_root || return 1
    menu_need_runtime || return 1
    menu_load_config_answers
    if [ -z "$WORKDIR" ]; then
        warn "the console reads its status files out of the service's working directory; choose 3 first so there is one."
        return 1
    fi
    [ -n "$FAMILY" ] || FAMILY=$(detect_distro_family) || FAMILY=''
    step_monitoring
}

# The one action the flags never had: they install the timers and print the
# command that turns them on. Still through `maybe`, so --dry-run only ever
# says what it would run.
item_enable_timers() {
    local timer
    say ""
    if [ "$(conformance_units_installed)" != "4" ]; then
        if [ "$DRY_RUN" != "1" ]; then
            warn "the four units are not all in $UNIT_DIR yet; choose 3 (or 8) first."
            return 1
        fi
        # A dry run installed nothing, so the units genuinely are not there;
        # refusing here would hide the one thing --dry-run is for -- saying
        # what this choice would do.
        say "[dry-run] nothing was installed by this run, so the units are not in $UNIT_DIR; what follows is what enabling them would do."
    fi
    say "The check timer looks for catalog entries to test; the build timer builds"
    say "them. Both are ordinary systemd timers you can stop again at any time."
    menu_ask_yes "Enable and start both conformance timers now?" || { say "  left disabled."; return 0; }
    for timer in $CONFORMANCE_TIMERS; do
        maybe "enable and start $timer" sudo systemctl enable --now "$timer" \
            || warn "could not enable $timer; 'sudo systemctl enable --now $timer' by hand"
    done
    if [ "$DRY_RUN" = "1" ]; then
        SUMMARY+=("conformance timers: would be enabled and started")
    else
        SUMMARY+=("conformance timers: enabled and started")
    fi
    return 0
}

item_everything() {
    say ""
    say "Everything for a staging server, in this order:"
    say "  1. prerequisites, container runtime, service user, runtime storage"
    say "  2. the engine checkout"
    say "  3. the development site nginx serves (its document root)"
    say "  4. the publish destinations (rehearsal here, then production)"
    say "  5. the configuration and the four systemd units, with 3 and 4 in it"
    say "  6. the monitoring console, if you want it"
    say "  7. enabling the timers, if you want them on now"
    say "  8. verification"
    menu_ask_yes "Go through those now?" || { say "  nothing done."; return 0; }
    item_prerequisites
    item_engine
    item_dev_site defer
    item_publish defer
    item_config_and_units
    if menu_ask_yes "Set up the monitoring console too (a private, separate repository)?"; then
        item_monitoring
    else
        SUMMARY+=("monitoring console: not chosen")
    fi
    item_enable_timers
    MENU_VERIFY=1
    return 0
}

# ------------------------------------------------------------- the loop
menu_loop() {
    say ""
    say "Nothing on this machine has been changed yet. Every choice below says where"
    say "it stands, asks for what it needs, and only then does its own one thing."
    [ -n "$ENGINE_ROOT" ] || ENGINE_ROOT=$(default_engine_root 2>/dev/null) || ENGINE_ROOT=''
    menu_load_config_answers
    local choice
    while :; do
        menu_print
        printf 'Choose 1-9, or q to finish: '
        read -r choice || { say ""; break; }
        case "$choice" in
            1) item_prerequisites ;;
            2) item_engine ;;
            3) item_config_and_units ;;
            4) item_dev_site apply ;;
            5) item_publish apply ;;
            6) item_monitoring ;;
            7) item_enable_timers ;;
            8) item_everything ;;
            9) ;;
            q|Q|quit|exit|'') break ;;
            # Deliberately without echoing what was typed: a password
            # meant for the secret prompt, mistyped here, must not end up
            # in this terminal's scrollback or in a piped log.
            *) warn "not one of the choices -- type 1-9, or q to finish." ;;
        esac
    done
    step_summary
    if [ "$MENU_VERIFY" = "1" ]; then
        step_verify
        return $?
    fi
    return 0
}

# The menu is what happens when nobody passed anything: an interactive
# terminal, no arguments at all, and no -y/--yes (which by definition means
# "do not ask me anything"). --dry-run and --overwrite-config still count as
# nothing passed: they are modifiers, not actions -- "--dry-run plus the
# menu" is how a person sees what each choice would do before choosing it,
# and --overwrite-config is the only way to replace a configuration that
# has since been edited, so it has to be reachable from here too (it still
# never overwrites anything on its own: the choice that writes that file
# has to be made). No terminal, or any other flag, and this script behaves
# exactly as it always did. SYNOS_MENU_ASSUME_TTY is the unit tests' own
# way in (the same convention as SYNOS_NGINX_ROOT above), never for a real
# run.
menu_should_run() {
    [ "$ASSUME_YES" = "1" ] && return 1
    local arg
    for arg in "$@"; do
        case "$arg" in
            --dry-run|--overwrite-config) ;;
            *) return 1 ;;
        esac
    done
    [ -t 0 ] || [ "${SYNOS_MENU_ASSUME_TTY:-0}" = "1" ]
}

main() {
    parse_args "$@"

    [ "$(uname -s)" = "Linux" ] || fail "this installs a systemd service; run it on Linux" 2

    if menu_should_run "$@"; then
        menu_loop
        return $?
    fi

    run_full_install
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

    if [ "$SKIP_PACKAGES" != "1" ]; then
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
    write_or_skip_config "monitoring app config" "$monitoring_config" "$forge_rendered" "$OVERWRITE_CONFIG"

    if [ "$DRY_RUN" = "1" ]; then
        say "[dry-run] would install $UNIT_DIR/synos-forge.service (WorkingDirectory=$app_root, SYNOS_FORGE_CONFIG=$monitoring_config, port $port, loopback only)"
        SUMMARY+=("monitoring app systemd unit: would install")
    else
        local forge_unit_tmp="$UNIT_DIR/synos-forge.service.new.$$"
        render_forge_unit_file "$app_root/packaging/synos-forge.service" "$forge_unit_tmp" \
            "$app_root" "$monitoring_config" "$MONITORING_SERVICE_USER" "$port"
        mv "$forge_unit_tmp" "$UNIT_DIR/synos-forge.service"
        sudo systemctl daemon-reload 2>/dev/null || warn "systemctl daemon-reload failed; is systemd running?"
        say "installed $UNIT_DIR/synos-forge.service (127.0.0.1:$port only; not enabled -- sudo systemctl enable --now synos-forge.service when ready)"
        SUMMARY+=("monitoring app systemd unit: installed under $UNIT_DIR (not enabled)")
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
