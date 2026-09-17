# build-guards.sh — validate build-time dependencies before running download.sh
# Source this file, then call `need_cmd <binary> [package-name]`.
# Missing commands print a loud error to stderr and exit 1.
# Call this:  need_cmd msgunfmt gettext

need_cmd() {
    local bin="$1"
    local pkg="${2:-$bin}"
    if ! command -v "$bin" >/dev/null 2>&1; then
        printf '\n%s\n' "============================================================" >&2
        printf 'BUILD ERROR: missing required tool: %s\n' "$bin" >&2
        printf '  Install it with:  sudo apt install -y %s\n' "$pkg" >&2
        printf '%s\n' "============================================================" >&2
        exit 1
    fi
}

# fetch_git_commit <url> <commit> <destination>
# Check out one pinned commit with a shallow fetch, retrying on network
# errors, and keep a cached copy under $SYNOS_SOURCE_CACHE (default
# .build/sources) so a repeated build does not download again.
fetch_git_commit() {
    local url="$1" commit="$2" dest="$3"
    case "$commit" in
        *[!0-9a-f]*|????????????????????????????????????????) ;;
        *) echo "fetch_git_commit: $url pins '$commit'; a shallow fetch needs the full 40-character commit hash" >&2; return 1 ;;
    esac
    # the lib is reached through packages/<name>/lib -> ../_lib: resolve the link first
    local lib_dir; lib_dir="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
    local cache_root="${SYNOS_SOURCE_CACHE:-$lib_dir/../../.build/sources}"
    local cache="$cache_root/$(printf '%s' "$url" | sed 's|[^A-Za-z0-9._-]|_|g')-$commit"
    if [ ! -f "$cache/.synos-fetched" ]; then
        rm -rf "$cache"
        mkdir -p "$cache"
        git -C "$cache" init -q
        git -C "$cache" remote add origin "$url"
        local attempt
        for attempt in 1 2 3 4; do
            if git -C "$cache" -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=60 fetch -q --depth 1 origin "$commit"; then
                break
            fi
            [ "$attempt" -lt 4 ] || { echo "fetch_git_commit: giving up on $url@$commit" >&2; return 1; }
            echo "fetch_git_commit: attempt $attempt failed for $url@$commit; retrying" >&2
            sleep $((attempt * 15))
        done
        git -C "$cache" checkout -q FETCH_HEAD
        touch "$cache/.synos-fetched"
    fi
    rm -rf "$dest"
    cp -a "$cache" "$dest"
}
