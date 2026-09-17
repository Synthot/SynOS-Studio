# Security

## Reporting a vulnerability

Report privately to the maintainers through GitHub's "Report a vulnerability"
form on this repository (Security tab) rather than in a public issue. Say
which engine version (`VERSION`), which base and suite, and how to
reproduce. We aim to acknowledge within a week.

## What the engine does and does not do

- **No telemetry.** The engine and the images it builds contact nothing
  of ours. A build downloads packages from the base's archives and from
  repositories the profile names.
- **Signed repositories.** SynOS packages are installed from a repository
  signed at build time. Development builds generate a key on first use;
  releases use the key injected through `SYNOS_SIGNING_KEY`. Images trust
  the public half of the key they were built with.
- **Bundles are data.** A bundle may contain manifests, profiles, brand
  kits, public keys of third-party repositories and answer files. Build
  scripts, packages or paths outside those folders are refused, and paths
  that escape the bundle are rejected. Third-party repositories must be
  signed; their keys ship in the bundle.
- **Builds run as root in a container**, as any image assembler must
  (chroot, filesystem mounts, loop devices). The launcher explains why it
  needs a root-capable container runtime. Nothing runs as root on the host
  outside that container.
- **Secure Boot.** Images boot with the base's signed shim and GRUB.
  Proprietary kernel modules (NVIDIA) need the base's module signing
  arrangements; on Debian this may require enrolling a key or disabling
  Secure Boot, which the profile documentation states.

## Supported versions

Only the newest engine version receives fixes. Bundles record the engine
version they were generated for; rebuilding an old bundle with a newer
engine is supported when the bundle format is unchanged.
