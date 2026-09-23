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

### Dependencies that differ by release

A `control` field can carry a `Field[base]:` override — `Depends[debian]:`
replaces `Depends:` when building for that base, e.g. because Debian and
Ubuntu name their kernel package differently. There is no equivalent
`Field[suite]:` override: bases already span several suites each
(`bases/*/base.env`'s `SUPPORTED_SUITES`), and a suite override would mean
copying the whole field for every suite just to change the one token that
differs — easy to let drift out of sync, exactly how
`packages/synos-core-system/control` ended up depending unconditionally on
`linux-generic-hwe-26.04` and `libfuse3-4`, kernel-HWE and libfuse sonames
that exist only on the newest Ubuntu release, so installing the package on
an older supported suite (24.04, 22.04) failed outright even though it built
fine.

The fix is almost always an **alternatives list**, `name-a | name-b`, when
either package really does satisfy the dependency: apt installs the first
one that resolves, so one line covers every suite without saying which suite
is which. Order alternatives newest-first so a suite that has more than one
still gets the best match:

```
Depends: linux-generic-hwe-26.04 | linux-generic-hwe-24.04 | linux-generic-hwe-22.04 | linux-generic,
         libfuse3-4 | libfuse3-3,
         libfuse2t64 | libfuse2,
         dracut-install | dracut-core
```

(the last one is for a suite where the upstream package split
`dracut-install` out of `dracut-core` later than others — on a suite that
predates the split, the already-mandatory `dracut-core` alternative covers
it.) Reach for a `Depends[base]:` override instead only when the two bases
genuinely need *different* packages, not merely differently-named ones — the
Ubuntu HWE kernel stack has no Debian equivalent at all, so
`Depends[debian]:` names `linux-image-${ARCH}` outright rather than listing
Ubuntu's kernel names as alternatives Debian would never match.

If a dependency truly does not exist before some release — no alternative
name, no split package — say so instead of dropping it or loosening a
version constraint to make the resolver happy: `synos-whisper-worker`
depends on `libggml0 (>= 0.9.11)`, which Ubuntu ships starting with the
25.10+ archive and not before; there is no 22.04/24.04 equivalent to fall
back to, so that package is not currently installable on those suites. That
gap is tracked, not hidden, in `tests/unit/test_control_dependencies.py`'s
`KNOWN_GAPS`.

`tests/unit/test_control_dependencies.py` guards this class of bug: an
offline test rejects any dependency name with no `|` fallback that embeds a
release number, and a slower, network-dependent test downloads the real
package indices for every base's `SUPPORTED_SUITES` (all the pockets
`bases/*/sources.tmpl` enables: `SUITE`, `SUITE-updates`, `SUITE-backports`,
`SUITE-security`) and checks that every `Depends`/`Pre-Depends` alternatives
group resolves against at least one of them; it skips cleanly, rather than
failing, when the archive is unreachable.

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
