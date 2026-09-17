# Proxy Switcher provenance

This package is an SynOS-maintained derivative of Proxy Switcher.

- Upstream repository: https://github.com/tomflannaghan/proxy-switcher
- Upstream commit used for the maintained source: `5b63ce78f81b79baf6eb9bea4ee12d2192ef966c`
- GNOME Extensions releases used to preserve the upstream translations:
  version 23 for GNOME 46 and version 27 for GNOME 49/50
- SynOS extension UUID: `proxy-switcher@synos.com`
- Upstream license: GNU General Public License, version 2; the unmodified
  upstream license text is stored in `COPYING`.
- Translation catalogs are preserved as editable `po/*.po` source files and
  compiled locally during the package build.

SynOS changes:

- maintain extension source directly in this repository;
- build without downloading executable extension code;
- use SynOS-owned UUID, gettext domain and settings namespace;
- support the SynOS GNOME 45–50 platform range from one source tree;
- show a status-area proxy icon whenever the system proxy mode is manual or
  automatic;
- add reproducible package and proxy-state checks.
