#!/usr/bin/env python3
"""Render a brand kit into a Debian branding package.

Input:  branding/<id>/brand.yml, logo.svg and the optional assets around it.
Output: .build/branding/<id>/<id>-branding_<version>_all.deb plus the loose
        assets the ISO build picks up (GRUB background).

The package owns everything the user sees that carries the brand:

  /usr/lib/os-release                   diverted from base-files (About panel, tooling)
  /usr/share/icons/hicolor/*/apps/<id>  logo at every size, plus distributor-logo
  /usr/share/plymouth/themes/<id>/      boot splash, registered as default.plymouth
  /etc/dconf/db/gdm.d/01-<id>-logo      login screen logo
  /usr/share/backgrounds/<id>/          default wallpaper (uploaded or derived)
  /etc/dconf/db/local.d/01-<id>-brand   desktop and lock screen defaults (+ locks)
  /usr/share/<id>/branding/             brand.env and source assets for other tools

    python3 tools/render_brand.py [--manifest manifest.yml] [--output-dir .build/branding]

Rasterisation uses rsvg-convert when present, ImageMagick's convert as a
fallback, and otherwise ships scalable assets only (Plymouth and GRUB need
PNGs, so the build host should have librsvg2-bin).
"""
from __future__ import annotations

import argparse
import importlib.util
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("render_manifest", ROOT / "tools" / "render_manifest.py")
render_manifest = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(render_manifest)
ManifestError = render_manifest.ManifestError

ICON_SIZES = (16, 22, 24, 32, 48, 64, 128, 256, 512)


# --------------------------------------------------------------- raster
class Rasteriser:
    def __init__(self) -> None:
        self.tool = None
        if shutil.which("rsvg-convert"):
            self.tool = "rsvg-convert"
        elif shutil.which("convert"):
            self.tool = "convert"
        self.skipped: list[str] = []

    def render(self, svg: Path, png: Path, width: int, height: int) -> bool:
        png.parent.mkdir(parents=True, exist_ok=True)
        if self.tool == "rsvg-convert":
            cmd = ["rsvg-convert", "-w", str(width), "-h", str(height), "-o", str(png), str(svg)]
        elif self.tool == "convert":
            cmd = ["convert", "-background", "none", "-density", "384", str(svg), "-resize", f"{width}x{height}", "-depth", "8", "PNG32:" + str(png)]
        else:
            self.skipped.append(png.name)
            return False
        result = subprocess.run(cmd, check=False, capture_output=True, text=True)
        if result.returncode != 0 or not png.is_file():
            self.skipped.append(png.name)
            return False
        return True


def hex_to_rgb(value: str) -> tuple[float, float, float]:
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


def hex_to_rgb_int(value: str) -> tuple[int, int, int]:
    c = value.lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)


def darken(value: str, factor: float) -> str:
    r, g, b = hex_to_rgb(value)
    return "#{:02X}{:02X}{:02X}".format(int(r * 255 * factor), int(g * 255 * factor), int(b * 255 * factor))


def mix(a: str, b: str, t: float) -> tuple[int, int, int]:
    ar, ag, ab = hex_to_rgb(a)
    br, bg, bb = hex_to_rgb(b)
    return (round((ar + (br - ar) * t) * 255), round((ag + (bg - ag) * t) * 255), round((ab + (bb - ab) * t) * 255))


def gradient_png(path: Path, width: int, height: int, stops: list[tuple[float, str]], diagonal: bool = True) -> None:
    """Write an 8-bit RGB PNG gradient with no external tool (GRUB needs 8-bit PNG)."""
    import struct
    import zlib

    span = width + height if diagonal else height
    ramp = bytearray()
    for i in range(span):
        t = i / max(span - 1, 1)
        for (t0, c0), (t1, c1) in zip(stops, stops[1:]):
            if t0 <= t <= t1:
                ramp += bytes(mix(c0, c1, (t - t0) / max(t1 - t0, 1e-9)))
                break
        else:
            ramp += bytes(mix(stops[-1][1], stops[-1][1], 0))
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter: none
        if diagonal:
            raw += ramp[y * 3:(y + width) * 3]
        else:
            raw += ramp[y * 3:y * 3 + 3] * width

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + chunk(b"IEND", b"")
    )


def rgba_png(path: Path, width: int, height: int, rgba: tuple[int, int, int, int], radius: int = 0,
             corner: str | None = None) -> None:
    """A solid RGBA PNG, optionally with one rounded corner (nw, ne, sw, se) for
    GRUB's nine-slice pixmaps. Pure Python, like gradient_png."""
    import struct
    import zlib
    r, g, b, a = rgba
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            alpha = a
            if corner and radius:
                cx = radius if corner in ("nw", "sw") else width - 1 - radius
                cy = radius if corner in ("nw", "ne") else height - 1 - radius
                outside_x = x < radius if corner in ("nw", "sw") else x > width - 1 - radius
                outside_y = y < radius if corner in ("nw", "ne") else y > height - 1 - radius
                if outside_x and outside_y and (x - cx) ** 2 + (y - cy) ** 2 > radius ** 2:
                    alpha = 0
            rows += bytes((r, g, b, alpha))
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(bytes(rows), 9)) + chunk(b"IEND", b""))


def grub_theme(bid: str, name: str, accent: str, ground: str, ink: str, fonts: dict[str, str]) -> str:
    """GRUB graphical theme (theme.txt): brand background, logo and name on top,
    a centred menu with an accent highlight, the countdown as a bar, key hints.
    `fonts` maps roles (title, item, small) to the names grub-mkfont registered;
    a missing role falls back to GRUB's default font."""
    title_font = fonts.get("title", "Unifont Regular 16")
    item_font = fonts.get("item", "Unifont Regular 16")
    small_font = fonts.get("small", "Unifont Regular 16")
    muted = "#%02x%02x%02x" % mix(ink, ground, 0.45)
    ground2 = "#%02x%02x%02x" % mix(ground, "#ffffff", 0.12)
    return f"""# {name} GRUB theme — generated by tools/render_brand.py from branding/{bid}/
desktop-image: "background.png"
desktop-image-scale-method: "stretch"
desktop-color: "{ground}"
title-text: ""
terminal-font: "{small_font}"
terminal-left: "10%"
terminal-top: "10%"
terminal-width: "80%"
terminal-height: "80%"
terminal-border: "0"

+ image {{
    id = "logo"
    left = 50%-48
    top = 14%
    width = 96
    height = 96
    file = "logo.png"
}}

+ label {{
    left = 0
    top = 14%+110
    width = 100%
    align = "center"
    text = "{name}"
    font = "{title_font}"
    color = "{ink}"
}}

+ boot_menu {{
    left = 22%
    top = 40%
    width = 56%
    height = 38%
    item_font = "{item_font}"
    item_color = "{ink}"
    selected_item_font = "{item_font}"
    selected_item_color = "#ffffff"
    selected_item_pixmap_style = "select_*.png"
    item_height = 42
    item_padding = 4
    item_spacing = 6
    item_icon_space = 0
    icon_width = 0
    icon_height = 0
    scrollbar = false
}}

+ progress_bar {{
    id = "__timeout__"
    left = 30%
    top = 84%
    width = 40%
    height = 20
    font = "{small_font}"
    text_color = "{ink}"
    fg_color = "{accent}"
    bg_color = "{ground2}"
    border_color = "{ground2}"
    text = "@TIMEOUT_NOTIFICATION_SHORT@"
}}

+ label {{
    left = 0
    top = 92%
    width = 100%
    align = "center"
    text = "Enter  boot        E  edit        C  command line"
    font = "{small_font}"
    color = "{muted}"
}}
"""


def grub_fonts(directory: Path) -> dict[str, str]:
    """Render DejaVu Sans into GRUB's font format when grub-mkfont and the font are
    present; returns the font names GRUB registers, by role."""
    mkfont = shutil.which("grub-mkfont") or shutil.which("grub2-mkfont")
    dejavu = Path("/usr/share/fonts/truetype/dejavu")
    if not mkfont or not (dejavu / "DejaVuSans.ttf").is_file():
        return {}
    roles = {"title": ("DejaVuSans-Bold.ttf", 26, "DejaVu Sans Bold 26"), "item": ("DejaVuSans.ttf", 17, "DejaVu Sans Regular 17"),
             "small": ("DejaVuSans.ttf", 13, "DejaVu Sans Regular 13")}
    out = {}
    for role, (file, size, registered) in roles.items():
        source = dejavu / file
        if not source.is_file():
            continue
        target = directory / f"dejavu-{role}-{size}.pf2"
        result = subprocess.run([mkfont, "-s", str(size), "-o", str(target), str(source)], check=False, capture_output=True, text=True)
        if result.returncode == 0 and target.is_file():
            out[role] = registered
    return out


def composite_logo(background: Path, logo_png: Path, output: Path, gravity: str, offset: str, opacity: int) -> bool:
    """Overlay the rendered logo with ImageMagick when available; otherwise keep the plain gradient."""
    output.parent.mkdir(parents=True, exist_ok=True)
    if not (shutil.which("convert") and logo_png.is_file()):
        shutil.copy(background, output)
        return False
    result = subprocess.run(
        ["convert", str(background), "(", str(logo_png), "-alpha", "set", "-channel", "A", "-evaluate", "multiply", f"{opacity / 100:.2f}", "+channel", ")",
         "-gravity", gravity, "-geometry", offset, "-composite", "-depth", "8", "PNG24:" + str(output)],
        check=False, capture_output=True, text=True)
    if result.returncode != 0:
        shutil.copy(background, output)
        return False
    return True


# ---------------------------------------------------------------- files
def write(path: Path, content: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(mode)


def sh_quote(value: str) -> str:
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`") + '"'


def os_release(brand: dict, manifest: dict, base: dict) -> str:
    bid = brand["id"]
    name = brand["display_name"]
    version = manifest["version"]
    suite = manifest["suite"]
    urls = brand.get("urls") or {}
    like = "ubuntu debian" if base["BASE_ID"] == "ubuntu" else "debian"
    lines = [
        f"PRETTY_NAME={sh_quote(f'{name} {version}')}",
        f"NAME={sh_quote(name)}",
        f"VERSION_ID={sh_quote(version)}",
        f"VERSION={sh_quote(f'{version} ({suite})')}",
        f"VERSION_CODENAME={suite}",
        f"ID={bid}",
        f"ID_LIKE={sh_quote(like)}",
        f"HOME_URL={sh_quote(urls.get('home', ''))}",
        f"SUPPORT_URL={sh_quote(urls.get('support', ''))}",
        f"BUG_REPORT_URL={sh_quote(urls.get('bug_report', ''))}",
        f"PRIVACY_POLICY_URL={sh_quote(urls.get('privacy', ''))}",
        f"LOGO={bid}",
        f"VENDOR_NAME={sh_quote(brand.get('vendor', name))}",
    ]
    if base["BASE_ID"] == "ubuntu":
        lines.append(f"UBUNTU_CODENAME={suite}")
    if base["BASE_ID"] == "debian":
        lines.append(f"DEBIAN_CODENAME={suite}")
    return "\n".join(lines) + "\n"


def plymouth_theme(bid: str, name: str, accent: str, ground: str) -> tuple[str, str]:
    theme = f"""[Plymouth Theme]
Name={name}
Description={name} boot splash
ModuleName=script

[script]
ImageDir=/usr/share/plymouth/themes/{bid}
ScriptFile=/usr/share/plymouth/themes/{bid}/{bid}.script
"""
    r, g, b = hex_to_rgb(ground)
    ar, ag, ab = hex_to_rgb(accent)
    script = f"""# {name} Plymouth theme — generated by tools/render_brand.py
Window.SetBackgroundTopColor({r:.3f}, {g:.3f}, {b:.3f});
Window.SetBackgroundBottomColor({r * 0.7:.3f}, {g * 0.7:.3f}, {b * 0.7:.3f});

logo.image = Image("watermark.png");
logo.sprite = Sprite(logo.image);
logo.sprite.SetX(Window.GetWidth() / 2 - logo.image.GetWidth() / 2);
logo.sprite.SetY(Window.GetHeight() / 2 - logo.image.GetHeight() / 2 - 40);

# Progress dots under the logo.
dots.count = 5;
dots.spacing = 22;
for (i = 0; i < dots.count; i++) {{
    dot.image = Image("dot.png");
    dot.sprite[i] = Sprite(dot.image);
    dot.sprite[i].SetX(Window.GetWidth() / 2 - (dots.count * dots.spacing) / 2 + i * dots.spacing);
    dot.sprite[i].SetY(Window.GetHeight() / 2 + logo.image.GetHeight() / 2);
    dot.sprite[i].SetOpacity(0.25);
}}
progress = 0;
fun refresh_callback () {{
    progress++;
    for (i = 0; i < dots.count; i++)
        dot.sprite[i].SetOpacity(0.25);
    dot.sprite[(progress / 8) % dots.count].SetOpacity(1);
}}
Plymouth.SetRefreshFunction(refresh_callback);

# Password prompt for encrypted disks.
fun display_password_callback (prompt, bullets) {{
    prompt.image = Image.Text(prompt, {ar:.2f}, {ag:.2f}, {ab:.2f});
    prompt.sprite = Sprite(prompt.image);
    prompt.sprite.SetPosition(Window.GetWidth() / 2 - prompt.image.GetWidth() / 2, Window.GetHeight() / 2 + logo.image.GetHeight() / 2 + 60, 1);
    bullet.text = "";
    for (i = 0; i < bullets; i++) bullet.text += "•";
    bullet.image = Image.Text(bullet.text, 1, 1, 1);
    bullet.sprite = Sprite(bullet.image);
    bullet.sprite.SetPosition(Window.GetWidth() / 2 - bullet.image.GetWidth() / 2, Window.GetHeight() / 2 + logo.image.GetHeight() / 2 + 90, 1);
}}
Plymouth.SetDisplayPasswordFunction(display_password_callback);
fun display_normal_callback () {{ prompt.sprite = null; bullet.sprite = null; }}
Plymouth.SetDisplayNormalFunction(display_normal_callback);
"""
    return theme, script


def wordmark_svg(logo_svg: str, name: str, text_color: str) -> str:
    """Logo plus name on one line; Ubuntu's Settings About page draws this file
    instead of the os-release icon (ubuntu-logo-text.svg and its dark variant)."""
    import re as _re
    inner = logo_svg.split(">", 1)[1].rsplit("</svg>", 1)[0] if "<svg" in logo_svg else ""
    match = _re.search(r'viewBox="([^"]+)"', logo_svg)
    view = match.group(1) if match else "0 0 64 64"
    width = wordmark_width(name)
    # textLength caps the text at the space reserved for it: a long or wide name
    # is squeezed a little instead of overflowing the image (GDM clips overflow).
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="48" viewBox="0 0 {width} 48">
  <svg viewBox="{view}" x="0" y="4" width="40" height="40">{inner}</svg>
  <text x="52" y="33" textLength="{width - 60}" lengthAdjust="spacingAndGlyphs" font-family="Cantarell, 'Noto Sans', sans-serif" font-size="26" font-weight="700" fill="{text_color}">{name}</text>
</svg>
"""


def wordmark_width(name: str) -> int:
    """Pixel width reserved for logo plus name at 26 px bold: about 16 px per
    character, wide letters and spaces averaged, plus the logo and margins."""
    return 52 + int(len(name) * 16.5) + 12


def dot_svg(accent: str) -> str:
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 12 12" width="12" height="12"><circle cx="6" cy="6" r="5" fill="{accent}"/></svg>'


def maintainer_scripts(bid: str) -> dict[str, str]:
    postinst = f"""#!/bin/sh
set -e
if [ "$1" = "configure" ]; then
    # Own /usr/lib/os-release: divert the base-files copy and install ours.
    if ! dpkg-divert --list /usr/lib/os-release | grep -q '{bid}-branding'; then
        dpkg-divert --package {bid}-branding --divert /usr/lib/os-release.base --rename --add /usr/lib/os-release
    fi
    install -m 0644 /usr/share/{bid}/branding/os-release /usr/lib/os-release
    if [ ! -L /etc/os-release ]; then
        ln -sf ../usr/lib/os-release /etc/os-release
    fi
    # GNOME Settings is compiled with a fixed distributor logo path on both
    # bases (Ubuntu: pixmaps/ubuntu-logo-text*.svg; Debian: desktop-base's
    # emblem-vendor.svg) instead of the os-release icon: own those files the
    # same way when the base ships them.
    for target in /usr/share/pixmaps/ubuntu-logo-text.svg /usr/share/pixmaps/ubuntu-logo-text-dark.svg \
                  /usr/share/icons/vendor/scalable/emblems/emblem-vendor.svg; do
        if [ -e "$target" ] || [ -e "$target.base" ]; then
            if ! dpkg-divert --list "$target" | grep -q '{bid}-branding'; then
                dpkg-divert --package {bid}-branding --divert "$target.base" --rename --add "$target"
            fi
            case "$target" in *-dark.svg) src=logo-text-dark.svg ;; *emblem-vendor.svg) src=logo.svg ;; *) src=logo-text.svg ;; esac
            install -m 0644 "/usr/share/{bid}/branding/$src" "$target"
        fi
    done
    # Boot splash.
    if command -v update-alternatives >/dev/null 2>&1 && [ -d /usr/share/plymouth/themes ]; then
        update-alternatives --install /usr/share/plymouth/themes/default.plymouth default.plymouth \\
            /usr/share/plymouth/themes/{bid}/{bid}.plymouth 200
        update-alternatives --set default.plymouth /usr/share/plymouth/themes/{bid}/{bid}.plymouth
    fi
    # dconf profile: the product's profile (synos-dconf-runtime) already lists
    # system-db:local. On a foreign system the file is only amended when it
    # exists and is not another package's conffile; never created here, so a
    # package shipping it later does not hit a conffile prompt.
    if [ -f /etc/dconf/profile/user ] && ! grep -q '^system-db:local$' /etc/dconf/profile/user \
        && ! dpkg-query -S /etc/dconf/profile/user >/dev/null 2>&1; then
        printf 'system-db:local\\n' >> /etc/dconf/profile/user
    fi
    if command -v dconf >/dev/null 2>&1; then dconf update || true; fi
    if command -v gtk-update-icon-cache >/dev/null 2>&1; then
        gtk-update-icon-cache -f -q /usr/share/icons/hicolor || true
    fi
fi
"""
    prerm = f"""#!/bin/sh
set -e
if [ "$1" = "remove" ] && command -v update-alternatives >/dev/null 2>&1; then
    update-alternatives --remove default.plymouth /usr/share/plymouth/themes/{bid}/{bid}.plymouth || true
fi
"""
    postrm = f"""#!/bin/sh
set -e
if [ "$1" = "remove" ] || [ "$1" = "purge" ]; then
    rm -f /usr/lib/os-release
    dpkg-divert --package {bid}-branding --rename --remove /usr/lib/os-release || true
    for target in /usr/share/pixmaps/ubuntu-logo-text.svg /usr/share/pixmaps/ubuntu-logo-text-dark.svg \
                  /usr/share/icons/vendor/scalable/emblems/emblem-vendor.svg; do
        rm -f "$target"
        dpkg-divert --package {bid}-branding --rename --remove "$target" || true
    done
    if command -v dconf >/dev/null 2>&1; then dconf update || true; fi
fi
"""
    return {"postinst": postinst, "prerm": prerm, "postrm": postrm}


# ---------------------------------------------------------------- build
def build(manifest_path: Path, output_dir: Path) -> Path:
    manifest = render_manifest.load_yaml(manifest_path)
    render_manifest.validate_schema(manifest, "manifest.schema.json")
    base = render_manifest.load_env(ROOT / "bases" / manifest["base"] / "base.env")
    brand = render_manifest.resolve_brand(manifest["brand"])
    bid = brand["id"]
    name = brand["display_name"]
    colors = brand.get("colors") or {}
    accent = colors.get("accent", "#0E6B7A")
    ground = colors.get("ground", "#12181C")
    ink = colors.get("ink", "#E6ECEA")
    policy = brand.get("policy") or {}
    kit = ROOT / "branding" / bid
    assets = brand.get("assets") or {}
    logo_src = kit / assets.get("logo", "logo.svg")
    logo_mono_src = kit / assets.get("logo_mono", assets.get("logo", "logo.svg"))
    version = manifest["version"]

    work = output_dir / bid
    if work.exists():
        shutil.rmtree(work)
    pkg = work / "pkg"
    raster = Rasteriser()
    share = pkg / "usr/share" / bid / "branding"

    # Source assets and brand.env for other tools (installer, welcome app, Ansible).
    shutil.copy(logo_src, share / "logo.svg") if share.mkdir(parents=True, exist_ok=True) is None else None
    shutil.copy(logo_mono_src, share / "logo-mono.svg")
    write(share / "brand.env", "".join(
        f"{k}={sh_quote(v)}\n" for k, v in {
            "BRAND_ID": bid, "BRAND_NAME": name, "BRAND_VENDOR": brand.get("vendor", name),
            "BRAND_TAGLINE": brand.get("tagline", ""), "BRAND_ACCENT": accent, "BRAND_GROUND": ground,
            "BRAND_URL_HOME": (brand.get("urls") or {}).get("home", ""),
            "BRAND_URL_SUPPORT": (brand.get("urls") or {}).get("support", ""),
        }.items()))
    write(share / "os-release", os_release(brand, manifest, base))

    # Icons: hicolor at every size, plus distributor-logo for the About panel.
    icons = pkg / "usr/share/icons/hicolor"
    for icon_name in (bid, "distributor-logo"):
        (icons / "scalable/apps").mkdir(parents=True, exist_ok=True)
        shutil.copy(logo_src, icons / "scalable/apps" / f"{icon_name}.svg")
        for size in ICON_SIZES:
            raster.render(logo_src, icons / f"{size}x{size}/apps/{icon_name}.png", size, size)
    raster.render(logo_src, pkg / "usr/share/pixmaps" / f"{bid}.png", 256, 256)

    # Wordmarks for Ubuntu's Settings About page (diverted over base-files' files).
    logo_text = logo_src.read_text(encoding="utf-8")
    write(share / "logo-text.svg", wordmark_svg(logo_text, name, "#1B2430"))
    write(share / "logo-text-dark.svg", wordmark_svg(logo_text, name, "#E6ECEA"))
    mark_width = wordmark_width(name)
    raster.render(share / "logo-text.svg", share / "logo-text.png", mark_width * 2, 96)
    raster.render(share / "logo-text-dark.svg", share / "logo-text-dark.png", mark_width * 2, 96)
    # GDM draws the login logo at its pixel size and clips what does not fit:
    # a 1x rendering, at most 320 px wide, 48 px tall.
    login_scale = min(1.0, 320 / mark_width)
    raster.render(share / "logo-text-dark.svg", share / "login-logo.png", int(mark_width * login_scale), int(48 * login_scale))

    # Login screen logo and dconf.
    raster.render(logo_src, share / "logo-128.png", 128, 128)
    # The login screen shows the wordmark (name and logo); GDM draws it on a dark ground.
    write(pkg / "etc/dconf/db/gdm.d" / f"01-{bid}-logo", f"[org/gnome/login-screen]\nlogo='/usr/share/{bid}/branding/login-logo.png'\n")

    # Plymouth.
    theme_dir = pkg / "usr/share/plymouth/themes" / bid
    theme, script = plymouth_theme(bid, name, accent, ground)
    write(theme_dir / f"{bid}.plymouth", theme)
    write(theme_dir / f"{bid}.script", script)
    raster.render(logo_mono_src, theme_dir / "watermark.png", 160, 160)
    with tempfile.TemporaryDirectory() as tmp:
        dot = Path(tmp) / "dot.svg"
        dot.write_text(dot_svg(accent), encoding="utf-8")
        raster.render(dot, theme_dir / "dot.png", 12, 12)

    # Wallpaper: uploaded, derived from the colours, or none.
    wall_setting = assets.get("wallpaper")
    backgrounds = pkg / "usr/share/backgrounds" / bid
    wallpaper_path = None
    if wall_setting and wall_setting not in ("derived", "none") and (kit / wall_setting).is_file():
        backgrounds.mkdir(parents=True, exist_ok=True)
        shutil.copy(kit / wall_setting, backgrounds / "default.png")
        wallpaper_path = f"/usr/share/backgrounds/{bid}/default.png"
    elif wall_setting != "none":
        with tempfile.TemporaryDirectory() as tmp:
            field = Path(tmp) / "field.png"
            logo_big = Path(tmp) / "logo.png"
            gradient_png(field, 3840, 2160, [(0.0, ground), (0.65, darken(accent, 0.55)), (1.0, accent)])
            raster.render(logo_src, logo_big, 288, 288)
            composite_logo(field, logo_big, backgrounds / "default.png", "southeast", "+160+160", 22)
            wallpaper_path = f"/usr/share/backgrounds/{bid}/default.png"
    if wallpaper_path:
        write(pkg / "usr/share/gnome-background-properties" / f"{bid}.xml", f"""<?xml version="1.0"?>
<!DOCTYPE wallpapers SYSTEM "gnome-wp-list.dtd">
<wallpapers>
  <wallpaper deleted="false">
    <name>{name}</name>
    <filename>{wallpaper_path}</filename>
    <filename-dark>{wallpaper_path}</filename-dark>
    <options>zoom</options>
    <shade_type>solid</shade_type>
    <pcolor>{ground}</pcolor>
    <scolor>{accent}</scolor>
  </wallpaper>
</wallpapers>
""")
        write(pkg / "etc/dconf/db/local.d" / f"01-{bid}-brand", f"""[org/gnome/desktop/background]
picture-uri='file://{wallpaper_path}'
picture-uri-dark='file://{wallpaper_path}'
picture-options='zoom'
primary-color='{ground}'

[org/gnome/desktop/screensaver]
picture-uri='file://{wallpaper_path}'
primary-color='{ground}'
""")
        if policy.get("wallpaper") == "locked":
            write(pkg / "etc/dconf/db/local.d/locks" / bid, "/org/gnome/desktop/background/picture-uri\n/org/gnome/desktop/background/picture-uri-dark\n/org/gnome/desktop/screensaver/picture-uri\n")

    # GRUB background for the ISO (picked up by build.sh, not installed on the system).
    gradient_png(work / "grub-background.png", 1024, 768, [(0.0, ground), (1.0, darken(accent, 0.4))], diagonal=False)

    # GRUB theme: for the ISO boot menu (build.sh copies work/grub-theme) and for
    # installed systems (/boot/grub/themes/<id> + /etc/default/grub.d, applied by
    # update-grub when the installer writes the bootloader).
    theme_dir = work / "grub-theme"
    if theme_dir.exists():
        shutil.rmtree(theme_dir)
    theme_dir.mkdir(parents=True)
    gradient_png(theme_dir / "background.png", 1600, 900, [(0.0, ground), (1.0, darken(accent, 0.45))], diagonal=True)
    if not raster.render(logo_src, theme_dir / "logo.png", 96, 96):
        rgba_png(theme_dir / "logo.png", 96, 96, (*hex_to_rgb_int(accent), 255), radius=20, corner="nw")
    ar, ag, ab = hex_to_rgb_int(accent)
    for slice_name, (w, h, corner) in {"c": (8, 8, None), "n": (8, 6, None), "s": (8, 6, None), "e": (6, 8, None), "w": (6, 8, None),
                                       "nw": (6, 6, "nw"), "ne": (6, 6, "ne"), "sw": (6, 6, "sw"), "se": (6, 6, "se")}.items():
        rgba_png(theme_dir / f"select_{slice_name}.png", w, h, (ar, ag, ab, 230), radius=6, corner=corner)
    fonts = grub_fonts(theme_dir)
    write(theme_dir / "theme.txt", grub_theme(bid, name, accent, ground, ink, fonts))
    installed_theme = pkg / "boot/grub/themes" / bid
    shutil.copytree(theme_dir, installed_theme)
    write(pkg / "etc/default/grub.d" / f"90-{bid}-theme.cfg", f"""# {name} boot menu theme (brand kit). Applied by update-grub.
GRUB_THEME=/boot/grub/themes/{bid}/theme.txt
GRUB_GFXMODE=auto
GRUB_TERMINAL_OUTPUT=gfxterm
""")

    # Package metadata.
    scripts = maintainer_scripts(bid)
    debian = pkg / "DEBIAN"
    write(debian / "control", f"""Package: {bid}-branding
Version: {version}
Section: misc
Priority: optional
Architecture: all
Maintainer: {brand.get('vendor', name)} <{(brand.get('urls') or {}).get('support', 'support@example.invalid')}>
Provides: synos-branding
Conflicts: synos-branding
Replaces: synos-branding
Description: {name} brand kit
 Identity of {name}: os-release, icons, boot splash, login logo,
 wallpaper and desktop defaults. Generated from branding/{bid}/ by
 tools/render_brand.py; do not edit the installed files, edit the kit.
""")
    for script_name, content in scripts.items():
        write(debian / script_name, content, 0o755)
    for path in pkg.rglob("*"):
        if path.is_dir():
            path.chmod(0o755)
        elif not (path.stat().st_mode & stat.S_IXUSR):
            path.chmod(0o644)

    deb = work / f"{bid}-branding_{version}_all.deb"
    builder = ["fakeroot"] if shutil.which("fakeroot") else []
    result = subprocess.run(builder + ["dpkg-deb", "--root-owner-group", "--build", str(pkg), str(deb)], check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise ManifestError("dpkg-deb failed: " + result.stderr.strip())
    manifest_out = work / "contents.txt"
    listing = subprocess.run(["dpkg-deb", "-c", str(deb)], check=True, capture_output=True, text=True).stdout
    manifest_out.write_text(listing, encoding="utf-8")
    if raster.skipped:
        print(f"note: no SVG rasteriser ({', '.join(sorted(set(raster.skipped)))} not rendered); install librsvg2-bin", file=sys.stderr)
    return deb


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", default=str(ROOT / "manifest.yml"))
    parser.add_argument("--output-dir", default=str(ROOT / ".build" / "branding"))
    args = parser.parse_args(argv)
    try:
        deb = build(Path(args.manifest).resolve(), Path(args.output_dir).resolve())
    except ManifestError as exc:
        print(f"brand error: {exc}", file=sys.stderr)
        return 1
    rel = deb.relative_to(ROOT) if deb.is_relative_to(ROOT) else deb
    print(f"built {rel}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
