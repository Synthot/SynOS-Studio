#!/usr/bin/env bash
# packaging/install-test-engine.sh — install and configure the catalog
# conformance test service (packaging/catalog-conformance/, driven by
# tools/catalog_conformance.py) on a single machine: checks the machine,
# installs whatever it is missing, points podman's storage at a disk with
# room for a build, writes /etc/synos/conformance.yml from answers instead
# of handing over a template to edit, installs the systemd units with a
# dedicated user, and then proves the result actually works.
#
# Usage:
#   packaging/install-test-engine.sh [options]
#
# Options:
#   --engine-root=PATH       checkout this service runs against (default:
#                             autodetected when this script still lives
#                             under packaging/ of a checkout; otherwise
#                             required)
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
#   --service-user=NAME      dedicated system user (default: synos-conformance)
#   --unit-dir=PATH          where systemd unit files go (default:
#                             /etc/systemd/system)
#   --skip-packages          do not detect a distribution family or install
#                             anything; use when packages were installed by
#                             hand on a distribution this script does not
#                             recognize
#   -y, --yes                answer yes to every question this script asks
#   --dry-run                print every action this script would take;
#                             perform none of them
#   -h, --help                this text
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
        debian:pyyaml)   printf 'python3-yaml\n' ;;
        debian:podman)   printf 'podman\n' ;;
        debian:qemu)     printf 'qemu-system-x86\n' ;;
        debian:xorriso)  printf 'xorriso\n' ;;
        debian:tesseract) printf 'tesseract-ocr\n' ;;
        debian:pillow)   printf 'python3-pil\n' ;;
        debian:browser)  printf 'chromium\n' ;;
        fedora:python3)  printf 'python3\n' ;;
        fedora:pyyaml)   printf 'python3-pyyaml\n' ;;
        fedora:podman)   printf 'podman\n' ;;
        fedora:qemu)     printf 'qemu-system-x86\n' ;;
        fedora:xorriso)  printf 'xorriso\n' ;;
        fedora:tesseract) printf 'tesseract\n' ;;
        fedora:pillow)   printf 'python3-pillow\n' ;;
        fedora:browser)  printf 'chromium\n' ;;
        suse:python3)    printf 'python3\n' ;;
        suse:pyyaml)     printf 'python3-PyYAML\n' ;;
        suse:podman)     printf 'podman\n' ;;
        suse:qemu)       printf 'qemu-x86\n' ;;
        suse:xorriso)    printf 'xorriso\n' ;;
        suse:tesseract)  printf 'tesseract-ocr\n' ;;
        suse:pillow)     printf 'python3-Pillow\n' ;;
        suse:browser)    printf 'chromium\n' ;;
        arch:python3)    printf 'python\n' ;;
        arch:pyyaml)     printf 'python-yaml\n' ;;
        arch:podman)     printf 'podman\n' ;;
        arch:qemu)       printf 'qemu-system-x86_64\n' ;;
        arch:xorriso)    printf 'libisoburn\n' ;;
        arch:tesseract)  printf 'tesseract\n' ;;
        arch:pillow)     printf 'python-pillow\n' ;;
        arch:browser)    printf 'chromium\n' ;;
        alpine:python3)  printf 'python3\n' ;;
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

REQUIREMENTS='python3 pyyaml podman qemu xorriso tesseract pillow browser'

requirement_present() {  # generic
    case "$1" in
        python3)  command -v "$PYTHON_BIN" >/dev/null 2>&1 ;;
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

# "mountpoint avail_kb fstype", one per real (non-pseudo, non-network)
# filesystem df knows about.
list_candidate_filesystems() {
    df -Pk --output=target,avail,fstype 2>/dev/null | tail -n +2 | while read -r target avail fstype; do
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
select_storage_path() {  # min_gb [explicit_path]
    local min_gb=$1 explicit=${2:-} min_kb
    min_kb=$((min_gb * 1024 * 1024))
    if [ -n "$explicit" ]; then
        if [ ! -d "$explicit" ] && ! mkdir -p "$explicit" 2>/dev/null; then
            printf 'could not create %s\n' "$explicit" >&2
            return 1
        fi
        local fstype avail
        fstype=$(filesystem_type "$explicit")
        if ! overlay_capable "$fstype"; then
            printf '%s is %s, which cannot back a container overlay filesystem; choose a disk formatted ext4, xfs, btrfs or similar\n' \
                "$explicit" "$fstype" >&2
            return 1
        fi
        avail=$(df -Pk "$explicit" | awk 'NR==2 {print $4}')
        printf 'chosen: %s (%d GB free, %s)\n' "$explicit" "$((avail / 1048576))" "$fstype" >&2
        if [ "$avail" -lt "$min_kb" ]; then
            printf 'note: %s has only %d GB free; a build needs about %d GB.\n' "$explicit" "$((avail / 1048576))" "$min_gb" >&2
        fi
        printf '%s %s\n' "$explicit" "$avail"
        return 0
    fi

    local best_target='' best_avail=0 considered=0 target avail fstype
    while read -r target avail fstype; do
        considered=$((considered + 1))
        printf 'considered: %s (%d GB free, %s)\n' "$target" "$((avail / 1048576))" "$fstype" >&2
        overlay_capable "$fstype" || continue
        [ "$avail" -gt "$best_avail" ] || continue
        best_target=$target
        best_avail=$avail
    done <<CANDIDATES
$(list_candidate_filesystems)
CANDIDATES

    if [ -z "$best_target" ]; then
        printf 'no filesystem capable of backing a container overlay was found among %d candidate(s)\n' "$considered" >&2
        return 1
    fi
    printf 'chosen: %s (%d GB free, the largest of %d candidate(s) considered)\n' "$best_target" "$((best_avail / 1048576))" "$considered" >&2
    if [ "$best_avail" -lt "$min_kb" ]; then
        printf 'note: the largest available filesystem, %s, has only %d GB free; a build needs about %d GB.\n' \
            "$best_target" "$((best_avail / 1048576))" "$min_gb" >&2
    fi
    printf '%s %s\n' "$best_target" "$best_avail"
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
    free_checkout_kb=$(df -Pk "$checkout" | awk 'NR==2{print $4}')
    free_store_kb=$(df -Pk "$store" | awk 'NR==2{print $4}')
    mem_kb=$(awk '/^MemTotal:/{print $2}' "${SYNOS_MEMINFO_FILE:-/proc/meminfo}" 2>/dev/null)
    cpu_count=$(nproc 2>/dev/null || printf '1\n')

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
write_config() {  # catalog_url site_url workdir container_root max_builds jobs service_user
    local catalog_url=$1 site_url=$2 workdir=$3 container_root=$4 max_builds=$5 jobs=$6 service_user=$7
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
}

# -------------------------------------------------------------- the service
SERVICE_USER=${SERVICE_USER:-synos-conformance}
SERVICE_GROUP=${SERVICE_GROUP:-synos-conformance}

create_service_user() {  # home runtime
    local home=$1 runtime=$2
    if id "$SERVICE_USER" >/dev/null 2>&1; then
        say "service user $SERVICE_USER already exists."
    else
        ask_yes "Create system user $SERVICE_USER (home $home)?" \
            || fail "a dedicated system user is required; create $SERVICE_USER yourself, then rerun this installer" 5
        maybe "create system user $SERVICE_USER (home $home)" \
            sudo useradd --system --home "$home" --create-home --shell /usr/sbin/nologin "$SERVICE_USER" \
            || fail "could not create user $SERVICE_USER" 5
    fi
    local group
    group=$(runtime_group "$runtime")
    if getent group "$group" >/dev/null 2>&1; then
        maybe "add $SERVICE_USER to group $group" sudo usermod -aG "$group" "$SERVICE_USER"
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

verify_browser() {
    local bin
    bin=$(command -v chromium || command -v chromium-browser || command -v google-chrome || command -v google-chrome-stable) || return 1
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
    [ -n "$avail_kb" ] || return 1
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
    run_check "headless browser starts"                       verify_browser                                 || failed=1
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

# ------------------------------------------------------------------- main
default_engine_root() {
    local here
    here=$(cd "$(dirname "$0")/.." 2>/dev/null && pwd) || return 1
    [ -f "$here/tools/catalog_conformance.py" ] || return 1
    printf '%s\n' "$here"
}

usage() { sed -n '2,43p' "$0" | sed 's/^# \{0,1\}//'; }

main() {
    local engine_root='' site_url='' catalog_url='' storage_path='' workdir='' \
          entries_per_run=5 jobs='' config_path=/etc/synos/conformance.yml \
          unit_dir=/etc/systemd/system skip_packages=0 arg

    for arg in "$@"; do
        case "$arg" in
            --engine-root=*)     engine_root=${arg#*=} ;;
            --site-url=*)        site_url=${arg#*=} ;;
            --catalog-url=*)     catalog_url=${arg#*=} ;;
            --storage-path=*)    storage_path=${arg#*=} ;;
            --workdir=*)         workdir=${arg#*=} ;;
            --entries-per-run=*) entries_per_run=${arg#*=} ;;
            --jobs=*)            jobs=${arg#*=} ;;
            --config=*)          config_path=${arg#*=} ;;
            --service-user=*)    SERVICE_USER=${arg#*=}; SERVICE_GROUP=${arg#*=} ;;
            --unit-dir=*)        unit_dir=${arg#*=} ;;
            --skip-packages)     skip_packages=1 ;;
            -y|--yes)             ASSUME_YES=1 ;;
            --dry-run)             DRY_RUN=1 ;;
            -h|--help)             usage; return 0 ;;
            *) fail "unrecognized option: $arg (--help for usage)" 2 ;;
        esac
    done

    [ "$(uname -s)" = "Linux" ] || fail "this installs a systemd service; run it on Linux" 2

    if [ -z "$engine_root" ]; then
        engine_root=$(default_engine_root) \
            || fail "pass --engine-root=/path/to/a/checkout/of/this/repository (this script was not found under a checkout's packaging/ directory)" 2
    fi
    [ -f "$engine_root/tools/catalog_conformance.py" ] \
        || fail "$engine_root does not look like a checkout of this engine (no tools/catalog_conformance.py)" 2
    say "engine checkout: $engine_root"

    local family=''
    if [ "$skip_packages" = "1" ]; then
        say "--skip-packages: not detecting a distribution or installing anything."
    else
        family=$(require_family) || exit $?
        say "distribution family: $family ($(pkg_manager_for "$family"))"
        install_requirements "$family"
    fi

    local runtime
    runtime=$(detect_runtime)
    [ -n "$runtime" ] || fail "no podman or docker on PATH even after the install step; install one and rerun" 4
    say "container runtime: $runtime"

    local service_home="/var/lib/$SERVICE_USER"
    if [ "$DRY_RUN" = "1" ]; then
        say "[dry-run] would create system user $SERVICE_USER (home $service_home) and add it to the $(runtime_group "$runtime") group"
    else
        create_service_user "$service_home" "$runtime"
    fi
    maybe "create /etc/synos" sudo install -d /etc/synos

    if is_podman "$runtime"; then
        if [ -z "$storage_path" ]; then
            local picked
            picked=$(select_storage_path "$MIN_BUILD_GB" "") || fail "could not find anywhere to put podman's storage" 5
            storage_path=${picked% *}
        else
            select_storage_path "$MIN_BUILD_GB" "$storage_path" >/dev/null || fail "$storage_path is not usable for podman's storage" 5
        fi
        if [ "$DRY_RUN" = "1" ]; then
            say "[dry-run] would configure podman's rootless storage.conf (graphroot) at $storage_path, owned by $SERVICE_USER"
        else
            configure_podman_storage "$service_home" "$storage_path"
        fi
    else
        docker_storage_note
        if [ -z "$storage_path" ]; then
            local picked_for_workdir
            picked_for_workdir=$(select_storage_path "$MIN_BUILD_GB" "") \
                || fail "could not find a disk with enough room for the working directory" 5
            storage_path=${picked_for_workdir% *}
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
    rendered=$(write_config "$catalog_url" "$site_url" "$workdir" "$config_container_root" "$entries_per_run" "$jobs" "$SERVICE_USER")
    if [ "$DRY_RUN" = "1" ]; then
        say "[dry-run] would write $config_path:"
        printf '%s\n' "$rendered" | sed 's/^/  /'
    elif [ -f "$config_path" ] && [ "$rendered" = "$(cat "$config_path")" ]; then
        say "$config_path already matches these answers; leaving it alone."
    elif [ -f "$config_path" ]; then
        if ask_yes "$config_path already exists and differs; overwrite it?"; then
            printf '%s\n' "$rendered" > "$config_path.new.$$" && mv "$config_path.new.$$" "$config_path"
            say "wrote $config_path"
        else
            warn "leaving $config_path unchanged"
        fi
    else
        printf '%s\n' "$rendered" > "$config_path.new.$$" && mv "$config_path.new.$$" "$config_path"
        say "wrote $config_path"
    fi
    if [ "$DRY_RUN" != "1" ]; then
        sudo install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$workdir" || warn "could not create/chown $workdir"
    fi

    install_service_units "$engine_root/packaging/catalog-conformance" "$unit_dir" "$engine_root" "$PYTHON_BIN"

    if [ "$DRY_RUN" = "1" ]; then
        say "[dry-run] skipping verification (nothing was actually installed)."
        return 0
    fi
    verify_installation "$runtime" "$storage_path" "$engine_root" "$config_path" "$PYTHON_BIN" "$MIN_STORE_GB"
}

# Only runs main when executed, never when sourced (as every test in
# tests/unit/ does, to call the functions above directly against fakes).
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
    main "$@"
fi
