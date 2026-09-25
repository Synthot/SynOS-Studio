#!/bin/sh
# Builds this configuration bundle into an installable image.
#
#   ./build.sh            build; the ISO and its evidence land in ./dist
#   ./build.sh check      only check that this machine can build (installs nothing)
#   ./build.sh update     fetch the latest launcher scripts and replace these four files, then exit
#   ./build.sh reset-storage   remove this bundle's own container storage and forget where it was (dist/ is untouched)
#   ./build.sh --yes      answer yes to the questions (install the container runtime)
#   ./build.sh --channel=stable|development   override which engine pipeline builds this bundle
#   ./build.sh --storage /path   keep this build's images, layers and chroot there instead
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
# Channels: a bundle is generated for one of two engine pipelines, recorded
# in its own bundle.json as "channel" ("stable" when the key is absent, so
# every bundle that already exists keeps working exactly as it does today).
# `stable` builds against the newest *released* engine that satisfies this
# bundle (a GitHub release tag, never a branch tip). `development` builds
# against the unreleased tip of the engine's main branch, for testing a
# bundle against engine changes that have not shipped yet - the front end
# that generated the bundle decides this, typically by asking whether it is
# itself running from a deployed, released instance or a local/staging one.
# SYNOS_CHANNEL (environment) or --channel=stable|development (flag) override
# the bundle's own value, for a person who knows what they are doing; either
# one wins over what the bundle says. A development build says so plainly in
# this script's own output, in dist/build.log, and in the image it is built
# from, so nobody mistakes it for a released build.
#
# When no engine image can be pulled, this script builds one locally from
# the engine source, extracted once per source identity into its own
# directory under .build/engine-src/ (the stable channel's release tag, or a
# hash of the archive on development) and reused - not re-downloaded, not
# re-extracted - on every later run for that same identity, the same way
# the image itself is. A build context already containing podman's own
# storage (left there by a run before this existed) is refused by name
# rather than walked; that directory owns whatever a previous run left
# inside it too, root-owned files included, and is removed and re-extracted
# rather than silently reused when it does not check out clean.
#
# Where the build's bytes land needs nothing from you on podman: this script
# works out on its own that the disk to use is the one this bundle was
# unpacked to, and keeps images, layers and the chroot in .build/ right next
# to it - the same disk you already chose, whatever its size. It says so
# once, the run that creates that directory, and always as an absolute path
# even when a relative one was given (--storage storage becomes, and is
# reported as, /path/to/bundle/storage - the container runtime, possibly
# run through sudo, does not necessarily share this script's own working
# directory, so a relative path handed to it would be interpreted by
# whatever directory it starts in instead). --storage /path (or
# --storage=/path) puts it somewhere else instead, with no root and no
# daemon restart needed; SYNOS_CONTAINER_ROOT does the same for unattended
# use, where a flag is awkward (a person passes --storage; a service sets
# the variable). --container-root=/path still works, an older spelling of
# the same flag, for anyone who already scripted it. --container-runroot=/path
# (SYNOS_CONTAINER_RUNROOT) does the same for podman's small state directory,
# which otherwise stays at podman's own default. Docker's storage is set
# once for the whole daemon: there is no per-build override, and this script
# says so, with the steps to move it, instead of guessing.
#
# With podman, a real choice remains only when the bundle's own disk cannot
# back a container's overlay at all: then, and only then (a terminal, no
# --yes), this script asks once whether to use podman's own storage
# instead, and remembers the answer in .build/container-root (also an
# absolute path) so the next run is never asked again ("forget" it: rm
# .build/container-root). --storage/SYNOS_CONTAINER_ROOT/--container-root
# always win over the remembered value, and update it.
#
# A storage location that carries state from a different configuration than
# the one now being asked of it (moved by hand, or reused after a change of
# --storage) fails with podman's own line shown first, then explained,
# rather than an opaque build failure: `./build.sh reset-storage` removes
# this bundle's own container storage (wherever it is remembered to be, the
# default location, an extracted engine source, and the storage debris an
# older launcher could leave at the bundle's own root) and forgets where it
# was, so a wedged build is one command away from starting fresh - dist/
# (the build log and the evidence next to the ISO) is never touched by it.
#
# Environment: SYNOS_BUILDER_IMAGE (use another image), SYNOS_IMAGE_REPOSITORY
#   (another registry, default ghcr.io/synthot/synos-builder), SYNOS_ENGINE_SOURCE
#   (an engine checkout to build the image from), SYNOS_ENGINE_URL (source archive),
#   SYNOS_LAUNCHER_URL (source for `update`, default the engine's tools/ on GitHub),
#   SYNOS_CHANNEL=stable|development (override the bundle's own channel; see above),
#   SYNOS_NO_UPDATE_CHECK=1 (skip the launcher update check at the start of
#   check/build), SYNOS_YES=1 (same as --yes), SYNOS_CONTAINER_ROOT (podman
#   only, same as --storage; see above - unattended use, prefer --storage by
#   hand) and SYNOS_CONTAINER_RUNROOT (podman only; see above). When this
#   script needs sudo to run the container runtime, all of the above (plus
#   the signing key variables) are forwarded to it explicitly - sudo's own
#   environment reset would otherwise silently drop them even though this
#   script still has them.
set -eu
cd "$(dirname "$0")"

# Resolves a path to an absolute one, from this bundle's own directory (cd'd
# into, just above): a relative storage location or engine checkout is used
# again later - sometimes after this script itself changes directory,
# sometimes by the container runtime, run through sudo, where a relative
# path is interpreted by whatever directory that process ends up with,
# which this script does not control. Made absolute once, here, so it is
# unambiguous everywhere after: the runtime arguments, the free-space and
# overlay checks, the remembered value and every line that reports it back.
abs_path() {  # path
    case "$1" in
        /*) printf '%s\n' "$1" ;;
        *) printf '%s/%s\n' "$PWD" "$1" ;;
    esac
}

# Refuses a storage location inside a directory this script is about to
# archive, copy or hash whole: "COPY . /opt/synos" in the engine's own
# Containerfile walks everything under the engine source, storage included,
# and a build mid-write there fails deep in the build with a pipe or
# permission error instead of a clear refusal up front; the evidence files
# next to the ISO (the SBOM, the checksums) are generated from dist/ the
# same way. Not relied on: an ignore file (.containerignore) excludes
# things for its own reasons, not as a guarantee this depends on.
refuse_storage_inside() {  # context-dir context-description
    [ -n "$container_root" ] || return 0
    context=$(abs_path "$1")
    case "$container_root" in
        "$context"|"$context"/*)
            fail "the build's storage ($container_root) is inside $2 ($context), which this build copies, archives or hashes whole; point --storage at a location outside it" 2 ;;
    esac
}

# podman's own storage, once created anywhere, always has this shape at its
# own root - confirmed against a real, corrupted bundle, not guessed at:
# overlay/, overlay-containers/, overlay-images/, overlay-layers/, libpod/,
# db.sql, defaultNetworkBackend. Checked at the context's own root and one
# level inside it, since that is where a previous run's --storage (a name
# nothing here controls) actually landed in the field; prints the storage
# root's path on a match, so a caller can name it, and answers nothing (via
# its own exit status) otherwise.
find_storage_root_inside() {  # dir
    for candidate in "$1" "$1"/*/; do
        [ -d "$candidate" ] || continue
        for marker in overlay overlay-containers overlay-images overlay-layers libpod db.sql defaultNetworkBackend; do
            if [ -e "${candidate%/}/$marker" ]; then
                printf '%s\n' "${candidate%/}"
                return 0
            fi
        done
    done
    return 1
}
# Before COPY . /opt/synos (or an equivalent whole-directory read) can walk
# a context that already contains a storage root, wherever it came from:
# refuse and name both paths, plus the command to remove it. Independent of
# refuse_storage_inside above, which only stops *this* run's own storage
# from landing somewhere dangerous - this catches one a run before this fix
# already left behind.
refuse_existing_storage_in() {  # context-dir context-description
    context=$(abs_path "$1")
    found=$(find_storage_root_inside "$context") || return 0
    fail "$2 ($context) already contains podman's own storage at $found, left there by an earlier run; this build copies, archives or hashes it whole. Remove it first: rm -rf $found (root-owned files there may need: sudo rm -rf $found)" 2
}

# Removes a directory this build is about to recreate from scratch,
# tolerating root-owned files a previous run's container runtime left
# inside it (podman's own storage, unpacked --privileged). Plain rm -rf,
# run as the invoking user, cannot read into those; escalated exactly when
# this script already decided the runtime itself needs to be ($run_as) -
# never a fresh escalation of its own - and this fails with a clear reason,
# rather than leaving a half-removed tree, when even that cannot clear it.
clean_dir() {  # dir
    # Called only with .build/engine-src/<source-id>, and a source id is
    # either a release tag or a hash - but this function removes a tree, with
    # sudo when it has to, so it refuses anything that could reach outside
    # that directory rather than trusting its caller to have checked.
    case "$1" in
        .build/engine-src/?*) ;;
        *) fail "refusing to remove $1: only an extracted engine source under .build/engine-src/ is ever removed here" 2 ;;
    esac
    case "$1" in
        *../*|*/..|*//*) fail "refusing to remove $1: the path is not a plain .build/engine-src/<id>" 2 ;;
    esac
    [ -e "$1" ] || return 0
    rm -rf "$1" 2>/dev/null && return 0
    if [ -n "${run_as:-}" ]; then
        $run_as rm -rf "$1" 2>/dev/null && return 0
    fi
    fail "could not remove $1, left behind by an earlier build (likely root-owned container storage inside it); remove it yourself: sudo rm -rf $1" 2
}

[ -z "${SYNOS_ENGINE_SOURCE:-}" ] || SYNOS_ENGINE_SOURCE=$(abs_path "$SYNOS_ENGINE_SOURCE")

yes_to_all=${SYNOS_YES:-}
command_word=""
container_root=${SYNOS_CONTAINER_ROOT:-}
container_runroot=${SYNOS_CONTAINER_RUNROOT:-}
channel_flag=""
remembered_root_file=".build/container-root"
# A for loop, not a positional while/shift: "$@" must survive this parse
# intact, unconsumed - check_for_launcher_update below, and the two
# self-refresh restarts further down, all re-exec this script with "$@"
# exactly as it was invoked. --storage's two-token form is instead handled
# with a one-step lookahead flag.
storage_wants_value=""
for arg in "$@"; do
    if [ -n "$storage_wants_value" ]; then
        container_root=$arg
        storage_wants_value=""
        continue
    fi
    case "$arg" in
        --yes|-y) yes_to_all=1 ;;
        --storage=*) container_root=${arg#--storage=} ;;
        --storage) storage_wants_value=1 ;;
        --container-root=*) container_root=${arg#--container-root=} ;;
        --container-runroot=*) container_runroot=${arg#--container-runroot=} ;;
        --channel=*) channel_flag=${arg#--channel=} ;;
        check|build|update|reset-storage) command_word=$arg ;;
        -h|--help) sed -n '2,97p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) printf 'unknown argument: %s\n' "$arg" >&2; exit 1 ;;
    esac
done
[ -z "$storage_wants_value" ] || { printf 'unknown argument: --storage needs a path\n' >&2; exit 1; }
# A relative --storage/--container-root or SYNOS_CONTAINER_ROOT/RUNROOT must
# still work; it simply stops being relative the moment it is accepted (see
# abs_path above) - podman receives this as a bare argument, not something
# this script itself cd's into first, so it is the one place a relative
# value would otherwise be at the mercy of whatever directory the elevated
# runtime process happens to start in.
[ -z "$container_root" ] || container_root=$(abs_path "$container_root")
[ -z "$container_runroot" ] || container_runroot=$(abs_path "$container_runroot")

say() { printf '%s\n' "$*"; }
fail() { printf 'error: %s\n' "$1" >&2; exit "${2:-1}"; }
ask() {  # ask "question" -> returns 0 for yes
    [ -n "$yes_to_all" ] && return 0
    [ -t 0 ] || return 1
    printf '%s [y/N] ' "$1"
    read -r answer
    case "$answer" in y|Y|yes|YES|Yes) return 0 ;; *) return 1 ;; esac
}
ask_yes() {  # ask_yes "question" -> returns 0 unless the answer is explicitly no (default: yes)
    [ -n "$yes_to_all" ] && return 0
    [ -t 0 ] || return 1
    printf '%s [Y/n] ' "$1"
    read -r answer
    case "$answer" in n|N|no|NO|No) return 1 ;; *) return 0 ;; esac
}
field() { sed -n "s/^$2:[[:space:]]*\"\{0,1\}\([^\"#]*\)\"\{0,1\}.*/\1/p" "$1" | head -n 1 | sed 's/[[:space:]]*$//'; }
# ver_ge A B -> success (0) when dotted-integer version A is >= B; both may
# have any number of components (1.2 vs 1.2.0 vs 1.2.0.1), missing ones
# treated as 0. Pure POSIX parameter expansion, no bashisms, no `sort -V`
# (not available on macOS/BSD sort or a plain BusyBox system).
ver_ge() {
    a=$1; b=$2
    while [ -n "$a" ] || [ -n "$b" ]; do
        an=${a%%.*}; bn=${b%%.*}
        [ -n "$an" ] || an=0
        [ -n "$bn" ] || bn=0
        [ "$an" -gt "$bn" ] 2>/dev/null && return 0
        [ "$an" -lt "$bn" ] 2>/dev/null && return 1
        case "$a" in *.*) a=${a#*.} ;; *) a="" ;; esac
        case "$b" in *.*) b=${b#*.} ;; *) b="" ;; esac
    done
    return 0
}
case "$channel_flag" in ""|stable|development) ;; *) fail "--channel must be 'stable' or 'development' (got '$channel_flag')" ;; esac
case "${SYNOS_CHANNEL:-}" in ""|stable|development) ;; *) fail "SYNOS_CHANNEL must be 'stable' or 'development' (got '$SYNOS_CHANNEL')" ;; esac
# Two files are "the same" launcher when they agree ignoring CR bytes: a bundle
# downloaded (or applied) on a system that turns build.cmd's CRLF into LF must
# not be offered an update forever over a line-ending difference alone. A real
# change is still written with the CRLF bytes exactly as fetched.
same_ignoring_cr() {
    [ -f "$1" ] && [ -f "$2" ] || return 1
    [ "$(tr -d '\r' < "$1" | cksum)" = "$(tr -d '\r' < "$2" | cksum)" ]
}

# ------------------------------------------------------------- the launchers
LAUNCHER_LIST="sh:build.sh ps1:build.ps1 cmd:build.cmd command:build.command"

# Downloads (or copies, offline via SYNOS_ENGINE_SOURCE) a fresh copy of one
# launcher into a temporary path. Nothing fetched here is ever executed,
# eval'd or sourced; it is only written to disk and syntax-checked.
fetch_launcher() {  # ext out-path
    ext=$1; out=$2
    if [ -n "${launcher_src:-}" ]; then
        from="$launcher_src/tools/bundle_launcher.$ext"
        [ -f "$from" ] || { fetch_error="$from is missing"; return 1; }
        cp "$from" "$out" 2>/dev/null || { fetch_error="could not copy $from"; return 1; }
        return 0
    fi
    url="$launcher_url/bundle_launcher.$ext"
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL --connect-timeout 5 --max-time 20 "$url" -o "$out" 2>/dev/null || { fetch_error="downloading $url failed"; return 1; }
    elif command -v wget >/dev/null 2>&1; then
        wget -q --timeout=20 "$url" -O "$out" 2>/dev/null || { fetch_error="downloading $url failed"; return 1; }
    else
        fetch_error="curl or wget is needed to download launchers (or set SYNOS_ENGINE_SOURCE to a checkout)"
        return 1
    fi
    return 0
}

# A fetched launcher never replaces the real one until it looks like the
# right kind of script: this rules out a captive portal, a registry error
# page or an empty response silently bricking a bundle's launchers.
validate_launcher() {  # ext path
    ext=$1; path=$2
    [ -s "$path" ] || { fetch_error="downloaded file is empty"; return 1; }
    case "$(head -c 512 "$path" 2>/dev/null | tr 'A-Z' 'a-z')" in
        *'<!doctype html'*|*'<html'*) fetch_error="downloaded file is an HTML page, not a script"; return 1 ;;
    esac
    case "$ext" in
        sh|command)
            [ "$(head -n 1 "$path")" = "#!/bin/sh" ] || { fetch_error="does not start with #!/bin/sh"; return 1; }
            sh -n "$path" 2>/dev/null || { fetch_error="failed a shell syntax check (sh -n)"; return 1; }
            ;;
        ps1)
            case "$(head -n 1 "$path")" in
                "# Builds this configuration bundle"*) ;;
                *) fetch_error="does not start with the expected header line"; return 1 ;;
            esac
            grep -q 'param(' "$path" || { fetch_error="has no param( block"; return 1; }
            ;;
        cmd)
            [ "$(head -n 1 "$path" | tr -d '\r')" = "@echo off" ] || { fetch_error="does not start with @echo off"; return 1; }
            ;;
    esac
    return 0
}

# ./build.sh update: pulls the four current launchers (SYNOS_ENGINE_SOURCE, an
# engine checkout, when set; otherwise SYNOS_LAUNCHER_URL or the engine's own
# repository) and replaces the bundle's copies. All four are downloaded and
# validated before any of them is touched, so a bad source changes nothing.
update_launchers() {
    launcher_src=${SYNOS_ENGINE_SOURCE:-}
    launcher_url=${SYNOS_LAUNCHER_URL:-https://raw.githubusercontent.com/Synthot/SynOS-Studio/main/tools}
    workdir=".build/update.$$"
    rm -rf "$workdir"
    mkdir -p "$workdir" || fail "could not create $workdir" 2
    for pair in $LAUNCHER_LIST; do
        ext=${pair%%:*}; target=${pair#*:}
        if ! fetch_launcher "$ext" "$workdir/$target" || ! validate_launcher "$ext" "$workdir/$target"; then
            rm -rf "$workdir"
            fail "$target: $fetch_error (nothing was changed)" 2
        fi
    done
    updated=0
    replaced=""
    for pair in $LAUNCHER_LIST; do
        ext=${pair%%:*}; target=${pair#*:}
        tmp="$workdir/$target"
        if same_ignoring_cr "$tmp" "$target"; then
            say "$target: already current"
            continue
        fi
        case "$target" in
            build.sh|build.command) mode=755 ;;
            *) if [ -f "$target" ] && [ -x "$target" ]; then mode=755; else mode=644; fi ;;
        esac
        if ! { cp "$tmp" "$target.new" && chmod "$mode" "$target.new" && mv -f "$target.new" "$target"; }; then
            rm -rf "$workdir"
            if [ -n "$replaced" ]; then
                fail "could not replace $target (already replaced:$replaced); run update again to finish" 2
            else
                fail "could not replace $target" 2
            fi
        fi
        say "$target: updated"
        updated=$((updated + 1))
        replaced="$replaced $target"
    done
    rm -rf "$workdir"
    if [ "$updated" -gt 0 ]; then say "launchers updated ($updated of 4)."; else say "launchers already up to date."; fi
}

# Called only when build.sh itself was just refreshed, below: pulls the other
# three launchers from the same source. Best effort and never blocks the
# build - a problem here is a one-line warning, not a failure.
refresh_siblings() {
    workdir=".build/sibling-refresh.$$"
    rm -rf "$workdir"
    mkdir -p "$workdir" 2>/dev/null || { say "warning: could not refresh the other launchers (no $workdir)."; return; }
    for pair in ps1:build.ps1 cmd:build.cmd command:build.command; do
        ext=${pair%%:*}; target=${pair#*:}
        tmp="$workdir/$target"
        if [ -n "${src:-}" ] && [ -f "$src/tools/bundle_launcher.$ext" ]; then
            cp "$src/tools/bundle_launcher.$ext" "$tmp" 2>/dev/null
        else
            run_runtime run --rm --platform "linux/$arch" "$image" cat "/opt/synos/tools/bundle_launcher.$ext" > "$tmp" 2>/dev/null
        fi
        if ! validate_launcher "$ext" "$tmp"; then
            say "warning: $target was not refreshed ($fetch_error); it was left unchanged."
            rm -rf "$workdir"
            return
        fi
    done
    for pair in ps1:build.ps1 cmd:build.cmd command:build.command; do
        ext=${pair%%:*}; target=${pair#*:}
        tmp="$workdir/$target"
        if same_ignoring_cr "$tmp" "$target"; then continue; fi
        case "$target" in
            build.command) mode=755 ;;
            *) if [ -f "$target" ] && [ -x "$target" ]; then mode=755; else mode=644; fi ;;
        esac
        if ! { cp "$tmp" "$target.new" && chmod "$mode" "$target.new" && mv -f "$target.new" "$target"; }; then
            say "warning: could not replace $target with the engine's current version."
        fi
    done
    rm -rf "$workdir"
}

# Runs once, for `check` and `build` only (not `update`, not -h, and never
# after a restart of this script): looks for newer launchers before anything
# that needs a runtime, disk space or a registry, and offers to fetch them.
# A network problem here is a one-line note, never a build failure.
check_for_launcher_update() {
    [ -z "${SYNOS_NO_UPDATE_CHECK:-}" ] || return 0
    [ -z "${SYNOS_LAUNCHER_REFRESHED:-}" ] || return 0
    launcher_src=${SYNOS_ENGINE_SOURCE:-}
    launcher_url=${SYNOS_LAUNCHER_URL:-https://raw.githubusercontent.com/Synthot/SynOS-Studio/main/tools}
    workdir=".build/update-check.$$"
    rm -rf "$workdir"
    mkdir -p "$workdir" 2>/dev/null || return 0
    changed=""
    for pair in $LAUNCHER_LIST; do
        ext=${pair%%:*}; target=${pair#*:}
        if ! fetch_launcher "$ext" "$workdir/$target" || ! validate_launcher "$ext" "$workdir/$target"; then
            say "could not check for a launcher update: $fetch_error; continuing"
            rm -rf "$workdir"
            return 0
        fi
        same_ignoring_cr "$workdir/$target" "$target" || changed="$changed $target"
    done
    if [ -z "$changed" ]; then
        # The check reached its source and the launchers agree: mark this run
        # refreshed so the late, mid-build self-refresh (a different source:
        # the engine image, not SYNOS_LAUNCHER_URL) does not undo this with an
        # older copy and start a downgrade-then-offer-upgrade loop next time.
        rm -rf "$workdir"
        SYNOS_LAUNCHER_REFRESHED=1
        return 0
    fi
    say "a newer launcher is available:$changed"
    proceed=""
    if [ -n "$yes_to_all" ]; then
        proceed=1
    elif [ ! -t 0 ]; then
        say "run ./build.sh update to apply it."
    elif ask_yes "A newer launcher is available. Update now?"; then
        proceed=1
    fi
    if [ -z "$proceed" ]; then
        rm -rf "$workdir"
        SYNOS_LAUNCHER_REFRESHED=1
        return 0
    fi
    replaced=""
    for pair in $LAUNCHER_LIST; do
        ext=${pair%%:*}; target=${pair#*:}
        tmp="$workdir/$target"
        case "$target" in
            build.sh|build.command) mode=755 ;;
            *) if [ -f "$target" ] && [ -x "$target" ]; then mode=755; else mode=644; fi ;;
        esac
        if ! { cp "$tmp" "$target.new" && chmod "$mode" "$target.new" && mv -f "$target.new" "$target"; }; then
            rm -rf "$workdir"
            if [ -n "$replaced" ]; then
                fail "could not replace $target (already replaced:$replaced); run ./build.sh update again to finish" 2
            else
                fail "could not replace $target" 2
            fi
        fi
        replaced="$replaced $target"
    done
    rm -rf "$workdir"
    say "the launchers were updated; starting again."
    SYNOS_LAUNCHER_REFRESHED=1 exec sh "$0" "$@"
}

[ "$command_word" = update ] && { update_launchers; exit 0; }

# ---------------------------------------------------------------- the bundle
[ -f bundle.json ] || fail "bundle.json is missing: run this script from the unzipped bundle folder"
manifest=$(sed -n 's/.*"manifest"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' bundle.json | head -n 1)
[ -n "$manifest" ] && [ -f "$manifest" ] || fail "bundle.json names no manifest, or $manifest is missing"
engine=$(sed -n 's/.*"min"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' bundle.json | head -n 1)
# A bundle is data, and this value is read straight out of it: it ends up in a
# URL (archive/refs/tags/v<engine>.tar.gz) and, when GitHub's release list
# cannot be reached, in the name of the directory the engine source is
# extracted to and removed from. Digits and dots only, so nothing from a
# bundle can ever reach outside .build/engine-src/ or into a fetched URL's
# path.
case "$engine" in
    "") ;;
    *[!0-9.]*|*..*|.*|*.) fail "bundle.json names an engine minimum that is not a version: $engine" 1 ;;
esac
base=$(field "$manifest" base)
suite=$(field "$manifest" suite)
arch=$(field "$manifest" arch)
[ -n "$arch" ] || arch=amd64
[ -n "$base" ] && [ -n "$suite" ] || fail "$manifest does not name a base and a suite"

# ------------------------------------------------------------- the channel
# Which engine pipeline this bundle builds against: recorded by the front end
# that generated it, in its own bundle.json ("stable" assumed when the key is
# absent, so every bundle generated before this existed keeps working exactly
# as it does today). SYNOS_CHANNEL/--channel override it for a person who
# knows what they are doing, and win over the bundle's own value either way.
bundle_channel=$(sed -n 's/.*"channel"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' bundle.json | head -n 1)
case "$bundle_channel" in
    ""|stable|development) ;;
    *) say "note: bundle.json names an unknown channel '$bundle_channel'; treating this bundle as stable."; bundle_channel="" ;;
esac
if [ -n "$channel_flag" ]; then
    channel=$channel_flag; channel_reason="the --channel flag"
elif [ -n "${SYNOS_CHANNEL:-}" ]; then
    channel=$SYNOS_CHANNEL; channel_reason="the SYNOS_CHANNEL environment variable"
elif [ -n "$bundle_channel" ]; then
    channel=$bundle_channel; channel_reason="bundle"
else
    channel=stable; channel_reason="default"
fi
case "$channel_reason" in
    bundle)
        if [ "$channel" = development ]; then
            say "channel: development (this bundle was made by a development instance of the page)"
        else
            say "channel: stable (this bundle was made by the released page)"
        fi
        ;;
    default) say "channel: stable (default; bundle.json names no channel)" ;;
    *) say "channel: $channel (override: $channel_reason)" ;;
esac
if [ "$channel" = development ]; then
    say "note: the development channel builds against the unreleased tip of the engine's main branch, not a released version."
fi

check_for_launcher_update "$@"

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

is_podman() { case "$runtime" in *podman) return 0 ;; *) return 1 ;; esac; }
is_docker() { case "$runtime" in *docker) return 0 ;; *) return 1 ;; esac; }

# Docker's storage (images, layers, the chroot) is one setting for the whole
# daemon; there is no per-build override. Printed when a person asks this
# script to put a build's storage somewhere while running under docker, and
# again inside the low-space refusal below, since moving it is the only fix
# there. The snap's daemon.json lives under a different path than a plain
# package's, and the snap also needs a manual `snap connect` before it can
# reach anything under /mnt, so both are spelled out rather than guessed.
docker_storage_help() {
    cat <<'EOF'
docker's storage is one setting for the whole daemon; there is no per-build location.
  plain install:  add "data-root": "<path>" to /etc/docker/daemon.json, then: sudo systemctl restart docker
  snap install:   the same key in /var/snap/docker/current/config/daemon.json, then: sudo snap restart docker
                  (once: sudo snap connect docker:removable-media, so docker can reach /mnt)
  or: install podman, which takes a location per build, no root, no restart (--storage=<path>)
  or: build on a machine that already has room where docker keeps its images
EOF
}

# The overlay filesystem that backs a container's chroot cannot live on
# everything; a location on vfat, exFAT, NTFS or a network share fails deep
# into the build with an opaque mount error. Caught once, here, instead of
# left for the person to chase through dist/build.log.
check_overlay_fs() {  # path
    fstype=$(stat -f -c %T "$1" 2>/dev/null || true)
    case "$fstype" in
        vfat|msdos|exfat|ntfs|fuseblk|cifs|smb2|nfs|nfs4)
            fail "$1 is $fstype, which cannot back a container's overlay filesystem; point --storage at a disk formatted ext4, xfs, btrfs or similar" 2 ;;
    esac
}

# Recorded next to the bundle so the second run is never asked again (see the
# storage question below): "default" means "asked, and podman's own storage
# was kept" - remembered too, so a machine that is merely a little tight is
# not asked on every single run. A flag or environment variable still wins
# this run; passing --container-root=<new path> also updates what is
# remembered. To forget it entirely: rm .build/container-root.
remember_container_root() {  # value ("default" or a path)
    mkdir -p .build 2>/dev/null || return 0
    printf '%s\n' "$1" > "$remembered_root_file" 2>/dev/null || true
}

# The exact debris a pre-fix launcher could leave at the bundle root:
# podman's own storage, confirmed against a real corrupted bundle (the
# same marker set find_storage_root_inside checks), plus its two lock
# files - never a bundle file (bundle.json, manifests/, profiles/,
# branding/, keys/, the launchers, README.md are the person's own).
BUNDLE_ROOT_STORAGE_DEBRIS="overlay overlay-containers overlay-images overlay-layers libpod db.sql defaultNetworkBackend storage.lock userns.lock"

human_kb() {  # kb -> "12 GB" / "340 MB" / "8 KB"
    kb=$1
    if [ "$kb" -ge 1048576 ]; then printf '%s GB\n' "$((kb / 1048576))"
    elif [ "$kb" -ge 1024 ]; then printf '%s MB\n' "$((kb / 1024))"
    else printf '%s KB\n' "$kb"
    fi
}
# A size to show before removing something, never load-bearing: when `du`
# is missing or refuses (a permission a plain rm -rf would still get
# through, via sudo, below), "size unknown" is shown rather than this
# failing the whole command over a number that is only ever informational.
target_size() {  # path
    [ -d "$1" ] || { printf 'a file\n'; return 0; }
    kb=$(du -sk "$1" 2>/dev/null | awk '{print $1}')
    case "$kb" in ''|*[!0-9]*) printf 'size unknown\n' ;; *) human_kb "$kb" ;; esac
}

# The full list of paths ./build.sh reset-storage will ever consider, built
# once from a closed set of sources - never a path read from anywhere else,
# and never a bundle file: the remembered storage root (wherever it points -
# that is the one this bundle is actually using), the default storage
# directory, an extracted engine source, the debris list above, all three
# relative to this bundle only, and a --storage/SYNOS_CONTAINER_ROOT given
# on this same command line (checked below before it is trusted).
reset_targets=""
add_reset_target() {  # path
    [ -e "$1" ] || return 0
    for existing in $reset_targets; do [ "$existing" = "$1" ] && return 0; done
    reset_targets="$reset_targets $1"
}

# ./build.sh reset-storage: the storage this bundle itself created, removed
# in one command instead of a block of sudo rm -rf pasted from a chat -
# what a storage-state mismatch (see explain_storage_mismatch above) tells
# a person to run. dist/ (the build log and the evidence next to the ISO)
# is never touched: it is not storage, and a person reading why a build
# failed still needs it after this runs.
reset_storage() {
    if [ -n "$container_root" ]; then
        in_bundle=""
        case "$container_root" in "$PWD"/*) in_bundle=1 ;; esac
        remembered_match=""
        if [ -f "$remembered_root_file" ]; then
            remembered=$(cat "$remembered_root_file" 2>/dev/null || true)
            if [ -n "$remembered" ] && [ "$remembered" != default ] && [ "$(abs_path "$remembered")" = "$container_root" ]; then
                remembered_match=1
            fi
        fi
        if [ -z "$in_bundle" ] && [ -z "$remembered_match" ]; then
            fail "--storage $container_root is not this bundle's own storage: nothing here remembers it, and it is outside this bundle. reset-storage only ever removes storage this bundle itself created; remove that path yourself if you are sure: sudo rm -rf $container_root" 2
        fi
        add_reset_target "$container_root"
    fi
    if [ -f "$remembered_root_file" ]; then
        remembered=$(cat "$remembered_root_file" 2>/dev/null || true)
        case "$remembered" in
            ""|default) ;;
            *) add_reset_target "$(abs_path "$remembered")" ;;
        esac
        add_reset_target "$PWD/$remembered_root_file"
    fi
    add_reset_target "$PWD/.build/container-storage"
    add_reset_target "$PWD/.build/engine-src"
    for name in $BUNDLE_ROOT_STORAGE_DEBRIS; do
        add_reset_target "$PWD/$name"
    done
    if [ -z "$reset_targets" ]; then
        say "nothing to remove: this bundle has no storage of its own yet."
        return 0
    fi
    say "this will remove:"
    for t in $reset_targets; do
        say "  $t ($(target_size "$t"))"
    done
    say "dist/ (the build log and the evidence next to the ISO) is left alone - it is not storage."
    ask "Remove this now?" || { say "nothing removed."; return 0; }
    reset_failed=""
    for t in $reset_targets; do
        rm -rf "$t" 2>/dev/null && continue
        if [ -n "${run_as:-}" ] && $run_as rm -rf "$t" 2>/dev/null; then continue; fi
        reset_failed="$reset_failed $t"
    done
    if [ -n "$reset_failed" ]; then
        fail "could not remove:$reset_failed - remove them by hand (sudo rm -rf <path>) and run reset-storage again" 2
    fi
    say "removed. The next build starts with fresh storage."
}

# Already running as root through sudo (rather than this script deciding to
# elevate a single command itself, below): sudo's default environment reset
# drops every SYNOS_* variable set before it, even though this script would
# otherwise still see them - there is no way to recover a value already gone
# by the time this script started, so the honest thing is to say so plainly
# rather than silently building with whatever defaults are left.
if [ "$(id -u)" -eq 0 ] && [ -n "${SUDO_USER:-}" ]; then
    say "note: already running as root through sudo, which resets the environment by default: SYNOS_ENGINE_SOURCE, SYNOS_ENGINE_URL, SYNOS_BUILDER_IMAGE, SYNOS_CONTAINER_ROOT, SYNOS_CHANNEL and SYNOS_YES set before 'sudo' are dropped unless you run 'sudo SYNOS_VAR=value ./build.sh' (repeat per variable) or 'sudo -E ./build.sh'."
fi

runtime=$(command -v podman || command -v docker || true)
if [ -z "$runtime" ]; then
    [ "$command_word" = check ] && fail "podman or docker is required; ./build.sh installs podman for you (Linux)" 2
    install_podman
    runtime=$(command -v podman || command -v docker || true)
    [ -n "$runtime" ] || fail "podman is still not on PATH; open a new terminal and run this script again" 2
fi

# Where the build's storage lives: podman takes --root/--runroot per
# invocation; docker's is one setting for the whole daemon, so asking it for
# a location here is refused plainly, before docker is ever run for any
# reason (even the sudo check below), rather than silently ignored (a build
# that then fails, or one that succeeds into the default location the person
# was trying to avoid, would both be worse than saying so now).
if { [ -n "$container_root" ] || [ -n "$container_runroot" ]; } && is_docker; then
    printf 'error: %s\n' "docker cannot put this build's storage anywhere but its own daemon-wide location." >&2
    docker_storage_help >&2
    exit 2
fi

# The build mounts filesystems, runs debootstrap and loop-mounts an EFI image:
# it needs a root container runtime. On Linux, rootless podman and a docker
# socket the user cannot reach are used through sudo. On macOS the runtime
# talks to a Linux virtual machine: podman machine is created rootful and
# sized for the build; Docker Desktop must be running with enough resources.
# Decided before the storage question below, which also needs to run a
# command through the runtime (podman info) to see what is free.
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

[ "$command_word" = reset-storage ] && { reset_storage; exit 0; }

# Runs the container runtime with --root/--runroot prepended when a storage
# location is in effect (podman only; see below - neither is ever set for
# docker). A string built once and word-split back apart (the earlier shape
# of this) breaks the moment either path contains a space; branching on real
# quoted arguments instead means a path survives intact regardless of what
# it contains.
run_runtime() {
    if [ -n "$container_root" ] && [ -n "$container_runroot" ]; then
        run_runtime_exec --root "$container_root" --runroot "$container_runroot" "$@"
    elif [ -n "$container_root" ]; then
        run_runtime_exec --root "$container_root" "$@"
    elif [ -n "$container_runroot" ]; then
        run_runtime_exec --runroot "$container_runroot" "$@"
    else
        run_runtime_exec "$@"
    fi
}
# Elevated through sudo when this script decided it needs to be (above).
# sudo's own environment reset would otherwise drop this script's SYNOS_*
# variables even though this (non-root) shell still has them: `sudo
# VAR=value cmd` (variables named directly on sudo's own command line, not
# merely inherited) is what actually reaches the child process, so the
# values are read here, from this script's own environment, and handed to
# sudo explicitly rather than trusted to survive on their own.
run_runtime_exec() {
    if [ -n "$run_as" ]; then
        sudo \
            SYNOS_ENGINE_SOURCE="${SYNOS_ENGINE_SOURCE:-}" SYNOS_ENGINE_URL="${SYNOS_ENGINE_URL:-}" \
            SYNOS_BUILDER_IMAGE="${SYNOS_BUILDER_IMAGE:-}" SYNOS_CONTAINER_ROOT="${SYNOS_CONTAINER_ROOT:-}" \
            SYNOS_CHANNEL="$channel" SYNOS_YES="${yes_to_all:-}" \
            SYNOS_SIGNING_KEY="${SYNOS_SIGNING_KEY:-}" SYNOS_SIGNING_KEY_FILE="${SYNOS_SIGNING_KEY_FILE:-}" \
            -- "$runtime" "$@"
    else
        "$runtime" "$@"
    fi
}

# podman's own wording for a storage location that carries state from a
# different configuration than the one now being asked of it - reproduced
# directly against a real podman for this launcher, not guessed at: moving
# a --root, a --runroot or a graph driver out from under an existing
# storage all end the same way ("database static dir ... does not match
# our static dir ...", "database run root ... does not match our run
# root ...", "database graph driver ... does not match our graph
# driver ..."), always closing with the same four words, which is the one
# thing safe to match on rather than any one of those prefixes; the same
# family covers running the same storage rootless after rootful or back.
# Podman's own line names exactly what disagrees but not what to do about
# it, so it is shown verbatim and then explained, never swallowed.
explain_storage_mismatch() {  # runtime-stderr-text storage-path
    case "$1" in
        *'database configuration mismatch'*)
            printf '%s\n' "$1" >&2
            fail "$2 carries state from a different configuration than this run is asking for (podman's own message, above, names exactly what disagrees); remove it with ./build.sh reset-storage, or point --storage at a different, empty location" 2 ;;
    esac
}
# `podman/docker info --format <field>` used only to look something up
# (whether the default storage can be used, how much space is free) -
# never load-bearing enough to fail the whole run over a field that
# simply is not there yet (a fresh runtime with nothing built). But a real
# storage-state mismatch is not "not there yet"; it is translated and
# stopped here rather than silently degrading to an empty value and a
# skipped check further down, which would otherwise look like this script
# just could not tell either way.
runtime_info_field() {  # format storage-path-for-the-message
    mkdir -p .build 2>/dev/null || true
    # Reused, not removed: this is a lookup that can run during `check`
    # alone, which promises never to need anything build-only like `rm` -
    # overwritten in place on every call instead (`>`, not a fresh name per
    # call), so nothing accumulates under .build/ either.
    err=.build/runtime-info-err
    value=$(run_runtime info --format "$1" 2>"$err") || explain_storage_mismatch "$(cat "$err" 2>/dev/null)" "$2"
    printf '%s\n' "$value"
}

free_kb=$(df -Pk . | awk 'NR==2 {print $4}')
[ "$free_kb" -ge 41943040 ] || fail "at least 40 GB free is needed in $(pwd); $((free_kb / 1048576)) GB available" 2

min_store_kb=31457280                          # 30 GB

# A location already chosen for this bundle (by hand, or by answering the
# question below on an earlier run) is remembered, never asked twice. A
# relative value here was written by a launcher older than the fix above;
# resolved against the bundle rather than discarded, since the person
# already chose that name on purpose - discarding it would silently move
# their storage to a location they never chose, right when this script is
# trying hardest not to do exactly that - and rewritten here as absolute,
# once, so the file itself is never ambiguous again.
storage_asked=""
if is_podman && [ -z "$container_root" ] && [ -z "$macos" ] && [ -f "$remembered_root_file" ]; then
    remembered=$(cat "$remembered_root_file" 2>/dev/null || true)
    case "$remembered" in
        "") ;;
        default) storage_asked=1
                 say "using podman's own storage for this build, not this bundle's disk (change: --storage=<path>; forget: rm $remembered_root_file)" ;;
        *) container_root=$(abs_path "$remembered"); storage_asked=1
           [ "$container_root" = "$remembered" ] || remember_container_root "$container_root"
           say "using the remembered build storage: $container_root (change: --storage=<path>; forget: rm $remembered_root_file)" ;;
    esac
fi

# The bundle's own disk is the default, needing nothing from you: this
# script already knows where to build from where the bundle was unpacked,
# and that is where its storage goes too - no variable to learn, no
# question to answer. A real choice remains only when that disk cannot back
# a container's overlay at all (an unpacked bundle on a removable
# vfat/exFAT/NTFS drive, say - the free-space minimum below is already
# guaranteed by the 40 GB check above, so it is never the reason to ask);
# only then, and only when it can actually be answered in two seconds (a
# terminal, no --yes, not just a check), is podman's own storage offered
# instead.
container_root_auto=""
if is_podman && [ -z "$container_root" ] && [ -z "$macos" ] && [ -z "$storage_asked" ]; then
    bundle_store="$PWD/.build/container-storage"
    bundle_fstype=$(stat -f -c %T . 2>/dev/null || true)
    bundle_store_unusable=""
    case "$bundle_fstype" in
        vfat|msdos|exfat|ntfs|fuseblk|cifs|smb2|nfs|nfs4) bundle_store_unusable=1 ;;
    esac
    if [ -n "$bundle_store_unusable" ] && [ "$command_word" != check ] && [ -z "$yes_to_all" ] && [ -t 0 ]; then
        default_store=$(runtime_info_field '{{.Store.GraphRoot}}' "podman's own storage")
        case "$default_store" in ""|*"{{"*|"<no value>") default_store="" ;; esac
        if [ -n "$default_store" ] && [ -d "$default_store" ]; then
            default_store_kb=$(df -Pk "$default_store" | awk 'NR==2 {print $4}')
            say "this bundle's own disk is $bundle_fstype, which cannot back a container's overlay filesystem; podman's own storage at $default_store ($((default_store_kb / 1048576)) GB free) can."
            if ask_yes "Use podman's own storage there instead?"; then
                container_root=""
            else
                container_root=$bundle_store
            fi
            storage_asked=1
            remember_container_root "${container_root:-default}"
        fi
    fi
    if [ -z "$storage_asked" ]; then
        container_root=$bundle_store
        container_root_auto=1
        if [ ! -d "$container_root" ]; then
            say "this build keeps its storage under $container_root; use --storage=<path> (or SYNOS_CONTAINER_ROOT) to put it somewhere else."
        fi
    fi
fi

if is_podman && { [ -n "$container_root" ] || [ -n "$container_runroot" ]; }; then
    if [ -n "$macos" ]; then
        say "note: --storage/SYNOS_CONTAINER_ROOT and --container-runroot/SYNOS_CONTAINER_RUNROOT have no effect here: podman on macOS runs in its own virtual machine with its own disk (set its size once with podman machine init --disk-size)."
        container_root=""; container_runroot=""
    else
        if [ -n "$container_root" ]; then
            refuse_storage_inside "$PWD/dist" "the build's output directory (dist/)"
            # The engine source a build downloads for itself lands under this
            # fixed directory, so the collision that actually happened in the
            # field (--storage storage, from a bundle, ending up inside the
            # unpacked engine checkout the image is then built from) is caught
            # here, before the directory is created and before the location is
            # remembered for later runs - not only later, in build_engine_image().
            refuse_storage_inside "$PWD/.build/engine-src" "the engine source a build unpacks for itself (.build/engine-src/)"
            if [ -n "${SYNOS_ENGINE_SOURCE:-}" ]; then
                refuse_storage_inside "$SYNOS_ENGINE_SOURCE" "the engine source this image would be built from"
                refuse_existing_storage_in "$SYNOS_ENGINE_SOURCE" "the engine source this image would be built from"
            fi
            mkdir -p "$container_root" 2>/dev/null || fail "could not create $container_root (--storage)" 2
            check_overlay_fs "$container_root"
            [ -n "$container_root_auto" ] || remember_container_root "$container_root"
        fi
        if [ -n "$container_runroot" ]; then
            mkdir -p "$container_runroot" 2>/dev/null || fail "could not create $container_runroot (SYNOS_CONTAINER_RUNROOT)" 2
        fi
    fi
fi

# The chroot, the image staging and the cache live in the container runtime's own
# storage, not next to the bundle; a small root partition there fails a build
# halfway with "No space left on device" while dpkg configures packages. When
# SYNOS_CONTAINER_ROOT chose that location already, it is checked directly
# instead of asked back of podman, so this is right even before the first run
# creates anything there.
if [ -n "$container_root" ] && is_podman; then
    store=$container_root
else
    store=$(runtime_info_field '{{.DockerRootDir}}' "${container_root:-$runtime's own storage}")
    case "$store" in ""|*"{{"*|"<no value>") store=$(runtime_info_field '{{.Store.GraphRoot}}' "${container_root:-$runtime's own storage}") ;; esac
fi
if [ -n "$store" ] && [ -d "$store" ]; then
    store_kb=$(df -Pk "$store" | awk 'NR==2 {print $4}')
    if [ "$store_kb" -lt "$min_store_kb" ]; then
        free_store_gb=$((store_kb / 1048576))
        if is_podman; then
            fail "this build needs 30 GB free; only ${free_store_gb} GB is free at $store, where podman keeps the build.
  no root needed: --storage=/path/with/room ./build.sh  (or SYNOS_CONTAINER_ROOT=/path/with/room)
  or: free space at $store
  or: move podman's own default storage (root): graphroot in \$HOME/.config/containers/storage.conf, or /etc/containers/storage.conf" 2
        else
            fail "this build needs 30 GB free; only ${free_store_gb} GB is free at $store, where docker keeps the build.
  free space at $store, or install podman (moves its storage with no root: SYNOS_CONTAINER_ROOT=/path), or:
$(docker_storage_help)" 2
        fi
    fi
fi

# ---------------------------------------------------------------- the image
# The published image is preferred. On the stable channel only a release that
# satisfies this bundle is ever pulled or built - never the moving tag, never
# the development branch, in any form. The development channel is unchanged
# from before: the moving tag, then a local build from the tip of the main
# branch - both named so an image built this way is never mistaken for a
# released one (the "-dev-" in its tag, and the note above in this script's
# own output).
GITHUB_REPO_URL="https://github.com/Synthot/SynOS-Studio"
GITHUB_TAGS_API="https://api.github.com/repos/Synthot/SynOS-Studio/tags?per_page=100"

fetch_url_to() {  # url out -> 0 on success; no error is printed here, the caller decides how to react
    url=$1; out=$2
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL --connect-timeout 5 --max-time 20 -H 'Accept: application/vnd.github+json' "$url" -o "$out" 2>/dev/null
    elif command -v wget >/dev/null 2>&1; then
        wget -q --timeout=20 --header='Accept: application/vnd.github+json' "$url" -O "$out" 2>/dev/null
    else
        return 1
    fi
}

# Resolves the release this bundle should build against on the stable
# channel: the newest tag (v<version> on the public repository) at or above
# engine.min, read from GitHub's tags API - no token needed, and never
# guessed from a moving branch. Sets resolved_version/resolved_tag/
# resolved_note, or refuses with `fail` when nothing satisfies the bundle.
# When the API cannot be reached at all (offline, or its anonymous rate
# limit), this falls back to the one release the bundle names directly -
# archive/refs/tags/v<engine.min>.tar.gz, no further API calls - rather than
# leaving the person with an opaque network error; chosen over always
# requiring the API precisely so a rate limit never turns into a build
# failure for something this bundle already names plainly.
resolve_stable_release() {
    tags_tmp=".build/github-tags.$$"
    mkdir -p .build 2>/dev/null || true
    if fetch_url_to "$GITHUB_TAGS_API" "$tags_tmp" && [ -s "$tags_tmp" ]; then
        overall_best=""; best=""
        for v in $(grep -o '"name": *"v[0-9][0-9.]*"' "$tags_tmp" | sed 's/.*"v\([0-9.]*\)".*/\1/' | sort -u); do
            case "$v" in ""|*[!0-9.]*) continue ;; esac
            if [ -z "$overall_best" ] || ver_ge "$v" "$overall_best"; then overall_best=$v; fi
            if [ -n "$engine" ] && ! ver_ge "$v" "$engine"; then continue; fi
            if [ -z "$best" ] || ver_ge "$v" "$best"; then best=$v; fi
        done
        rm -f "$tags_tmp"
        if [ -z "$overall_best" ]; then
            fail "GitHub's release list for $GITHUB_REPO_URL came back but named no v<version> tag; set SYNOS_ENGINE_URL, SYNOS_ENGINE_SOURCE or SYNOS_BUILDER_IMAGE, or build with --channel=development" 2
        fi
        [ -n "$engine" ] || best=$overall_best
        if [ -z "$best" ]; then
            fail "this bundle needs engine $engine; the newest release is $overall_best. Wait for a release that satisfies it, build your own engine (SYNOS_ENGINE_SOURCE=<checkout> ./build.sh), or build with --channel=development for the unreleased engine." 2
        fi
        resolved_version=$best
        resolved_tag="v$best"
        resolved_note="the newest published release satisfying this bundle, from GitHub's release list"
        return 0
    fi
    rm -f "$tags_tmp"
    if [ -n "$engine" ]; then
        resolved_version=$engine
        resolved_tag="v$engine"
        resolved_note="GitHub's release list could not be checked (offline, or its anonymous rate limit); using the release this bundle names directly"
        return 0
    fi
    fail "this bundle names no minimum engine version and GitHub's release list could not be checked (offline, or its anonymous rate limit); set SYNOS_ENGINE_URL or SYNOS_ENGINE_SOURCE, try again shortly, or build with --channel=development" 2
}

# Fetches the engine source archive, reusing the existing file unchanged
# when the server confirms nothing changed - checked with an ETag
# (If-None-Match): GitHub's own archive endpoint does not honor
# If-Modified-Since (confirmed against the real service - it sends no
# Last-Modified header at all), only If-None-Match, which it answers with a
# real 304 and no body. Always fetched in full the first time, or whenever
# no ETag was kept from before; this is exactly where the moving
# development archive earns the check, since a tag's own URL never repeats
# with different content in the first place.
fetch_engine_archive() {  # url archive-file
    etag_file="$2.etag"
    headers=".build/engine-src.headers.$$"
    if [ -f "$2" ] && [ -s "$etag_file" ] && command -v curl >/dev/null 2>&1; then
        etag=$(cat "$etag_file")
        http_code=$(curl -sS -L --connect-timeout 5 --max-time 120 -w '%{http_code}' \
                    -H "If-None-Match: $etag" -D "$headers" -o "$2.new" "$1" 2>/dev/null || true)
        case "$http_code" in
            304)
                rm -f "$2.new" "$headers"
                say "the development engine source is unchanged upstream; reusing $2"
                return 0 ;;
            200)
                if [ -s "$2.new" ]; then
                    mv -f "$2.new" "$2"
                    sed -n 's/^[Ee][Tt][Aa][Gg]:[[:space:]]*//p' "$headers" | tail -n 1 | tr -d '\r' > "$etag_file"
                    rm -f "$headers"
                    say "downloaded a changed engine source from $1"
                    return 0
                fi ;;
        esac
        rm -f "$2.new" "$headers"
    fi
    say "downloading the engine source from $1"
    if command -v curl >/dev/null 2>&1; then
        if [ -t 1 ]; then curl -fL --progress-bar -D "$headers" "$1" -o "$2.new" || fail "downloading $1 failed" 2
        else curl -fsSL -D "$headers" "$1" -o "$2.new" || fail "downloading $1 failed" 2; fi
        sed -n 's/^[Ee][Tt][Aa][Gg]:[[:space:]]*//p' "$headers" | tail -n 1 | tr -d '\r' > "$etag_file" 2>/dev/null || rm -f "$etag_file"
        rm -f "$headers"
    elif command -v wget >/dev/null 2>&1; then
        wget -q --show-progress "$1" -O "$2.new" || fail "downloading $1 failed" 2
        rm -f "$etag_file"
    else
        fail "curl or wget is needed to download the engine source (or set SYNOS_ENGINE_SOURCE to a checkout)" 2
    fi
    mv -f "$2.new" "$2"
}

# A content-addressed extraction is trusted only when its own completion
# marker is there (a half-extracted tree, from an interrupted run, never
# is) and it still passes the same storage-root scan as any other build
# context - the marker survives contamination that happened after
# extraction finished; the scan does not.
engine_src_is_reusable() {  # dir marker-file
    [ -d "$1" ] || return 1
    [ -f "$2" ] || return 1
    find_storage_root_inside "$1" >/dev/null && return 1
    return 0
}

build_engine_image() {  # build_engine_image [forced] - forced skips reusing a same-named image already on this machine
    forced=${1:-}
    src=${SYNOS_ENGINE_SOURCE:-}
    if [ -n "$src" ]; then
        source_id="local"
        src=$(abs_path "$src")
    else
        if [ -n "${SYNOS_ENGINE_URL:-}" ]; then
            url=$SYNOS_ENGINE_URL
            source_id=""
        elif [ "$channel" = development ]; then
            url=https://github.com/Synthot/SynOS-Studio/archive/refs/heads/main.tar.gz
            source_id=""
        else
            [ -n "${resolved_tag:-}" ] || resolve_stable_release
            url="$GITHUB_REPO_URL/archive/refs/tags/$resolved_tag.tar.gz"
            say "channel stable: building from release $resolved_tag ($resolved_note)"
            # The release tag is immutable once published, so it is as good an
            # identity as a hash of the download - without needing the download
            # to know it first. A changed engine is still always a new tag, so
            # the "new engine, new name, new build" guarantee this replaces
            # still holds; only how cheaply that name is reached changes.
            source_id=$resolved_tag
        fi
        mkdir -p .build
        archive_file=.build/engine-src.tar.gz

        # Known without fetching anything on the stable channel: a run that
        # has already built this image touches the network for nothing but
        # the release lookup above.
        if [ -n "$source_id" ]; then
            if [ "$channel" = development ]; then local_tag="synos-builder:$base-$suite-dev-$source_id"
            else local_tag="synos-builder:$base-$suite-$source_id"; fi
            if [ -z "$forced" ] && run_runtime image inspect "$local_tag" >/dev/null 2>&1; then
                say "using the engine image already built for $source_id: $local_tag"
                image=$local_tag; return 0
            fi
        fi

        # A source already extracted for this exact identity, complete and
        # still clean, is reused as-is - no download, nothing re-extracted
        # for nothing.
        if [ -n "$source_id" ] && engine_src_is_reusable ".build/engine-src/$source_id" ".build/engine-src/$source_id.ok"; then
            say "reusing the engine source already extracted for $source_id: .build/engine-src/$source_id"
        else
            fetch_engine_archive "$url" "$archive_file"
            [ -n "$source_id" ] || source_id=$(sha256sum "$archive_file" | cut -c1-12)
            src_dir=".build/engine-src/$source_id"
            if ! engine_src_is_reusable "$src_dir" "$src_dir.ok"; then
                # The directory a source is extracted to is owned by that
                # extraction: whatever a previous run left inside it - a
                # half extraction, or a poisoned container storage root,
                # root-owned or not - is removed first, never reused
                # silently.
                clean_dir "$src_dir"
                rm -f "$src_dir.ok"
                mkdir -p "$src_dir"
                tar -xzf "$archive_file" -C "$src_dir" --strip-components=1 \
                    || { clean_dir "$src_dir"; fail "the engine source archive could not be unpacked" 2; }
                : > "$src_dir.ok"
            else
                # The archive's own identity was not known until it was
                # fetched (development channel), but the fetch itself just
                # said whether it changed; when it did not, and the tree
                # from last time is still there and still trustworthy, say
                # that plainly too, the same as the stable channel's
                # already-known-identity case above does.
                say "reusing the engine source already extracted for $source_id: $src_dir"
            fi
        fi
        src=$(abs_path ".build/engine-src/$source_id")
    fi
    refuse_storage_inside "$src" "the engine source this image is built from"
    refuse_existing_storage_in "$src" "the engine source this image is built from"
    [ -f "$src/bases/$base/Containerfile" ] || fail "$src has no bases/$base/Containerfile: not an engine checkout" 2
    if [ "$channel" = development ]; then
        local_tag="synos-builder:$base-$suite-dev-$source_id"
    else
        local_tag="synos-builder:$base-$suite-$source_id"
    fi
    if [ -z "$forced" ] && run_runtime image inspect "$local_tag" >/dev/null 2>&1; then
        say "using the engine image built earlier on this machine from this engine source: $local_tag"
        image=$local_tag; return 0
    fi
    say "building the engine image $local_tag from $src (about 20 minutes, once)."
    say "Each build step and package is shown as it happens; the complete output is kept in dist/image-build.log"
    mkdir -p dist
    started=$(date +%s)
    # The full output goes to the log; the terminal gets the lines that show progress
    # (build steps from podman and docker, packages being fetched and set up).
    status_file=$PWD/dist/.image-build.status
    ( cd "$src" && run_runtime build --platform "linux/$arch" --build-arg "SUITE=$suite" -t "$local_tag" -f "bases/$base/Containerfile" . 2>&1; st=$?; echo "$st" >"$status_file" ) \
        | tee dist/image-build.log \
        | awk '/^(STEP [0-9]+\/[0-9]+|Step [0-9]+\/[0-9]+|#[0-9]+ \[[0-9]+\/[0-9]+\]|Get:[0-9]+ |Setting up |Successfully built|Successfully tagged|COMMIT|-->)/ { print "  " $0; fflush() }'
    status=$(cat "$status_file" 2>/dev/null || echo 1)
    rm -f "$status_file"
    if [ "$status" -ne 0 ]; then
        explain_storage_mismatch "$(grep -m 1 'database configuration mismatch' dist/image-build.log 2>/dev/null || true)" "${container_root:-$runtime's own storage}"
        fail "building the engine image failed (exit $status); see dist/image-build.log" 2
    fi
    say "engine image built in $(( ($(date +%s) - started) / 60 )) min"
    image=$local_tag
}
repository=${SYNOS_IMAGE_REPOSITORY:-ghcr.io/synthot/synos-builder}
image=${SYNOS_BUILDER_IMAGE:-}
if [ -z "$image" ]; then
    if [ "$channel" = development ]; then
        pinned="$repository:$base-$suite${engine:+-v$engine}"
        moving="$repository:$base-$suite"
        say "pulling the build engine $pinned (one-time download, about 1.5 GB)"
        if run_runtime pull --platform "linux/$arch" "$pinned" >/dev/null 2>&1; then
            image=$pinned
        elif run_runtime pull --platform "linux/$arch" "$moving" >/dev/null 2>&1; then
            say "no image pinned to engine ${engine:-?}; using the current $moving"
            image=$moving
        elif [ "$command_word" = check ]; then
            image="none published: it will be built here at the first build, about 20 minutes"
        else
            say "no published engine image can be pulled from $repository (not published yet, or the registry refused);"
            say "the image is built here instead, from the engine source."
            build_engine_image
        fi
    else
        # stable: only ever a release that satisfies this bundle.
        pulled=""
        if [ -n "$engine" ]; then
            pinned="$repository:$base-$suite-v$engine"
            say "pulling the build engine $pinned (one-time download, about 1.5 GB)"
            if run_runtime pull --platform "linux/$arch" "$pinned" >/dev/null 2>&1; then
                image=$pinned; pulled=1
            fi
        fi
        if [ -z "$pulled" ]; then
            resolve_stable_release
            say "channel stable: using release $resolved_tag ($resolved_note)"
            pinned2="$repository:$base-$suite-v$resolved_version"
            if [ "$pinned2" != "${pinned:-}" ]; then
                say "pulling the build engine $pinned2 (one-time download, about 1.5 GB)"
                if run_runtime pull --platform "linux/$arch" "$pinned2" >/dev/null 2>&1; then
                    image=$pinned2; pulled=1
                fi
            fi
        fi
        if [ -z "$pulled" ]; then
            if [ "$command_word" = check ]; then
                image="none published: it will be built here at the first build, about 20 minutes"
            else
                say "no published engine image can be pulled from $repository for this release (not published yet, or the registry refused);"
                say "the image is built here instead, from the matching release source - never from the development branch."
                build_engine_image
            fi
        fi
    fi
fi

# The failure this guards against: a cached image (built earlier, from an
# older source, or pulled once and kept) carrying an engine below what this
# bundle needs. Read plainly instead of assumed, and rebuilt from the right
# source rather than left to fail deep in the build with two bare numbers and
# no explanation. Skipped for `check` (no build is about to happen) and when
# the image could not be resolved to a real tag at all.
verify_engine_version() {
    [ -n "$engine" ] || return 0
    case "$image" in "none published:"*) return 0 ;; esac
    if [ -n "${src:-}" ] && [ -f "$src/VERSION" ]; then
        # image was just built from this exact source a moment ago (above):
        # its baked-in VERSION is this one, no need to start a container to ask.
        actual=$(tr -d '[:space:]' < "$src/VERSION")
    else
        actual=$(run_runtime run --rm --platform "linux/$arch" "$image" cat /opt/synos/VERSION 2>/dev/null | tr -d '[:space:]')
    fi
    case "$actual" in ""|*[!0-9.]*) return 0 ;; esac
    ver_ge "$actual" "$engine" && return 0
    if [ -n "${SYNOS_BUILDER_IMAGE:-}" ]; then
        fail "SYNOS_BUILDER_IMAGE=$image carries engine $actual, older than this bundle needs ($engine); use a newer image, or unset SYNOS_BUILDER_IMAGE to let this script choose one" 2
    fi
    say "the image $image carries engine $actual, older than this bundle needs ($engine); rebuilding it from the matching source instead of using a stale image."
    build_engine_image forced
}

if [ "$command_word" = check ]; then
    root_args_display=""
    [ -z "$container_root" ] || root_args_display="--root $container_root"
    [ -z "$container_runroot" ] || root_args_display="$root_args_display --runroot $container_runroot"
    say "ready: $run_as $runtime${root_args_display:+ $root_args_display}, $((free_kb / 1048576)) GB free here${store:+, $((store_kb / 1048576)) GB free in $store}, engine image $image"
    exit 0
fi

verify_engine_version

# ---------------------------------------------------------------- this script
# The engine that builds the image also carries the current launcher. A bundle
# downloaded before a launcher fix is refreshed here, once, then restarted, so
# a fixed script reaches every bundle without a trip back to the Studio.
if [ -z "${SYNOS_LAUNCHER_REFRESHED:-}" ]; then
    latest=""
    if [ -n "${src:-}" ] && [ -f "$src/tools/bundle_launcher.sh" ]; then
        latest=$(cat "$src/tools/bundle_launcher.sh")
    else
        latest=$(run_runtime run --rm --platform "linux/$arch" "$image" cat /opt/synos/tools/bundle_launcher.sh 2>/dev/null || true)
    fi
    case "$latest" in
        "#!/bin/sh"*)
            if [ "$latest" != "$(cat "$0")" ]; then
                { printf '%s\n' "$latest" > "$0.new" && chmod 755 "$0.new" && mv "$0.new" "$0"; } \
                    || fail "could not replace $0 with the engine's current launcher (copy tools/bundle_launcher.sh from the engine by hand)" 2
                say "build.sh was updated to the engine's current launcher; starting again."
                refresh_siblings
                SYNOS_LAUNCHER_REFRESHED=1 exec sh "$0" "$@"
            fi
            ;;
    esac
fi

# ---------------------------------------------------------------- the build
mkdir -p dist
# One build of a bundle at a time: two builds would share dist/ and the cache
# volume, and apt in the second stops on the first one's lock ("held by process 0").
if [ -f dist/build.pid ]; then
    other=$(cat dist/build.pid 2>/dev/null || true)
    if [ -n "$other" ] && kill -0 "$other" 2>/dev/null; then
        fail "another build of this bundle is already running (process $other, output in dist/build.log); wait for it to finish, or stop it, then run this script again" 2
    fi
fi
printf '%s\n' "$$" > dist/build.pid
trap 'rm -f dist/build.pid' EXIT INT TERM
# One log per run: the previous one is kept as build.previous.log, so the errors shown below are this run's.
[ -f dist/build.log ] && mv -f dist/build.log dist/build.previous.log
# Seeded here, before the container appends to it, so a development build is
# obvious in the log itself and not only in this script's own terminal output.
if [ "$channel" = development ]; then
    printf 'channel: development (unreleased engine, tip of the main branch - not a release build)\n' > dist/build.log
    say "building with the development engine (unreleased, tip of the main branch) - this image and ISO are not a release build."
else
    printf 'channel: stable\n' > dist/build.log
fi
say "building $manifest with $image"
say "first build about 40 minutes; the cache volume synos-cache-$base-$suite makes the next ones shorter."
say "the full output is kept in dist/build.log"
set +e
# Only stderr is captured here (stdout keeps streaming live, unbuffered, the
# way it always has): a real storage-state mismatch fails before "synos
# build" ever gets to write anything to dist/build.log itself, so that log
# alone cannot show it - this is the one place that failure can actually be
# seen and translated.
run_err_file=".build/build-run-stderr.$$"
run_runtime run --rm --privileged --platform "linux/$arch" \
    -v "$PWD:/bundle:z" \
    -v "synos-cache-$base-$suite:/opt/synos/.build" \
    -v /opt/synos/new_building_os -v /opt/synos/image \
    -e SYNOS_KEYS_DIR=.build/keys \
    -e "SYNOS_UID=$(id -u)" -e "SYNOS_GID=$(id -g)" \
    -e SYNOS_SIGNING_KEY -e SYNOS_SIGNING_KEY_FILE \
    -e SYNOS_CHANNEL="$channel" \
    "$image" synos build /bundle --output /bundle/dist --log /bundle/dist/build.log 2>"$run_err_file"
status=$?
set -e
if [ "$status" -ne 0 ]; then
    run_err=$(cat "$run_err_file" 2>/dev/null || true)
    [ -z "$run_err" ] || printf '%s\n' "$run_err" >&2
    rm -f "$run_err_file"
    explain_storage_mismatch "$run_err" "${container_root:-$runtime's own storage}"
    say ""
    say "the build did not finish (exit code $status). The first errors in dist/build.log:"
    grep -n -m 6 -E 'No space left on device|dpkg: error|dpkg-query: error|^E: |cannot allocate memory|Killed process|FAILED|package error|skipped .*prebuild|Traceback|error:' dist/build.log 2>/dev/null | grep -v 'locale' | cut -c1-200 | sed 's/^/  /'
    say "The complete output is in dist/build.log (the previous run is in dist/build.previous.log). Running ./build.sh again resumes from the cache."
    exit "$status"
fi

iso=$(ls -t dist/*.iso 2>/dev/null | head -n 1 || true)
say ""
say "done. Your image: ${iso:-dist/}"
say "next to it: .sha256 (checksum), .packages.lock, .sbom.cdx.json (what is inside), .resolved.json, build.log"
checksum_name="${iso:-image.iso}"
checksum_name="${checksum_name##*/}"
checksum_name="${checksum_name%.iso}.sha256"
if [ -n "$macos" ]; then
    say "verify it: cd dist && shasum -a 256 -c $checksum_name"
else
    say "verify it: cd dist && sha256sum -c $checksum_name"
fi
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
