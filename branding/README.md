# Brand kits

One folder per brand. Only `brand.yml` and `logo.svg` are required; every
other asset falls back to a neutral default.

```text
branding/<id>/
  brand.yml        id, display name, vendor, URLs, colours, hostname prefix
  logo.svg         master logo; all icon sizes, GRUB, Plymouth and GDM are derived
  logo-mono.svg    optional monochrome variant
  wallpaper.png    optional desktop and lock screen
  installer/       optional slideshow slides and welcome text
  legal/           optional EULA and privacy notice
  browser/         optional homepage, bookmarks, enterprise policies
```

Rules enforced before a build: `id` is lowercase `[a-z0-9-]` and at most
32 characters (ISO volume label); the display name contains no `"`, `\` or
`$` (GRUB); the SVG has no external references.

## Rendering

`make brand` (or `python3 tools/render_brand.py --manifest …`) builds
`.build/branding/<id>/<id>-branding_<version>_all.deb`. The package owns
`/usr/lib/os-release`, the icons, the Plymouth theme, the login logo, the
wallpaper and the dconf defaults; `policy.wallpaper: locked` adds dconf
locks so users cannot change it. The same package can be installed on an
already deployed machine by the `desktop_branding` Ansible role.

`make newbrand ID=acme NAME="Acme Workstation" LOGO=logo.svg` scaffolds a kit.

## The distribution name follows the brand

`display_name` is the name of the distribution, not only a logo caption. Two
names exist in a build:

- `synos` is the internal namespace and never changes: package names
  (`synos-installer-beta`), paths (`/usr/share/synos`), dconf and D-Bus keys
  (`com.synos.*`), kernel options (`rd.synos.live`), the ISO volume label and
  the filesystem labels the installer writes (`synos`, `synos-swap`). Like
  `ubuntu` inside an Ubuntu derivative, it is what scripts and tooling see.
- `SynOS` is only the displayed product name. When the packages are built
  (`tools/build_packages.py`), every whole-word "SynOS" in a text file or a
  compiled translation of a package becomes the brand's `display_name`: window
  titles, `.desktop` entries, the installer and its translations, package
  descriptions, GRUB fallback titles. A brand named "Acme Workstation" gives
  "Acme Workstation Installer", "Acme Workstation-Installer" in German, and
  "Acme Workstation" in the About page, the login screen and the boot menu
  (those come from the brand kit package itself).

The word boundary keeps identifiers such as `SynOSBtrfsSnapshotsManager`
intact. Literals that must keep the product name are listed in
`packages/_lib/brand-keep.txt` (an HTTP User-Agent token, for instance).
Bitmaps are never patched: the wordmarks the brand kit renders from
`logo.svg` replace them. Because the name is part of the package fingerprint,
changing a brand rebuilds the packages that mention it.
