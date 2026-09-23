# Configuration bundles

A configuration bundle is everything the build engine needs to produce one
image, packed by a front end (the web configurator, a script, a person) and
built by `tools/synos` on any machine with podman or docker. The bundle is
the contract between front ends and the engine: front ends are free to
change, the engine only promises to build bundles of the formats it lists.

## Layout (format 1)

    bundle.json                 descriptor (schema/bundle.schema.json)
    manifests/<name>.yml        the manifest to build, named by bundle.json
    profiles/<id>.yml           profiles the manifest uses, unless they ship with the engine
    branding/<id>/brand.yml     brand kit; logo.svg and optional assets next to it
    branding/<id>/logo.svg
    keys/<repo>.asc             signing keys of third-party repositories the profile adds
    answers/<file>.yml          unattended-install answer files
    README.md                   for the person who opens the zip; not applied
    build.sh                    launcher, Linux and macOS: builds the bundle with podman or docker
    build.command               launcher, macOS: double-clickable wrapper around build.sh
    build.ps1, build.cmd        launcher, Windows: Docker Desktop or Podman Desktop (WSL 2)

Nothing else is accepted: a bundle cannot carry build scripts, mods,
packages or anything outside those folders, and paths must stay inside the
bundle (`..` and absolute paths are rejected). Regions and bases are engine
data; a bundle references them by id and the engine reports the ones it
does not have.

## Archive formats

A bundle travels as a zip file (Windows, macOS) or a tar.gz file (Linux),
so that nobody installs a decompressor: both open with the tools every
system ships. The content is the same bundle either way; `tools/synos`
accepts both, plus an unpacked directory. Archives may only contain regular
files; links and devices are refused.

## bundle.json

```json
{
  "format": 1,
  "name": "acme-workstation",
  "manifest": "manifests/acme-workstation.yml",
  "engine": {"min": "0.1.0"},
  "generator": {"name": "SynOS configurator", "version": "0.1.0"},
  "created": "2026-09-13T20:00:00Z",
  "description": "Acme Workstation — ubuntu resolute amd64, profile acme-workstation",
  "files": [{"path": "manifests/acme-workstation.yml", "sha256": "…"}, "..."],
  "signature": {"alg": "ed25519", "key_id": "studio-2026", "value": "…"}
}
```

`files` may list plain names or objects carrying the SHA-256 of each file's
bytes (every file except `bundle.json` itself). With digests, `tools/synos
bundle validate` reports which files were changed after generation, which
are missing and which are not listed; the bundle stays valid either way,
because a bundle is data and a person is allowed to edit it by hand.

`signature`, when present, is an Ed25519 signature by the generator's
service over the canonical JSON (sorted keys, no spaces) of `format`,
`name`, `manifest` and the digest list sorted by path. The engine reports
it as `valid`, `invalid`, `unknown-signer` (key not in `schema/signers.json`
or the file named by `SYNOS_TRUSTED_SIGNERS`) or `unverifiable` (no
`python3-cryptography`). A signature proves who generated the bundle and
that nothing changed since; it never proves the content is safe, so the
engine validates signed and unsigned bundles exactly the same way.

`format` and `manifest` are required. `engine.min` is the oldest engine
(`VERSION` at the root of this repository) that understands the bundle: the
engine refuses to build when it is older, so a bundle generated against a
newer catalog fails early instead of producing a wrong image.

## Building without a checkout

The builder container images CI publishes carry the engine itself at
`/opt/synos` (`bases/<base>/Containerfile`, `COPY . /opt/synos`), tagged
`<base>-<suite>` (latest) and `<base>-<suite>-v<VERSION>` (per release).

Every base's Containerfile installs its own suite's `build-essential` (the
oldest LTS suite the catalog pins, noble, ships GCC 13; that is fine for the
C packages), but pins a single Rust toolchain with `rustup` instead of
whatever `cargo`/`rustc` that suite's archive happens to carry: the engine's
own Rust packages need a lock-file format and an edition newer than a
still-supported LTS suite's packaged Rust understands. The version lives in
one file, `bases/rust-toolchain.txt`, read by every base's Containerfile, so
bumping it is a one-line change that reaches ubuntu, debian and any base
added from `bases/_template/Containerfile` the same way. `rustup toolchain
install` verifies every component it downloads against the signed release
manifest it fetches from `static.rust-lang.org` before installing it; no
separate checksum bookkeeping is needed here. `synos-whisper-worker` is the
one package that also needs a newer C++ frontend (GCC 15, for the pinned
whisper.cpp release); rather than growing every builder image for one
package, its `prebuild.sh` fetches the pinned `gcc-15-x86-64-linux-gnu`
driver, frontend and fixed-include packages itself (checksummed, from the
Ubuntu archive's pool, which keeps historical `.deb`s regardless of the
suite you build), and uses them directly instead of assuming the host
already has GCC 15. GCC 15 also emits assembly (Ubuntu's package-metadata
notes) that an older suite's own `as` cannot read, so the same recipe pins
a matching `binutils` and puts it first on `PATH` for that one compile:
GCC's own `-B` search does not override the assembler path it was
configured with, but `PATH` does. Three other GTK packages
(`synos-swapcontrol-gtk`, `synos-ufwall-gtk`, `synos-yubikey-manager`) ask
Cargo's `libadwaita` binding for its `v1_6` feature although nothing in
their code needs more than `v1_5`; noble's own libadwaita is 1.5, so their
`Cargo.toml` requests `v1_5` instead — a dependency pin, not a toolchain
fetch, since the gap was in what the package asked for rather than in what
the base image can build. When a future suite's own toolchain is finally
new enough, the fix is to drop the corresponding fetch (or the whole
rustup block, once every supported suite's packaged Rust is new enough on
its own) rather than to keep pinning ahead of it.

The launchers every bundle ships (`tools/bundle_launcher.*`, exported in the
catalog and written into the zip by the front end) do this and nothing else:

1. read `bundle.json` and the manifest for the engine version, base and suite;
   pull the image pinned to that engine, else the current one; when neither
   can be pulled (not published yet, a registry refusing anonymous pulls, no
   network to it), build the same image locally from the engine source
   (the public repository's `main` archive, or `SYNOS_ENGINE_SOURCE`) and keep
   it named after that source, so a bundle never depends on the registry
   being reachable; then compare themselves with the launcher inside that
   engine and, when it is newer, replace themselves (and best-effort refresh
   the other three launcher files from the same source) and start again, so a
   launcher fix reaches bundles downloaded before it. `./build.sh update` (or
   `.\build.ps1 update`) does the same fetch-and-replace for all four files on
   demand, without building: from `SYNOS_ENGINE_SOURCE` when set, otherwise
   from `SYNOS_LAUNCHER_URL` (default the engine's `tools/` on GitHub);
2. for `check` and a plain build (not `update`), first look, with a short
   timeout, for launchers newer than the bundle's own; a difference offers to
   update now (default yes; `--yes`/`SYNOS_YES=1` updates without asking, no
   terminal prints how to run `update` and continues); a network problem is a
   one-line note, never a build failure. `SYNOS_NO_UPDATE_CHECK=1` skips this;
3. find podman or docker; when neither exists, offer to install podman with
   the distribution's package manager (apt, dnf, zypper, pacman, apk, brew;
   `--yes` or `SYNOS_YES=1` answers for scripts). On Windows, `build.ps1`
   offers Docker Desktop through winget. Rootless podman and an unreachable
   docker socket are used through sudo: the build mounts filesystems and
   loop-mounts the EFI image, which needs a root runtime;
4. check 40 GB free next to the bundle, then check the container runtime's own
   storage (below) before pulling the image pinned to the engine version, or
   the latest one for that base and suite when no pinned image exists;
5. run it privileged with the bundle mounted at `/bundle`, a named volume
   `synos-cache-<base>-<suite>` at `/opt/synos/.build` (package, apt and
   source caches, signing key), anonymous volumes for the chroot and the
   image staging (GRUB must probe a real filesystem, not the container's
   overlay), and `synos build /bundle --output /bundle/dist --log /bundle/dist/build.log`;
6. print where the ISO is and how to write it to a USB stick or boot it in a
   virtual machine; on failure, where to look in `build.log` and that running
   again resumes from the cache.

### Where the build's bytes land

Running the launcher from a roomy disk does not put the build there: the
chroot, the image layers and the cache live in the container runtime's own
storage, normally on the system disk, wherever that runtime was installed.
A machine with a small system disk and a large second one needs the runtime
told to use it, not the bundle moved.

- **podman** takes a location per invocation. Set `SYNOS_CONTAINER_ROOT`
  (or pass `--container-root=<path>` to `build.sh`/`tools/synos build`, or
  `-ContainerRoot` to `build.ps1`) to a directory on the disk with room; no
  root and no daemon restart are needed, and the same value on the next run
  reuses what is already there rather than starting over. `SYNOS_CONTAINER_RUNROOT`
  (`--container-runroot`/`-ContainerRunroot`) does the same for podman's small
  state directory, which otherwise stays at podman's own default. The chosen
  path must be on a filesystem that can back a container's overlay (ext4, xfs,
  btrfs and similar; not vfat, exFAT, NTFS or a network share) — `build.sh`
  checks this and says so plainly rather than failing deep inside the build.
- **docker**'s storage is one setting for the whole daemon: there is no
  per-build override. Asking the launcher or `tools/synos build` for a
  location while running under docker is refused, with the two ways to move
  it instead (`data-root` in `/etc/docker/daemon.json` and a service restart
  for a plain install; the same key in
  `/var/snap/docker/current/config/daemon.json`, a `snap restart docker`, and
  a one-time `snap connect docker:removable-media` so it can even reach
  `/mnt`, for the snap) — or install podman, or build on a machine that
  already has room where docker keeps its images.
- `tools/build_matrix.py` and `tools/catalog_conformance.py` accept the same
  setting (`--container-root`/`--container-runroot` on the former,
  `container_root`/`container_runroot` in the latter's config) and pass it to
  every target's `tools/synos build`; `tools/host_resources.py` measures free
  disk there instead of the runtime's default when it is set, so `--jobs auto`
  is derived from the disk actually being used, not the one being avoided.

If your system disk is small, this is the fix: point `SYNOS_CONTAINER_ROOT`
at the roomier one and nothing about how you run the launcher changes.

**The launcher offers this up front, on podman, before it becomes a
problem.** When no location was given and podman's own storage is not
clearly big enough, `build.sh`/`build.ps1` (not `check`, not `--yes`, not
without a terminal, not when podman's own storage clearly has room) asks
once:

    podman would keep this build under <podman's default storage> (<N> GB free); it needs 30 GB.
    Use <bundle>/.build/container-storage next to this bundle instead (<M> GB free there)? [Y/n]

Enter (or "y") accepts the directory beside the bundle; that alternative is
only offered as the default answer when the bundle's own disk actually has
more room than podman's default. The answer — that path, or "keep the
default" — is written to `.build/container-root` next to the bundle, so the
next run says where the storage is instead of asking again:

    using the remembered build storage: <path> (change: --container-root=<path>; forget: rm .build/container-root)

`--container-root=<path>` (or `SYNOS_CONTAINER_ROOT`) always wins over the
remembered value for that run, and also becomes the new remembered value;
deleting `.build/container-root` forgets the choice and asks again next
time. Never asked on docker, where there is no per-build answer to offer —
podman is only mentioned there as an alternative once space is actually
short (see the docker refusal above).

Inside the image `synos build` detects `SYNOS_IN_CONTAINER` and runs the
engine's make directly; the ISO, its evidence files and the log are copied to
`dist/` next to the bundle and handed to the invoking user (`SYNOS_UID`/`SYNOS_GID`).
A user therefore unpacks and runs one file; the launcher installs the only
thing it needs. Releases must be tagged `v<VERSION>` so the pinned image
exists.

macOS: `build.command` runs `build.sh`, which adds Homebrew to the PATH,
installs podman through Homebrew when no runtime exists, creates a rootful
podman machine sized for the build (4 CPUs, 8 GB, 80 GB disk) or checks that
Docker Desktop is running with enough memory, and pulls and runs the image
with `--platform linux/<manifest arch>`. On Apple Silicon an amd64 bundle
builds under emulation (Rosetta in Docker Desktop, qemu-user in podman
machine) and takes several times longer; an arm64 bundle builds natively.
The builder images are published for amd64 and arm64 (multi-architecture
manifests). This path is written from the runtimes' documentation and has
not been exercised on a Mac by the project yet.

## Commands

    tools/synos check                        podman/docker, disk, python modules
    tools/synos bundle validate <zip|dir>    structure, schemas, references; changes nothing
    tools/synos bundle apply <zip|dir>       copy into this checkout, run the manifest validator
    tools/synos build <zip|dir|manifest>     apply, then build in a container
    tools/synos build x.zip --pull           use the builder image published by CI
    tools/synos --json ...                   one JSON object on stdout, for scripts and CI

Exit codes: 0 success, 1 the bundle or manifest is invalid, 2 the host
cannot build, 3 the build failed. `apply` never overwrites a file that
already exists with different content unless `--force` is given, so a
bundle cannot silently replace a checked-in profile.

## Versioning

- The **bundle format** changes only when the layout or the meaning of
  `bundle.json` changes. The engine lists the formats it accepts
  (`SUPPORTED_FORMATS` in `tools/synos`, `bundle_format` in the catalog).
- The **engine version** (`VERSION`) changes whenever the catalog a front
  end embeds changes shape or content that affects generated bundles: a new
  region, profile field, schema rule. Front ends write it into
  `engine.min`.
- Manifests and profiles keep their own schemas (`schema/manifest.schema.json`,
  `schema/profile.schema.json`); a bundle is validated against them before
  anything is written.

## Producing a bundle without the web page

Any tool can write one. The catalog (`tools/export_catalog.py`) lists the
ids and schemas to generate against. A minimal bundle is a folder with
`bundle.json` and a manifest; `tools/synos bundle validate` tells what is
missing.

## The bundle catalog

A catalogued bundle is an ordinary bundle (`bundle.json`, its manifest, and
almost always its own `profiles/<id>.yml`) kept in this repository as a
ready-made *appliance*: a machine that boots to something that already
works — a web server serving a page, a registry that already requires a
login, a build host with the packages a real build needs — not a bare copy
of an existing machine kind with nothing configured. A front end offers
these next to "start from scratch", the same as reopening any other
downloaded bundle; nothing else about the bundle format changes. This is a
third, distinct sense of "bundle" from the package groups named `bundles` in
`profiles/bundles.yml` (office, containers, build-tools...) and from the
downloadable configuration bundle itself; context tells them apart, but the
word is worth being careful with.

Layout: `bundle-catalog/<folder>/` holds one bundle per folder, and
`bundle-catalog/index.yml` lists them under a `bundle_catalog` key, each
entry naming its `id`, `name`, `folder`, lowercase `tags` for search,
`services` (the `software.services` names the profile runs, `[]` for an
appliance with none), `ports` (the bare port numbers `security.open_ports`
opens by default, as integers), and optionally `contributed_by` (a name or
handle) and `verified` (whether a maintainer checked the package names and
image tags below actually resolve; every entry shipped with the engine is
`true`).

Two fields carry the prose, kept deliberately separate so a catalogue card
is not a wall of text:

- `summary`: one sentence, no shell commands, no "first boot" clause —
  what the machine is and what it runs, the way a catalogue entry reads.
  A front end shows this on the card.
- `first_boot`: the steps a person does first, as a list of short strings,
  one step each (a command may appear inside a step, but keep each step
  to a line or two). `[]`, not invented filler, when there is no
  meaningful first step — the AI workstation and both Yocto entries are
  `[]` because there is nothing to do before using them. A front end
  shows this after the bundle is chosen, not on the card.

`tools/export_catalog.py` reads every listed folder and exports a
`bundle_catalog` array of
`{id, name, summary, first_boot, tags, kind, files, services, ports,
verified, contributed_by?}`, where `kind` is the machine-kind (profile) id
the bundle's manifest names and `files` maps every path the folder
contains (`bundle.json`, `manifests/<id>.yml`, `profiles/<id>.yml`) to
that file's exact text, read as bytes and decoded like `launchers()`
above, so a front end can write the bundle out unchanged.

`verified: true` means a maintainer checked the package names and image
tags resolve; it does not by itself mean every appliance was booted and
exercised over HTTP. Most of this round's appliances were: nginx, Apache,
Caddy, the registry and PostgreSQL were built, run and hit with a real
request (the registry and Postgres tests below are effectively that
verification, executable). Gitea and Grafana were verified by
configuration and logs only — the shipped config paths and settings were
confirmed correct from the images' own startup scripts and log output
(Gitea's `ConfigFile` line and `INSTALL_LOCK` behavior; Grafana's
provisioning YAML is standard and low-risk) — but neither was confirmed to
answer a live HTTP request in the environment this was built in, which hit
very slow first-run database migrations unrelated to the shipped
configuration. Say which kind of verification an entry got when proposing
one.

The self-hosting round (home automation, ad/tracker blocking, personal
cloud, password manager, container management, status monitoring, file
sync, object storage, automation flows, VPN server) was built, run and hit
with a real request for every entry except two things this environment
could not exercise: a full WireGuard handshake between two real peers
(the server side — key generation, the wg0 interface, and the UDP
listener — was confirmed running) and Vaultwarden's registration
endpoint under `SIGNUPS_ALLOWED=false` (the setting is real and
documented upstream; the exact route to hit by hand was not found in the
time available). Two schema gaps came out of this round and were fixed
rather than worked around: `software.services[].ports` now accepts an
optional `/tcp` or `/udp` suffix (WireGuard and Pi-hole both need a
UDP-bound port), and services gained `cap_add` for the rare container
that has to manage its own network interface (`NET_ADMIN`, for
WireGuard) — both checked against this engine's actual container
runtime, not assumed from the images' documentation. MinIO's official
image is no longer published to Docker Hub (moved to `quay.io/minio/minio`
partway through 2024); its tag was verified against quay.io's own
registry API instead, and `MINIO_ROOT_USER_FILE`/`MINIO_ROOT_PASSWORD_FILE`
were tried and found not to work in the pinned release, which is why that
entry ships neither variable and instead keeps its ports closed.

An appliance's profile carries its own configuration through
`software.files` (see "Configuration files a profile ships" in
`docs/ARCHITECTURE.md`): real files written into the image, never a
generator invented for the occasion. A shipped credential is always an
obvious, documented placeholder the person is told to change — never a
working password left in place, and never a secret that would matter if
read by anyone with this public repository.

### Proposing a catalogued bundle

1. Copy `bundle-catalog/_template/` to `bundle-catalog/<your-id>/` and
   rename `template-appliance` to your id everywhere: the folder, the `name`
   and `manifest` fields in `bundle.json`, the manifest file's name, and
   `profiles/<your-id>.yml` (its file name and its `id:`) if you ship one.
   Extend the machine kind that fits — `server` for most appliances,
   `kiosk` for a locked-down single application — rather than inventing a
   new archetype; do not invent new profile keys.
2. Write the appliance: `software.services` for a container (the
   `container_services` role, `docs/ARCHITECTURE.md` "GPU and container
   services"), `software.packages` and `software.repositories` for native
   packages and third-party apt sources (verify every package name and
   repository resolves for the suites this engine supports — the same way
   `bases/*/packages.map`'s `yocto-host-tools` entries were checked, one
   base and suite at a time), and `software.files` for the configuration
   that makes it boot to something useful. Pin container image tags to a
   real released version, never `latest`. List every port the appliance
   needs in `security.open_ports` (it replaces the parent profile's list
   entirely, so re-list `22/tcp` too if you extend `server`).
3. Add one entry to `bundle-catalog/index.yml`: a one-sentence `summary`
   with no commands and no "first boot" clause, and the steps a person
   does first as a list of short strings in `first_boot` (`[]` if there
   genuinely is none). See `bundle-catalog/_template/README.md` for the
   exact shape.
4. Run `PYTHONPATH=tests python3 -m unittest discover -s tests/unit -p
   'test_*.py'`. `tests/unit/test_bundle_catalog.py` checks, automatically,
   for every entry: `bundle.json` and the manifest and profile schemas
   validate; the manifest renders on its declared base and suite; every
   `extends` and every package group it names exists and resolves to a
   non-empty package list on both bases; every `software.files` path stays
   inside the allowed directories, contains no `..`, and fits the size
   limits (`tools/render_manifest.py`'s `validate_profile_files`); `summary`
   is a plain sentence under a sensible length with no shell command in it
   and `first_boot` is a list of strings; and the folder and the index
   agree, with `bundle-catalog/_template/` exempt from that last check on
   both sides.
5. Open a pull request. A reviewer additionally checks, by eye, what the
   automated tests cannot: that no shipped file contains a real secret,
   that image tags and package names were actually verified rather than
   guessed (say how, in the description), that `first_boot` honestly
   covers everything a person needs to do before trusting the appliance
   with anything — nothing quietly left only in the profile's own
   `description` — and that any capability a service requests through
   `cap_add` is one the appliance actually needs (the schema only allows
   choosing among a fixed, reviewed set; widening that set is itself a
   reviewed change to `schema/profile.schema.json`, not something a
   single bundle can do on its own).
