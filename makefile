# Makefile —— SynOS build orchestrator
SHELL         := /usr/bin/env bash
.DEFAULT_GOAL := current

# The build manifest is the single source of truth. args.sh is rendered from
# it (plus the base, profile, region and brand files it names) and is never
# edited by hand. Override with: make MANIFEST=manifests/acme.yml
MANIFEST ?= manifest.yml
MANIFEST_INPUTS := $(MANIFEST) tools/render_manifest.py \
  $(wildcard schema/*.json bases/*/base.env profiles/*.yml regions/*.yml branding/*/brand.yml)

DEPS_COMMON := \
  binutils \
  curl \
  debootstrap \
  fonts-unifont \
  librsvg2-bin \
  fakeroot \
  ansible \
  apt-utils \
  dpkg-dev \
  python3-yaml \
  python3-jsonschema \
  python3-pil \
  python3-jinja2 \
  gnupg \
  isomd5sum \
  squashfs-tools \
  sbsigntool \
  xorriso \
  grub2-common \
  mtools \
  dosfstools \
  util-linux

# Pick arch-specific GRUB packages at run time so the same Makefile works on
# both amd64 and arm64 build hosts. amd64 uses grub-pc-bin for its El Torito
# image; both architectures declare the signed GRUB and shim payloads directly
# because build.sh creates a Secure Boot capable removable EFI image.
DEPS_amd64 := \
  grub-pc-bin \
  grub-efi-amd64 \
  grub-efi-amd64-signed \
  shim-signed

# The ARM64 EFI payload is built with the target root's GRUB modules, signed
# GRUB image and shim inside a private mount namespace.  Installing foreign
# shim-signed on an amd64 build host would conflict with the host's own signed
# bootloader, so ARM64 deliberately has no target-EFI host packages here.
DEPS_arm64 :=

# Per-base host packages: the archive keyring debootstrap verifies against.
DEPS_base_ubuntu := ubuntu-keyring
DEPS_base_debian := debian-archive-keyring

HOST_ARCH ?= $(shell dpkg --print-architecture)
# args.sh is rendered from the manifest; render it before reading it.
TARGET_ARCH ?= $(shell env -u TARGET_ARCH bash -c 'python3 tools/render_manifest.py --manifest "$(MANIFEST)" >/dev/null 2>&1; source ./args.sh; printf "%s\n" "$$TARGET_ARCH"')
BASE_ID ?= $(shell bash -c 'source ./args.sh 2>/dev/null; printf "%s\n" "$${BASE_ID:-ubuntu}"')
DEPS_CROSS :=
ifneq ($(HOST_ARCH),$(TARGET_ARCH))
DEPS_CROSS += qemu-user-binfmt
endif
DEPS := $(DEPS_COMMON) $(DEPS_$(TARGET_ARCH)) $(DEPS_base_$(BASE_ID)) $(DEPS_CROSS)

.PHONY: current clean bootstrap menuconfig buildtorrent test help args validate brand newbrand container-build packages repo-key publish-repo serve-repo check bundle catalog

help:
	@echo "Usage:"
	@echo "  make          (or make current)   Build current language"
	@echo "  make args                         Render args.sh from $(MANIFEST)"
	@echo "  make validate                     Validate the manifest without writing"
	@echo "  make brand                        Render the brand kit package from branding/<brand>/"
	@echo "  make newbrand ID=acme NAME=...    Scaffold branding/<ID>/ (LOGO=path.svg optional)"
	@echo "  make container-build [PULL=1]     Build inside a container of the target base (PULL=1 uses the published builder image)"
	@echo "  make packages                     Build packages/ into the local repository .build/repo"
	@echo "  make publish-repo DEST=...        rsync .build/repo to user@host:/path or /path (per suite)"
	@echo "  make serve-repo [PORT=8080]       Serve .build/repo over HTTP for a test build"
	@echo "  make check                        Host requirements for a container build (tools/synos check)"
	@echo "  make bundle BUNDLE=x.zip          Apply a configuration bundle to this checkout (tools/synos bundle apply)"
	@echo "  make catalog                      Regenerate profiles/catalog.apps.yml from the base archives (network)"
	@echo "  make menuconfig                   Configure build options (TUI, legacy)"
	@echo "  make clean                        Remove build artifacts"
	@echo "  make bootstrap                    Validate environment and deps"
	@echo "  make buildtorrent                 Generate torrents for dist/*.iso"
	@echo "  make test                         Test the newest ISO in dist/"
	@echo "  make test ISO=... ARCH=...        Test an explicit ISO"

args: args.sh

args.sh: $(MANIFEST_INPUTS)
	@python3 tools/render_manifest.py --manifest "$(MANIFEST)"

validate:
	@python3 tools/render_manifest.py --manifest "$(MANIFEST)" --check

brand: args.sh
	@python3 tools/render_brand.py --manifest "$(MANIFEST)"

container-build: args.sh
	@python3 tools/synos build "$(MANIFEST)" $(if $(IMAGE),--image "$(IMAGE)") $(if $(PULL),--pull) $(if $(BUILD_LOG),--log "$(BUILD_LOG)")

check:
	@python3 tools/synos check

catalog:
	@python3 tools/build_catalog.py

bundle:
	@test -n "$(BUNDLE)" || { echo "usage: make bundle BUNDLE=path/to/config.zip [FORCE=1]"; exit 2; }
	@python3 tools/synos bundle apply "$(BUNDLE)" $(if $(FORCE),--force)

newbrand:
	@test -n "$(ID)" -a -n "$(NAME)" || { echo "usage: make newbrand ID=acme NAME=\"Acme Workstation\" [LOGO=logo.svg]"; exit 2; }
	@python3 tools/new_brand.py --id "$(ID)" --name "$(NAME)" $(if $(LOGO),--logo "$(LOGO)")

bootstrap: args.sh
	@if [ "$$(id -u)" -eq 0 ] && [ -z "$$SYNOS_IN_CONTAINER" ]; then \
	  echo "Error: Do not run as root (the container build sets SYNOS_IN_CONTAINER)"; \
	  exit 1; \
	fi
	@if ! lsb_release -i | grep -qE "(Ubuntu|Debian|Tuxedo|SynOS)"; then \
	  echo "Error: Unsupported OS — only Ubuntu, Debian, Tuxedo or SynOS allowed"; \
	  exit 1; \
	fi
	@host=$$(lsb_release -cs); host_id=$$(lsb_release -is | tr 'A-Z' 'a-z'); \
	target=$$(grep -oP 'export TARGET_SUITE="\K[^"]+' args.sh); \
	base=$$(grep -oP 'export BASE_ID="\K[^"]+' args.sh); \
	case "$$host_id" in synos*|tuxedo*) host_id=ubuntu ;; esac; \
	if [ "$$host_id" = "$$base" ] && [ "$$host" != "$$target" ]; then \
	  echo "Error: Host codename '$$host' != target '$$target'"; \
	  echo "Build machine must run the same $$base release as the target ISO."; \
	  exit 1; \
	elif [ "$$host_id" != "$$base" ]; then \
	  echo "[MAKE] Warning: cross-base build ($$host_id host, $$base $$target target)."; \
	  echo "[MAKE] Supported through debootstrap; the container build (migration step 7) is the tested path."; \
	fi
	@sudo -v

	@missing="" ; \
	for pkg in $(DEPS); do \
	  if ! dpkg -s $$pkg >/dev/null 2>&1; then \
	    missing="$$missing $$pkg"; \
	  fi; \
	done; \
	if [ -n "$$missing" ]; then \
	  echo "Missing packages:$$missing"; \
	  echo "Installing missing dependencies..."; \
	  sudo apt-get update && sudo apt-get install -y$$missing; \
	else \
	  echo "[MAKE] All required packages are already installed."; \
	fi

menuconfig:
	@echo "[MAKE] args.sh is generated from $(MANIFEST); edits made here are overwritten by 'make args'."
	@echo "[MAKE] Edit $(MANIFEST) (or the profile/region/brand files) instead."
	@./menuconfig.sh

PACKAGES_FLAGS ?=
packages: args.sh
	@python3 tools/build_packages.py --manifest "$(MANIFEST)" $(PACKAGES_FLAGS)

publish-repo: packages
	@test -n "$(DEST)" || { echo "usage: make publish-repo DEST=user@host:/var/www/synos"; exit 2; }
	@python3 tools/publish_repo.py publish --manifest "$(MANIFEST)" --dest "$(DEST)"

serve-repo: packages
	@python3 tools/publish_repo.py serve --manifest "$(MANIFEST)" --port $(or $(PORT),8080)

repo-key: packages
	@echo "[MAKE] The signing key is created automatically by 'make packages' when missing (keys/private)."

current: bootstrap brand packages
	@echo "[MAKE] Building $(MANIFEST)..."
	@./build.sh

buildtorrent:
	@if [ ! -d dist ]; then \
	  echo "[ERROR] dist/ directory not found. Run 'make' first."; \
	  exit 1; \
	fi; \
	shopt -s nullglob; isos=(dist/*.iso); \
	if [ $${#isos[@]} -eq 0 ]; then \
	  echo "[ERROR] No ISO files found in dist/."; \
	  exit 1; \
	fi; \
	if ! command -v mktorrent &>/dev/null; then \
	  echo "[MAKE] Installing mktorrent..."; \
	  sudo apt-get update && sudo apt-get install -y mktorrent; \
	fi; \
	tracker=$$(mktemp); \
	curl -fsSL -o "$$tracker" https://raw.githubusercontent.com/ngosang/trackerslist/master/trackers_best.txt; \
	mapfile -t raw < "$$tracker"; \
	rm "$$tracker"; \
	announce_args=(); \
	for t in "$${raw[@]}"; do \
	  [ -n "$$t" ] && announce_args+=(-a "$$t"); \
	done; \
	for iso in "$${isos[@]}"; do \
	  base="$${iso%.iso}"; \
	  echo "[MAKE] Generating torrent for $$(basename "$$iso")..."; \
	  rm -f "$${base}.torrent"; \
	  mktorrent "$${announce_args[@]}" -o "$${base}.torrent" "$$iso"; \
	done; \
	echo "[MAKE] Torrent generation complete."

test:
	@$(MAKE) --no-print-directory args.sh
	@python3 tests/run.py clean-disks --root test-results
	@PYTHONPATH=tests python3 -m unittest discover -s tests/unit -p 'test_*.py'
	@iso='$(ISO)'; arch='$(ARCH)'; \
	if [ -z "$$iso" ]; then \
		iso=$$(find dist -maxdepth 1 -type f -name '*.iso' -printf '%T@ %p\n' 2>/dev/null | sort -nr | sed -n '1s/^[^ ]* //p'); \
		if [ -z "$$iso" ]; then \
			echo "[ERROR] No ISO found in dist/. Build one or pass ISO=/path/to/image.iso"; \
			exit 2; \
		fi; \
		echo "[TEST] Auto-selected newest ISO: $$iso"; \
	fi; \
	if [ -z "$$arch" ]; then \
		case "$$(basename "$$iso")" in \
			*amd64*.iso) arch=amd64 ;; \
			*arm64*.iso|*aarch64*.iso) arch=arm64 ;; \
			*) echo "[ERROR] Cannot infer architecture from $$iso; pass ARCH=amd64|arm64"; exit 2 ;; \
		esac; \
	fi; \
	python3 tests/run.py --iso "$$iso" --arch "$$arch" \
		$(TEST_ARGS)

clean:
	@echo "[MAKE] Cleaning build artifacts..."
	@./clean_all.sh
	@echo "[MAKE] Clean complete."
