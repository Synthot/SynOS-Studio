# SynOS packages

One directory per package, built into a local APT repository by
`tools/build_packages.py` (`make packages`), and served to the build chroot
and installed from the local repository; `packages/stack.yml` names the package
that fills each role of the desktop, installer and live system.

```text
packages/<name>/
  control            Debian control fields; ${VERSION} ${BASE} ${SUITE} ${BRAND_*} substituted
  assets/            files installed as-is, rooted at /
  scripts/           postinst, prerm, postrm (optional)
  templates/         files rendered with the same substitutions before install (optional)
  conffiles          list of configuration files (optional)
```

The repository is signed. The first `make packages` on a machine generates a
development key under `keys/private/` (git-ignored) and writes its public
half to `keys/public/`, which `synos-archive-keyring` ships. Releases inject
the real key through `SYNOS_SIGNING_KEY` (armored secret, a CI secret) or
`SYNOS_SIGNING_KEY_FILE`. Keep using one key per product line: images only
trust the repository signed by the key they were built with.

Recipe kinds:

| Kind | Files in `packages/<name>/` | Built by |
|---|---|---|
| plain | `control`, `assets/`, `scripts/`, `conffiles`, `triggers` | copied |
| prebuild | plus `upstream/` (the upstream sources and scripts), `prebuild.sh`, `includes.txt`, `lib -> ../_lib` | `prebuild.sh` runs the upstream download or build commands inside `upstream/`; `includes.txt` maps its outputs into the package |
| fork | plus `fork.json` | the upstream binary package is downloaded from the archive named in `fork.json`, unpacked, patched by `prebuild.sh`, overlaid with `includes.txt` and repackaged |

A package whose prebuild needs a tool or library the host lacks is
skipped with a reason (see `.build/repo/SKIPPED`); the container build
carries the full toolchain and runs `make packages` with `--strict`, where
a skip is an error. Network is needed for forks and for prebuilds that
fetch upstream sources (Fluent themes, GNOME extensions).

Prebuilds never run inside `packages/`: the builder copies each recipe's
inputs to `.build/packages-work/src/<name>/` and runs `prebuild.sh` there, so
downloads, compiled translations and fork stages stay out of the repository
(a container build running as root leaves nothing root-owned in `packages/`).
Git sources are cached under `.build/sources`, the recipe fingerprint decides
whether the copy and the prebuild happen at all.

Every text file and compiled translation of a built package has the displayed
product name replaced by the brand's `display_name` (see
`branding/README.md`); `packages/_lib/brand-keep.txt` lists the literals that
keep it.

## Publishing

Installed systems update from the repository named in the manifest's
`packages.repository`. The layout is one flat repository per suite:
`<repository>/<suite>/`. The build refuses to seal an image whose configured
repository does not answer or is not signed by the shipped keyring.

```bash
make packages                                   # build .build/repo (creates the key if missing)
make serve-repo PORT=8080                       # test server on this machine
make publish-repo DEST=user@host:/var/www/synos # rsync to the real server
```

Any static web server can host the published directory.

Not ported on purpose: the AI assistant (`why-ai`, needs clang and a long compile; the placeholder that offers to install it ships), the development apt configuration, the server tweaks variant and the upstream base-files fork.
