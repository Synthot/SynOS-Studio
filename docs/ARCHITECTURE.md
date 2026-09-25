# SynOS architecture and migration plan

## Four axes, one engine

| Axis    | Folder      | Decides                                                        |
|---------|-------------|----------------------------------------------------------------|
| base    | bases/      | bootstrap tool, apt sources, package name map, kernel, bootloader |
| profile | profiles/   | software, installer behaviour, security posture, directory join, desktop policy, Ansible hooks |
| region  | regions/    | locales, keyboard, timezone, mirrors, compliance baseline, eID middleware, legal text |
| brand   | branding/   | id, display name, vendor, URLs, colours, logo, wallpaper, installer slides |

The engine never names a distribution. Mods call `pkg_install <abstract-name>`
and the base's `packages.map` resolves it.

Every image this engine builds is a live-boot installer ISO. A base's
`base.env` lists every suite it can bootstrap and package for
(`SUPPORTED_SUITES`); its optional `live.map` names whichever of those
suites cannot back a Live image at all (no package on that suite's archive
provides the dracut modules `mods/stack.sh`'s `ensure_dracut_live_modules()`
needs) and why. `tools/render_manifest.py` refuses such a suite at
render/`--check` time, naming the reason and what to choose instead, rather
than letting the build reach the same, more expensive refusal inside the
chroot after a long download.

## Migration steps

1. **Done.** Manifest and schema. `tools/render_manifest.py` renders
   `args.sh` (git-ignored) from `manifest.yml`, the base adapter, the
   profile chain, the regions and the brand kit; `make args`, `make validate`.
   The resolved configuration is written to `.build/resolved.json`.
2. **Done.** Base adapters. `build.sh` renders the apt sources from
   `bases/<base>/sources.tmpl` and passes the base's keyring to debootstrap;
   the makefile installs the per-base keyring package and allows cross-base
   builds with a warning. `tools/render_manifest.py` resolves bundles and
   abstract package names through `bases/<base>/packages.map` at render
   time, so the chroot only sees concrete lists: `LANGUAGE_PACKS` (mod 02)
   and `PROFILE_INSTALL_PACKAGES` / `PROFILE_REMOVE_PACKAGES` (new mod 06).
   `manifests/debian-workstation.yml` renders; building it waits for step 4
   because the upstream desktop packages exist only for Ubuntu suites.
3. Neutral namespace. Rename the upstream kernel parameters and
   app IDs to the `synos` namespace; the tests read the namespace
   from one fixture setting.
4. **Done (kit).** Brand kit renderer. `tools/render_brand.py` turns
   `branding/<id>/` into `<id>-branding_<version>_all.deb`: os-release
   (diverted from base-files, `ID=<brand>`, `ID_LIKE` the base), hicolor
   icons and `distributor-logo`, a Plymouth theme registered as the default,
   the GDM logo, the wallpaper (uploaded or derived) with dconf defaults and
   optional locks, and `brand.env` for other tools. `make brand`; mod 07
   installs it in the chroot and verifies the identity; build.sh puts the
   brand GRUB background on the ISO. `make newbrand ID=… NAME=…` scaffolds a
   kit; `branding/example-acme/` is a customer example.
   **4b done (porting).** `packages/stack.yml` describes the product stack
   by role and group; the renderer resolves it into `STACK_<GROUP>_*` lists
   and mods 01 and 05 install the groups. The 53 desktop packages live in
   `packages/`: plain data packages are checked in; packages built by
   scripts keep their sources under `upstream/` with a `prebuild.sh` and
   `includes.txt`; forks of base packages (Firefox from Mozilla, Plymouth,
   sound firmware, software-properties) are described by `fork.json` and
   rebuilt from the upstream binary at build time. `make packages` builds
   the local repository, skipping what the host cannot build and failing
   under `--strict` in the container. The manifest's `packages.takeover`
   is `all`: no stack list names an upstream package any more.
   **Step 3 next:** the vendored contents still carry the upstream
   namespace inside (binary names, dconf database, session mode,
   the upstream namespace); renaming is one coordinated pass
   over `packages/` and `tests/` now that nothing upstream remains.

5. **Done (evidence).** Profiles. Merge chain with `extends` and validation
   (step 1); package lock and CycloneDX SBOM written next to the ISO by
   `tools/sbom.py`, together with the resolved manifest. Builds are pinned
   to the archive snapshot named in `mirrors.snapshot` (snapshot.debian.org,
   snapshot.ubuntu.com); the image is switched back to the live mirrors in
   mod 85 so installed systems receive updates.
   Third-party software (`software.repositories` with armored keys under
   `keys/`, `debs` and `appimages` with SHA-256, `flatpak` remotes and apps
   preinstalled or deferred to first boot) is staged by the renderer into
   `.build/software/` and installed by mod 08; the staged declaration is
   kept under `/usr/share/synos/third-party-software` as evidence.
6. **Done.** Regions. The Live GRUB menu and the locale list follow the
   selected regions, and the acceptance matrix follows the ISO:
   `tests/cases/install.json` declares `live_region: iso-default`, resolved
   after ISO inspection to the first Live entry (the manifest's default
   region). Cases and suites may declare `requires_locales`; the Chinese
   input method and Chinese UI contracts live in suites that require
   `zh_CN`, and anything the image cannot serve is reported as skipped
   with its reason instead of failing. `rime` is effective only in a
   Chinese session. The installed-region probe reads its expected GNOME
   Shell markers per language from
   `tests/assertions/guest/ui/region_markers.py` (zh, en, fr, de, nl).
   Desktop suites need the guest label table in `core.py`, which today
   covers English and Chinese: a French-default image runs the install
   matrix and the region probe, and skips the desktop suites until French
   labels are added to that table.

7. **Done (except Packer).** `ansible/collections/ansible_collections/synos/workstation`
   with roles desktop_branding, base_hardening, directory_join,
   workplace_apps, desktop_policy, fleet_enrollment, container_services
   and apt_cache_ready. `build.sh` runs `playbooks/customize_chroot.yml`
   plus the profile's `ansible.playbooks` inside the chroot (connection
   `community.general.chroot`), passing the resolved profile as variables;
   the renderer refuses playbooks that do not exist and marks Ansible as
   required when the profile carries policy, hardening or enrollment.
   **Build order is a stated contract, not mod numbering:** `mods/85-cleanup-mod`
   deletes `/var/lib/apt/lists` (and the build-time local repository) to
   keep the squashfs small, so it must be the very last thing that touches
   the chroot. `build.sh` runs it as its own phase
   (`install_all_mods.sh cleanup`, function `run_cleanup_mod`) *after*
   `run_ansible_chroot`, not as part of the numbered mod loop in
   `run_chroot` (`install_all_mods.sh early`), so the profile's own package
   installs still see a populated apt cache. Any role that installs
   packages from the archive also includes the `apt_cache_ready` role
   first, which refreshes the index only when it is actually missing —
   belt and braces against a future step that runs after cleanup, or is
   invoked outside `build.sh` altogether. `bases/<base>/Containerfile` and
   `make container-build` run the whole build in a privileged Podman
   container of the target base (its pinned Rust toolchain is documented in
   `docs/BUNDLE.md`, "Building without a checkout"). `ci/gitlab-ci.yml` and
   `.github/workflows/build.yml` are the pipeline templates customers
   include. `packer/synos.pkr.hcl` builds qcow2, vmdk and vhd images from
   the ISO with an answer file from `answers/`; the answer-file contract is
   finalised with the installer rebuild (step 4).
8. **Step 1 done.** Configuration bundles and the `synos` tool. A bundle
   (`docs/BUNDLE.md`, `schema/bundle.schema.json`) is a zip or folder holding
   `bundle.json`, one manifest and the profile, brand kit and keys it needs.
   `tools/synos` validates it, applies it to a checkout and builds it in a
   container (`synos build bundle.zip --pull` uses the builder image CI
   publishes for each base and suite; without `--pull` the image is built
   locally). `--json` and the exit codes (1 invalid, 2 host, 3 build) make it
   the same entry point for a laptop, Ansible and CI; `make container-build`
   calls it. `tools/export_catalog.py` exports the data any front end needs
   (bases, profiles, regions, schemas, engine version, bundle format).
   The web configurator that generates bundles from that catalog lives in its
   own private repository (SynOS-Studio-Configurator); the engine never depends on
   it. Next: accounts, organisations and plans on one platform, per-tenant
   repositories, then the corporate mode of the tool (lock input, Ansible
   module, CI templates driven by bundles).

## Boot menu and regions

The ISO boot menu has one Live entry, "Try or install <name>", plus an
Advanced submenu (safe graphics, persistent USB, media check). It boots the
manifest's default region: locale, keyboard and time zone as kernel
arguments. The other regions are not boot entries: `/etc/synos/live-regions`
lists them in the image and the installer's first screen offers their
languages first, the rest of the world after. A region is therefore what it
always was, the languages installed, the keyboard, time zone, paper size,
compliance baseline and eID support, and never a boot-time question.

The acceptance framework boots a non-default region by appending its
arguments to the single entry; the live session takes the last occurrence
of a kernel argument. The menu is drawn by the brand kit's GRUB theme
(`.build/branding/<id>/grub-theme`, also installed at
`/boot/grub/themes/<id>` with `/etc/default/grub.d/90-<id>-theme.cfg` so
installed systems get the same menu). Because a themed menu cannot be read
from screenshots, the framework then boots through GRUB's command line on
amd64 as it already did on arm64.

## GPU and container services

`hardware.gpu` (none, nvidia, amd, intel) is the one hardware choice a profile
carries: the installer detects everything else, but a compute stack is a
decision (proprietary driver, CUDA, container access). The bases' package maps
translate it (`gpu-nvidia`, `gpu-amd`, `gpu-intel`); NVIDIA's container
toolkit comes from NVIDIA's repository, whose key ships with the engine
(`keys/nvidia-container-toolkit.asc`) so a bundle only references it.

`software.services` lists containers run as system services: the
`container_services` role writes podman quadlet units, images are pulled on
first start (the image stays small and reproducible), and `gpu: true` maps
the GPU into the container the way each vendor needs (CDI for NVIDIA,
generated at boot; /dev/kfd and /dev/dri for AMD and Intel). `ports` are
quadlet `PublishPort=` strings (`host:container`, protocol suffix optional
and defaulting to tcp — `51820:51820/udp` for a UDP-only service such as
WireGuard); `cap_add` lists Linux capabilities the container needs beyond
the runtime default (quadlet `AddCapability=`), for a service that manages
its own network interface or similar; `env_file` names a host path read as
the container's environment (quadlet `EnvironmentFile=`) for a service
whose credentials must never be baked into the image — the profile that
uses it deliberately does not ship that path, the same fail-closed shape
`software.files` already gives the database appliance, so the service
refuses to start until the person creates the file (docs/BUNDLE.md, "The
bundle catalog"). `cap_add` is a closed enum, not a free-form list: a
container that can request `SYS_ADMIN` or `ALL` has defeated the point of
containing it, so `schema/profile.schema.json` enumerates only the
capabilities an appliance in this catalog actually needs today
(`NET_ADMIN`, `NET_RAW`, `NET_BIND_SERVICE`, `SYS_TIME`, `SYS_NICE`) and
refuses anything else outright; widening it is a reviewed change, not
something a bundle can opt into on its own. The catalog
(`profiles/catalog.yml`, `services`) describes the ones Studio offers with
the image per GPU. The `ai-workstation` archetype uses all of this.

## Configuration files a profile ships

`software.files` lists plain configuration files a profile writes into the
image: `{path, content, mode}`, `path` an absolute path under `/etc`,
`/srv`, `/opt` or `/usr/local`, `content` text written verbatim, `mode` an
octal string defaulting to `0644`. This is how a bundle-catalog appliance
becomes more than a copy of a machine kind: an nginx site, a registry's
htpasswd file, a Kubernetes node's sysctl settings.

`tools/render_manifest.py`'s `validate_profile_files` checks every entry
again at render time, byte-accurately, on top of what
`schema/profile.schema.json` already checks: the path allowlist, no `..`,
mode, 16 KiB per file, 64 KiB total across the profile (a schema pattern
cannot sum an array, so the total is render-time only), and a best-effort
symlink check against the render host's own filesystem — that host is
always Debian- or Ubuntu-family, like the eventual build chroot, but it is
not that chroot, so this catches symlinks conventional to the family
(`/etc/mtab` and the like), not one a base image happens to add later.
The `synos.workstation.profile_files` role (next to `container_services`,
same wiring) writes the files into the chroot from the resolved profile.

A shipped file is never a place for a real secret: a credential a
container needs to boot (a database superuser password, a registry's
htpasswd) is always an obvious, documented placeholder the profile's
description or the bundle catalog's summary tells the person to replace,
following the pattern set by `bases/*/packages.map` never carrying a real
mirror credential either.

## Profiles: archetypes and derived profiles

A profile is the role of a machine, never its hardware. Two levels exist and
stay separate:

- **Archetypes ship with the engine** (`profiles/`): `workstation`,
  `developer`, `thin-client`, `kiosk`, `server`, `minimal`. They are
  opinionated, tested defaults and change only with the engine. A new
  archetype is added when several customers ask for the same role (a shared
  hot-desk machine, a high-compliance workstation, a training lab), not
  before. `server` is headless-ready (SSH, Cockpit, containers, ports opened
  at first boot) but the image keeps the desktop: the engine builds one kind
  of media, and the installer lives in it.
- **Derived profiles belong to a company** and extend an archetype
  (`acme-workstation` extends `workstation`), carrying only the differences.
  Studio generates them; nobody copies an archetype. Inheritance keeps a
  company's profiles small enough to review and lets engine updates reach
  every derived profile.

Most users have one kind of machine and never see any of this: the page shows
five cards and produces the derived profile silently. Companies with several
roles get one derived profile per role on one brand kit; a bundle carrying
one brand and several manifests is the planned extension of the bundle
format for that case.

## Rebuild by customers

Customers clone this repository, add their own `profiles/<name>.yml` and
`branding/<name>/`, and run either `make build MANIFEST=...` or
`ansible-playbook ansible/playbooks/build.yml -e manifest=...` on their own
runner. Builds run in a Podman container from the base's Containerfile
against snapshot mirrors pinned by date; the package lock and SBOM are
written next to the ISO.

## Immutable workstation

The differentiator that makes "the machine is a document" a property of the
operating system rather than a promise: a read-only system, atomic updates
with automatic rollback, and machines on the same version that are identical
byte for byte. Phones and Chromebooks work this way; Windows and macOS cannot
offer it to their installed base. The Btrfs subvolume layout the installer
already creates and the snapshot manager already in the stack are most of the
machinery.

### Step I1: transactional updates (on the current base)

- Every package operation runs inside a fresh snapshot of the root subvolume,
  never on the running system; the snapshot becomes the root at the next boot.
  The running root is mounted read-only.
- A version that fails to boot falls back to the previous one automatically;
  the boot menu lists the last versions for a manual choice.
- `synos-admin unlock` gives an administrator a writable root for a session,
  recorded in the journal, for the exceptions that need it.
- Built on the snapshot manager and its apt hook; the installer keeps its
  layout. The acceptance suite finds packages that write under /usr at runtime.

### Step I2: image-based updates

- The pipeline builds each version once; machines fetch the difference
  (casync or zsync over the published repository) and stage it as a new
  subvolume. apt is no longer needed on the device.
- Update and rebuild become the same operation: what the pipeline tested,
  with its SBOM and lock, is exactly what runs. Drift becomes impossible.
- /etc gets a three-way merge between the image version and local changes;
  /var and /home stay persistent.
- Kernel modules and drivers move from on-device builds (driver centre) into
  the pipeline, so they are part of the version.

### Step I3: applications outside the system

- User applications install as Flatpaks; developers get containers
  (Distrobox/Podman). Neither touches the system image.
- Company software enters the image through the profile and is therefore
  part of the version; the app store shows both sources with their origin.

### Order

After the first image and acceptance run are validated, since immutability
builds on the same packages. I1 first (contained: snapshot manager and the
update path), I3 can start at once in the profiles, I2 when the platform
produces versioned images anyway.

