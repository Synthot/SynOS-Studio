# SynOS

SynOS builds Linux workstations with your name on them. You describe the
machine you want, a small configuration bundle comes out, and this engine
turns that bundle into an installable image on your own computer. It exists
for organisations and people moving away from Windows and macOS who want a
desktop that is familiar, maintained, and theirs: their logo, their
languages, their security baseline, their applications.

This repository is the build engine. It is free software under the GPL-3.0.
The web page that generates bundles ("SynOS Studio") is a separate,
proprietary front end; nothing here depends on it, and everything it
produces is plain YAML you can write by hand.

## What you get

An ISO that boots to a live GNOME desktop with a taskbar and a start menu,
and installs from there. Inside:

- **Your brand everywhere.** The name you choose becomes the name of the
  system: boot menu, login screen, installer, About page, application
  titles. The logo becomes the icons, the boot splash and the wallpaper.
- **A base you trust.** Ubuntu 26.04 or Debian 13, with their security
  updates, without snaps. The same profile builds on either.
- **Regions, not just languages.** A country adds its languages, keyboard,
  time zone, paper size, compliance baseline (CIS, ANSSI, BSI, BIO, NCSC,
  ITSG-33, NIST) and national eID support. Sixty-two countries are defined today; nine of them have been exercised in QEMU.
- **Machine roles.** Office workstation, developer, AI workstation (GPU
  stack and inference services), thin client, kiosk, server, minimal; a
  company derives its own profile from one of them.
- **Workplace features.** Disk encryption, directory login (Active
  Directory, FreeIPA, LDAP, Entra ID), USB policy, firewall, unattended
  installs, first-boot fleet enrollment with Ansible.
- **Evidence for every build.** Next to the ISO: its SHA-256, the exact list
  of packages, a CycloneDX SBOM and the resolved configuration. Builds can
  be pinned to an archive snapshot so the same bundle gives the same image.

## Build one

You need a Linux machine (or Windows with WSL 2, or a Mac), podman or
docker, 40 GB of free disk and a network connection. Nothing else is
installed on your machine: the engine runs inside a published container
image made for the exact engine version of the bundle.

**From a bundle** (what most people do): unpack it (zip on Windows and macOS, tar.gz on Linux, nothing to install) and run the launcher
inside, `./build.sh` on Linux, `build.cmd` on Windows, `build.command` on
macOS. It offers to install podman if you have no runtime, pulls the published
builder image or builds it locally from this repository's source when none
can be pulled, builds the image,
and tells you how to write it to a USB stick or boot it in a virtual
machine. The ISO has to be written whole, byte for byte — balenaEtcher,
Fedora Media Writer, GNOME Disks' "Restore Disk Image", or `dd`. A tool that
copies the ISO's *files* onto the stick instead, as Ubuntu's Startup Disk
Creator, Rufus in ISO mode, UNetbootin and Ventoy do, makes a stick that
starts and then fails with squashfs read errors; the boot menu's "Check
installation media for defects" entry says so in one minute. A build takes about an hour on a fast
machine and longer on a laptop: installing the packages is most of it, so the
cache a later build reuses saves their download (a few minutes) rather than
the hour. Each build writes its own phase times to
`dist/<name>.timings.json` and prints them, longest first, when it finishes.
`./build.sh check` only verifies your machine. The complete output is kept
in `dist/build.log`.

Where the build's bytes land needs nothing from you: on podman, the
launcher keeps images, layers and the chroot beside the bundle, on the disk
you already unpacked it to, instead of the container runtime's own default
(usually the system disk). `--storage /another/disk` (or `--storage=/path`)
points it elsewhere instead, no root, no daemon restart; `SYNOS_CONTAINER_ROOT`
does the same for unattended use. Docker's storage is one setting for the
whole daemon and has no per-build override, so the launcher says how to move
it instead of guessing (`docs/BUNDLE.md`, "Where the build's bytes land").

**From this checkout** (developers, Ansible, CI):

```bash
tools/synos check                         # can this machine build?
tools/synos bundle validate my-bundle.zip # check a bundle, change nothing
tools/synos build my-bundle.zip --pull    # apply it and build in the published builder image
make container-build MANIFEST=manifests/my.yml   # build a manifest you wrote yourself
```

Every command takes `--json` and uses exit codes 1 (invalid bundle), 2 (the
host cannot build) and 3 (the build failed). `docs/BUNDLE.md` describes the
bundle format; `schema/` holds the JSON schemas for manifests, profiles and
bundles; `ci/` and `.github/workflows/` are pipeline templates you can copy.

## What is verified, and what is not

Honest as of engine 0.4.0 (September 2026):

- Ubuntu 26.04 images generated by the Studio page, built by their own
  launcher, boot to the live desktop and the installer in QEMU with UEFI;
  brand, boot menu theme, default language and installer language order
  were checked on screen. Debian 13 images build and boot; they have been
  exercised less.
- 1478 unit tests (`PYTHONPATH=tests python3 -m unittest discover -s
  tests/unit -p 'test_*.py'`, the same command CI runs — count it yourself
  rather than trust this line, since it will drift again) run on every push
  and pass on a bare Ubuntu 24.04 machine. They cover the manifest
  renderer, the package builder, the brand kit, the bundle tool, the
  launchers, the catalog, the build matrix and the conformance runner.
  The easier way to run that command, plus the other checks a push wants
  (manifest validation, a shell syntax pass, `compileall`, an offline
  bundle-catalog validation loop), in one sequential pass instead of by
  hand: `python3 tools/run_checks.py` (`--level fast` for the seconds-long
  checks only, `--list` to see every stage, `--only NAME` for one of them).
  Each stage's full output lands under `test-results/checks/`; the tool
  never builds an image, boots QEMU or touches the network. It can also
  drive the separate front end's own suites when pointed at that checkout
  (`--studio-repo PATH` or `SYNOS_STUDIO_REPO`) — never required here.
- One catalogued appliance (the Nginx web server, Ubuntu 24.04) has been
  built end to end from a bundle the Studio page produced, booted in QEMU,
  and certified on screen: the live session came up without a password and
  the installer read "SynOS NGINX Installer". The other 49 catalogued
  bundles are validated, their packages resolve and their images run, but
  they have not each been built yet; the build status the page shows is
  written by the runner, and an entry with no record claims nothing.
- What the catalog is checked against, in four layers, each proving what the
  one before it cannot (`docs/BUILD_MATRIX.md`): every application name the
  front end offers exists in the archive it claims, on every base and suite
  (2207 names, about fifteen seconds); apt's own solver accepts each of them
  on top of what an image already installs (`apt-get --simulate` in a
  container of that suite, about six hours for the whole catalog, so it is a
  nightly job); the engine's own application groups and machine profiles
  build into one real image per base; and a single catalogued appliance's own
  configuration survives the front end and its launcher end to end. The first
  two found and fixed 50 offered applications that did not exist and a
  generator that had been re-asserting them on every run.
- Language packages are resolved from the archive, not from a naming
  convention (`bases/*/language-packages.map`, generated by
  `tools/generate_language_packages.py`). Before 0.4.0 a German image shipped
  no German spellcheck: `hunspell-de` is not a package, `hunspell-de-de` is,
  Finnish uses Voikko rather than hunspell, and English has no office
  localisation package at all. 67 such names were wrong; the build now says
  out loud when a language genuinely has nothing available.
- An appliance's own services no longer start in the live session
  (`ConditionKernelCommandLine=!rd.synos.live`), because that session is
  passwordless by design and a live boot should not stand up a web server, a
  container registry or a kubelet on someone's network. They stay enabled and
  start normally on the installed system.
- **One image has now been built, booted, installed and started from its own
  disk, end to end** (27 September 2026): a Debian 13 image described on the
  Studio page, built on a workstation by the bundle's own launcher in 55
  minutes, booted in a UEFI virtual machine, installed to an NVMe disk by the
  installer in the live session, and booted from that disk afterwards. That is
  the first complete run in this project's history, and it is one machine, one
  base, one profile, in a virtual machine rather than on bare metal — every
  other line in this section still stands.
- Debian images could not be installed before 0.4.0: the installer planned
  `grub-install --no-extra-removable`, which Ubuntu's own packaging patch
  provides and Debian's GRUB does not, and its preflight correctly refused to
  run it. The option is now chosen by probing the target's own `grub-install`.
  Two more defects shipped in the same image and stopped the desktop coming up:
  `auditd` failed on every boot because the build wiped `/var/log` and dpkg
  never recreates a directory a maintainer script made once, which also cost
  every appliance its own log directory. All three were found by a person
  installing, not by a test.
- The QEMU acceptance suite (`make test`: installs, regions, desktop
  behaviour) exists and has not yet been run end to end by this project.
  That is why the Debian install defect above shipped: nothing here has ever
  run the installer on a built image.
- Not tested on real GPU hardware (AI workstation), not tested on macOS,
  ARM images depend on ARM runners. NVIDIA on Debian needs Secure Boot
  handling. Desktop acceptance in French, German and Dutch is not automated.

`docs/BACKLOG.md` lists what is agreed and not built, in order.

## Privacy and security

- The engine sends nothing anywhere. A build downloads packages from the
  base's archives and from repositories you named; that is all.
- Images update from a repository you control. The engine signs its package
  repository; for development it generates a key on first use, and CI
  injects a real one (`SYNOS_SIGNING_KEY`). Bundles carry only the public
  keys of third-party repositories they add.
- Bundles are data. A bundle cannot carry build scripts, packages or
  anything outside its manifests, profiles, brand kit, keys and answer
  files; paths outside the bundle are rejected. Applying a bundle to a
  checkout never overwrites an existing file with different content unless
  you say so.
- Reporting a vulnerability: see `SECURITY.md`.

## Layout

```text
manifest.yml     what to build: base, profile, regions, brand, version
manifests/       more of them
schema/          JSON schemas: manifest, profile, bundle
bases/           per-distribution adapters (bootstrap, sources, package map, builder container)
profiles/        machine roles, bundles of applications, the application catalog
regions/         per-country data (languages, keyboard, timezone, compliance, eID)
branding/        brand kits (brand.yml + logo.svg + optional assets)
packages/        the SynOS desktop packages, built from recipes into a signed repository
mods/            build steps executed inside the chroot
ansible/         collection: build, customise the chroot, first-boot roles
tools/           synos (the build tool), the renderers, the catalog and brand generators
tests/           unit tests and the QEMU acceptance framework
docs/            architecture, bundle format, backlog
```

## Licence

Everything in this repository is free software under the GPL-3.0
(`LICENSE`). Parts of the code base incorporate work from other GPL
projects; their copyright notices are preserved in the source files, as the
licence requires. The packages inside an image keep their own licences,
listed in the SBOM and in `OSS.md`. SynOS is not affiliated with Ubuntu,
Debian or GNOME.

## Contributing and support

Issues and pull requests are welcome on this repository. Say which base,
suite and profile you built and attach `dist/build.log` for build problems.
Keep bundles you share free of internal repository keys and hostnames.
