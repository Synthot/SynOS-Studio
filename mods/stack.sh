#!/bin/bash
# Shared by mods 01 and 05: install one group of packages/stack.yml as
# resolved by tools/render_manifest.py into STACK_<GROUP>_* variables.
#   install_stack_group <group> [extra apt arguments...]

install_stack_group() {
    local group="$1"; shift
    local upper="${group^^}"
    local packages_var="STACK_${upper}_PACKAGES"
    local recommends_var="STACK_${upper}_RECOMMENDS"
    local exclude_var="STACK_${upper}_EXCLUDE"
    local arch_var="STACK_${upper}_ONLY_ARCH"
    local packages="${!packages_var:-}"
    local only_arch="${!arch_var:-}"

    if [ -n "$only_arch" ] && ! grep -qw "$TARGET_ARCH" <<< "$only_arch"; then
        print_ok "Stack group $group is not for $TARGET_ARCH; skipped."
        return 0
    fi
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
    local pkg
    for pkg in ${!exclude_var:-}; do
        args+=("${pkg}-")          # apt syntax: trailing dash keeps the package out
    done

    print_ok "Installing stack group $group: $packages"
    # shellcheck disable=SC2086
    apt install -y "${args[@]}" "$@" $packages
    judge "Install stack group $group"
}

export -f install_stack_group
