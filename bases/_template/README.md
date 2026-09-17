# Adding a base

Copy this folder to `bases/<id>/` and fill in:

- `base.env`: bootstrap tool, suites, mirrors, components, keyring, kernel, signed bootloader packages
- `sources.tmpl`: package source template rendered with the variables from base.env
- `packages.map`: abstract name to concrete package names
- `bootstrap.sh` (future): only when BOOTSTRAP_TOOL is not debootstrap
- `Containerfile` (step 7): build container for this base

Nothing else in the engine needs to change.

Package map rules: `name = pkg pkg…`; an empty right-hand side means the
base provides it elsewhere; `${LANG}` expands once per language code and
`${ARCH}` to the target architecture. Names missing from the map pass
through as concrete package names, and the renderer reports them.
