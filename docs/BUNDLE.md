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
`<base>-<suite>` (latest) and `<base>-<suite>-v<VERSION>` (per release). The
launchers every bundle ships (`tools/bundle_launcher.*`, exported in the
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
4. check 40 GB free, then pull the image pinned to the engine version, or the
   latest one for that base and suite when no pinned image exists;
5. run it privileged with the bundle mounted at `/bundle`, a named volume
   `synos-cache-<base>-<suite>` at `/opt/synos/.build` (package, apt and
   source caches, signing key), anonymous volumes for the chroot and the
   image staging (GRUB must probe a real filesystem, not the container's
   overlay), and `synos build /bundle --output /bundle/dist --log /bundle/dist/build.log`;
6. print where the ISO is and how to write it to a USB stick or boot it in a
   virtual machine; on failure, where to look in `build.log` and that running
   again resumes from the cache.

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
