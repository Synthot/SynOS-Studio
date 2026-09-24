#!/bin/sh
# Run inside the target (the build chroot, or an installed system) by the
# apt_cache_ready role. Any role that installs packages from the archive
# includes that role first instead of trusting that an earlier build step
# left a populated cache behind (mods/85-cleanup-mod deletes
# /var/lib/apt/lists to keep the image small, and it is meant to run last,
# but a role should not silently break if it ever runs after some other
# step that also clears the cache).
#
# Cheap by construction: it only calls `apt-get update` when the index is
# actually missing, so a normal build (where the cache is already warm)
# never pays for a second download.
set -e

# SYNOS_APT_LISTS_DIR lets tests point this at a throwaway directory instead
# of the real apt state; unset (the normal case, inside the chroot or an
# installed system) it is /var/lib/apt/lists.
lists_dir="${SYNOS_APT_LISTS_DIR:-/var/lib/apt/lists}"

has_index() {
    # Real index files sit directly under lists_dir; "lock" and "partial"
    # are apt's own housekeeping entries, not package data.
    for entry in "$lists_dir"/*; do
        case "$(basename "$entry")" in
            lock|partial) ;;
            *) [ -e "$entry" ] && return 0 ;;
        esac
    done
    return 1
}

if has_index; then
    echo "apt_cache_ready: package index already present; not refreshing."
else
    echo "apt_cache_ready: package index missing; running apt-get update."
    apt-get update
fi
