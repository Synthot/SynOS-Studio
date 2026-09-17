#!/usr/bin/env python3
"""Scaffold a brand kit: python3 tools/new_brand.py --id acme --name "Acme Workstation" [--logo path.svg] [--accent '#B5642B'] [--lock-wallpaper]"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOGO = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="64" height="64">
  <rect width="64" height="64" rx="14" fill="{accent}"/>
  <text x="32" y="43" text-anchor="middle" font-family="sans-serif" font-size="32" font-weight="700" fill="#FFFFFF">{initial}</text>
</svg>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id", required=True, help="lowercase [a-z0-9-], max 32 chars")
    parser.add_argument("--name", required=True, help="display name shown in GRUB, login and About")
    parser.add_argument("--vendor", default=None)
    parser.add_argument("--logo", default=None, help="SVG to copy as logo.svg; a placeholder is generated otherwise")
    parser.add_argument("--accent", default="#0E6B7A")
    parser.add_argument("--support-url", default="")
    parser.add_argument("--lock-wallpaper", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9-]{1,32}", args.id):
        sys.exit("brand id must be lowercase letters, digits or dashes, 1 to 32 characters")
    if re.search(r'["\\$]', args.name):
        sys.exit("display name cannot contain quotes, backslashes or dollar signs")
    if not re.fullmatch(r"#[0-9A-Fa-f]{6}", args.accent):
        sys.exit("accent must be a #RRGGBB colour")
    kit = ROOT / "branding" / args.id
    if kit.exists():
        sys.exit(f"{kit.relative_to(ROOT)} already exists")
    kit.mkdir(parents=True)
    if args.logo:
        shutil.copy(args.logo, kit / "logo.svg")
    else:
        (kit / "logo.svg").write_text(DEFAULT_LOGO.format(accent=args.accent, initial=args.name[:1].upper()), encoding="utf-8")
    (kit / "brand.yml").write_text(f"""id: {args.id}
display_name: {args.name}
vendor: {args.vendor or args.name}
tagline: ""
hostname_prefix: {args.id}
urls:
  home: ""
  support: "{args.support_url}"
  bug_report: ""
  privacy: ""
colors:
  accent: "{args.accent}"
  ground: "#12181C"
  ink: "#E6ECEA"
assets:
  logo: logo.svg
  logo_mono: logo.svg
  wallpaper: derived        # derived | none | <file.png in this folder>
policy:
  wallpaper: {"locked" if args.lock_wallpaper else "free"}   # free | locked
""", encoding="utf-8")
    print(f"created {kit.relative_to(ROOT)}/ — set brand: {args.id} in your manifest, then run: make brand MANIFEST=...")
    return 0


if __name__ == "__main__":
    sys.exit(main())
