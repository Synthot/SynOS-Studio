# Adding a base

Copy this folder to `bases/<id>/` and fill in:

- `base.env`: bootstrap tool, suites, mirrors, components, keyring, kernel, signed bootloader packages
- `sources.tmpl`: package source template rendered with the variables from base.env
- `packages.map`: abstract name to concrete package names
- `live.map` (optional): which of base.env's SUPPORTED_SUITES cannot back a Live image, and why
- `bootstrap.sh` (future): only when BOOTSTRAP_TOOL is not debootstrap
- `Containerfile` (step 7): build container for this base

Nothing else in the engine needs to change.

Package map rules: `name = pkg pkg…`; an empty right-hand side means the
base provides it elsewhere; `${LANG}` expands once per language code and
`${ARCH}` to the target architecture. Names missing from the map pass
through as concrete package names, and the renderer reports them.

Live map rules: `suite = unavailable: <reason>`, one line per suite of
SUPPORTED_SUITES whose own archive cannot back a Live image — checked
against the real archive index, never assumed (see bases/ubuntu/live.map's
jammy entry). A suite this base's Live image builds fine on needs no line;
a base where every suite is fine needs no live.map file at all.
tools/render_manifest.py refuses, at render/--check time, a manifest naming
a suite listed here, with the reason; mods/stack.sh's own
ensure_dracut_live_modules() check inside the chroot is unaffected and
stays the backstop for a suite whose archive changes later.
