#!/bin/bash
set -e                  # exit on error
set -o pipefail         # exit on pipeline error
set -u                  # treat unset variable as error
#==========================
# Stack group "apt": package source configuration, archive keyring and the
# base-files fork. Resolved from packages/stack.yml; roles listed in the
# come from the local SynOS repository.
#==========================
# shellcheck disable=SC1091
source /root/mods/stack.sh

if [ -f /etc/apt/sources.list.d/synos-local.sources ]; then
    print_ok "Local SynOS repository available"
fi

install_stack_group apt

# When the manifest names a published repository, the image must be able to
# update from it: prove that here, at build time, rather than on a customer's
# first apt update. The source was installed by synos-apt-config.
if [ -n "${SYNOS_REPO_URL:-}" ] && [ -f /etc/apt/sources.list.d/synos.sources ]; then
    print_ok "Checking the published repository ${SYNOS_REPO_URL}${TARGET_SUITE}/ ..."
    if ! apt-get update -o Dir::Etc::SourceList=/etc/apt/sources.list.d/synos.sources \
            -o Dir::Etc::SourceParts=- -o APT::Get::List-Cleanup=0 2>&1 | tee /tmp/synos-repo-check.log \
        || grep -qE "^(W|E):.*synos" /tmp/synos-repo-check.log; then
        print_error "The repository named in packages.repository is unreachable or its signature is not trusted:"
        cat /tmp/synos-repo-check.log
        exit 1
    fi
    judge "Published repository is reachable and signed"
fi
