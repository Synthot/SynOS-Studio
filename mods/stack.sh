#!/bin/bash
# Shared by mods 01 and 05: install one group of packages/stack.yml as
# resolved by tools/render_manifest.py into STACK_<GROUP>_* variables.
#   install_stack_group <group> [extra apt arguments...]

# The dracut modules the Live initrd needs (mod 80's --add list draws from
# this so the two never drift). "dmsquash-live" and "dmsquash-live-autooverlay"
# come from upstream dracut; "overlayfs" from dracut-core; "synos-live-layers"
# is our own package.
LIVE_DRACUT_MODULES="dmsquash-live dmsquash-live-autooverlay overlayfs synos-live-layers"
# The subset of LIVE_DRACUT_MODULES that the dracut-live package provides.
# packages/stack.yml already tries to install dracut-live (marked optional,
# since Ubuntu resolute does not have it as an installable package at all —
# dracut-core carries the module there instead). This function is the
# fallback for a suite where that guess turns out wrong, and the actual
# authority for whether the Live image can build: ask dracut what modules it
# has, rather than trust a package having installed.
DRACUT_LIVE_PACKAGE_MODULES="dmsquash-live dmsquash-live-autooverlay"

# Modules from LIVE_DRACUT_MODULES that `dracut --list-modules` does not
# report, space-separated (empty if none are missing). --no-kernel: we are
# asking what modules dracut has available, not what it would build for a
# specific kernel — without it, dracut hard-fails ("Cannot find module
# directory /lib/modules/<running kernel>/") whenever the build chroot has
# not installed a kernel matching the *build host's* running kernel yet,
# which this check must not depend on either way.
missing_dracut_live_modules() {
    local available module missing=""
    available=$(dracut --no-kernel --list-modules 2>/dev/null || true)
    for module in $LIVE_DRACUT_MODULES; do
        grep -qx "$module" <<< "$available" || missing="$missing $module"
    done
    echo "${missing# }"
}

# Called once right after the "live" stack group installs (mod 05, so a
# missing module fails in the first few minutes of a build, not 30+ minutes
# later when mod 80 hands the same gap to dracut) and again, cheaply, right
# before mod 80 invokes dracut for the Live initrd. Installs dracut-live when
# it would actually help; fails with the exact missing module and package
# otherwise, rather than letting the Live image build silently lose a module.
ensure_dracut_live_modules() {
    local missing fixable=0 module
    missing=$(missing_dracut_live_modules)
    if [ -z "$missing" ]; then
        print_ok "Dracut has every Live module it needs ($LIVE_DRACUT_MODULES)"
        return 0
    fi

    for module in $missing; do
        grep -qw "$module" <<< "$DRACUT_LIVE_PACKAGE_MODULES" && fixable=1
    done
    if [ "$fixable" -eq 1 ]; then
        print_warn "Dracut is missing:$missing — installing dracut-live"
        apt-get install -y --no-install-recommends dracut-live
        missing=$(missing_dracut_live_modules)
    fi

    if [ -n "$missing" ]; then
        print_error "Dracut is still missing:$missing after installing dracut-live"
        print_error "No installed package on $TARGET_SUITE provides them; the Live image cannot build. Check what dracut-core or dracut-live carries on $TARGET_SUITE and, if it is a new gap, extend DRACUT_LIVE_PACKAGE_MODULES or packages/stack.yml's \"live\" group."
        exit 1
    fi
    print_ok "dracut-live provided the missing module(s); Dracut now has: $LIVE_DRACUT_MODULES"
}

export -f missing_dracut_live_modules ensure_dracut_live_modules

install_stack_group() {
    local group="$1"; shift
    local upper="${group^^}"
    local packages_var="STACK_${upper}_PACKAGES"
    local recommends_var="STACK_${upper}_RECOMMENDS"
    local exclude_var="STACK_${upper}_EXCLUDE"
    local arch_var="STACK_${upper}_ONLY_ARCH"
    local optional_var="STACK_${upper}_OPTIONAL"
    local packages="${!packages_var:-}"
    local only_arch="${!arch_var:-}"

    if [ -n "$only_arch" ] && ! grep -qw "$TARGET_ARCH" <<< "$only_arch"; then
        print_ok "Stack group $group is not for $TARGET_ARCH; skipped."
        return 0
    fi

    # packages/stack.yml's `only_base` is decided at render time (this host
    # has no apt connection to the target suite); `optional: true` is for the
    # narrower case only that suite's own archive can settle — a package
    # folded into a sibling on some releases (dracut-install into
    # dracut-core on Ubuntu jammy) and not installable under its own name
    # there at all. `apt install` has no per-package "if it exists" syntax
    # the way a Depends field has alternatives, so check first: asking apt
    # to install a name that plain does not exist aborts the whole group,
    # not just that one package.
    local pkg kept="" optional opt
    for pkg in $packages; do
        optional=0
        for opt in ${!optional_var:-}; do
            [ "$opt" = "$pkg" ] && optional=1
        done
        if [ "$optional" -eq 1 ] && ! apt-cache show "$pkg" >/dev/null 2>&1; then
            print_warn "$pkg is not installable on $TARGET_SUITE (packages/stack.yml marks it optional); dropping it from stack group $group"
            continue
        fi
        kept="$kept $pkg"
    done
    packages="${kept# }"

    if [ -z "$packages" ]; then
        print_warn "Stack group $group resolves to no packages; skipped."
        return 0
    fi

    local args=()
    if [ "${!recommends_var:-false}" = "true" ]; then
        args+=(--install-recommends)
    else
        args+=(--no-install-recommends)
    fi
    for pkg in ${!exclude_var:-}; do
        args+=("${pkg}-")          # apt syntax: trailing dash keeps the package out
    done

    print_ok "Installing stack group $group: $packages"
    # shellcheck disable=SC2086
    apt install -y "${args[@]}" "$@" $packages
    judge "Install stack group $group"
}

export -f install_stack_group
