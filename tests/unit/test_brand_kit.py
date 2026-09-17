"""The brand kit package is the only carrier of user-visible identity."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("render_brand", ROOT / "tools" / "render_brand.py")
render_brand = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(render_brand)


def _listing(deb: Path) -> list[str]:
    output = subprocess.run(("dpkg-deb", "-c", str(deb)), check=True, capture_output=True, text=True).stdout
    return [line.split()[5].lstrip(".") for line in output.splitlines()]


class BrandKitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.deb = render_brand.build(ROOT / "manifest.yml", Path(cls.tmp.name))
        cls.files = _listing(cls.deb)
        cls.work = cls.deb.parent

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def test_package_metadata_names_the_brand(self) -> None:
        control = subprocess.run(("dpkg-deb", "-f", str(self.deb)), check=True, capture_output=True, text=True).stdout
        self.assertIn("Package: synos-branding", control)
        self.assertIn("Architecture: all", control)
        self.assertIn("Provides: synos-branding", control)

    def test_os_release_is_the_brand_on_top_of_the_base(self) -> None:
        text = (self.work / "pkg/usr/share/synos/branding/os-release").read_text(encoding="utf-8")
        self.assertIn("ID=synos\n", text)
        self.assertIn('NAME="SynOS"\n', text)
        self.assertIn("VERSION_CODENAME=resolute\n", text)
        self.assertIn('ID_LIKE="ubuntu debian"\n', text)
        self.assertIn("LOGO=synos\n", text)
        self.assertIn("/usr/share/synos/branding/os-release", self.files)

    def test_maintainer_scripts_divert_os_release_and_register_plymouth(self) -> None:
        pkg = self.work / "pkg/DEBIAN"
        postinst = (pkg / "postinst").read_text(encoding="utf-8")
        self.assertIn("dpkg-divert --package synos-branding --divert /usr/lib/os-release.base --rename --add /usr/lib/os-release", postinst)
        self.assertIn("update-alternatives --set default.plymouth /usr/share/plymouth/themes/synos/synos.plymouth", postinst)
        self.assertIn("dconf update", postinst)
        postrm = (pkg / "postrm").read_text(encoding="utf-8")
        self.assertIn("dpkg-divert --package synos-branding --rename --remove /usr/lib/os-release", postrm)
        for name in ("postinst", "prerm", "postrm"):
            self.assertTrue((pkg / name).stat().st_mode & 0o111, name)

    def test_visible_identity_assets_are_present(self) -> None:
        for path in (
            "/usr/share/icons/hicolor/scalable/apps/synos.svg",
            "/usr/share/icons/hicolor/scalable/apps/distributor-logo.svg",
            "/usr/share/plymouth/themes/synos/synos.plymouth",
            "/usr/share/plymouth/themes/synos/synos.script",
            "/etc/dconf/db/gdm.d/01-synos-logo",
            "/usr/share/synos/branding/brand.env",
            "/usr/share/synos/branding/logo.svg",
        ):
            self.assertIn(path, self.files, path)
        gdm = (self.work / "pkg/etc/dconf/db/gdm.d/01-synos-logo").read_text(encoding="utf-8")
        self.assertIn("[org/gnome/login-screen]", gdm)
        theme = (self.work / "pkg/usr/share/plymouth/themes/synos/synos.plymouth").read_text(encoding="utf-8")
        self.assertIn("ModuleName=script", theme)

    def test_wallpaper_policy_follows_the_brand_kit(self) -> None:
        # The SynOS kit derives its wallpaper and leaves it unlocked.
        self.assertIn("/usr/share/backgrounds/synos/default.png", self.files)
        self.assertIn("/etc/dconf/db/local.d/01-synos-brand", self.files)
        self.assertNotIn("/etc/dconf/db/local.d/locks/synos", self.files)
        dconf = (self.work / "pkg/etc/dconf/db/local.d/01-synos-brand").read_text(encoding="utf-8")
        self.assertIn("picture-uri='file:///usr/share/backgrounds/synos/default.png'", dconf)

    def test_generated_pngs_are_eight_bit(self) -> None:
        # GRUB refuses 16-bit PNGs; the gradients are written by the tool itself.
        theme = self.work / "grub-theme"
        self.assertTrue((theme / "theme.txt").is_file() and (theme / "background.png").is_file() and (theme / "logo.png").is_file())
        for slice_name in ("c", "n", "s", "e", "w", "nw", "ne", "sw", "se"):
            png = (theme / f"select_{slice_name}.png").read_bytes()
            self.assertEqual(b"\x89PNG", png[:4])
            self.assertEqual(6, png[25], "highlight slices carry alpha")
        text = (theme / "theme.txt").read_text(encoding="utf-8")
        self.assertIn("+ boot_menu {", text)
        self.assertIn('selected_item_pixmap_style = "select_*.png"', text)
        self.assertIn('id = "__timeout__"', text)
        self.assertTrue(any(f.endswith("boot/grub/themes/synos/theme.txt") for f in self.files), "installed systems get the theme")
        self.assertTrue(any(f.endswith("etc/default/grub.d/90-synos-theme.cfg") for f in self.files))
        grub = (self.work / "grub-background.png").read_bytes()
        self.assertEqual(b"\x89PNG", grub[:4])
        self.assertEqual(8, grub[24])   # bit depth
        self.assertEqual(2, grub[25])   # colour type: RGB
        self.assertEqual((1024, 768), (int.from_bytes(grub[16:20], "big"), int.from_bytes(grub[20:24], "big")))

    def test_build_and_mods_consume_the_kit(self) -> None:
        build = (ROOT / "build.sh").read_text(encoding="utf-8")
        self.assertIn('.build/branding/$BRAND_ID/${BRAND_ID}-branding_', build)
        self.assertIn("grub-background.png", build)
        self.assertIn("background_image /boot/grub/background.png", build)
        mod = (ROOT / "mods/07-brand-kit/install.sh").read_text(encoding="utf-8")
        self.assertIn('/root/branding/"${BRAND_ID}"-branding_*_all.deb', mod)
        self.assertIn('test "$os_id" = "$BRAND_ID"', mod)
        mods = sorted(p.name for p in (ROOT / "mods").iterdir() if p.is_dir())
        self.assertLess(mods.index("05-live-kernel-apps-installer"), mods.index("07-brand-kit"))
        self.assertLess(mods.index("07-brand-kit"), mods.index("80-dracut-live-image"))
        assertion = (ROOT / "tests/assertions/install.py").read_text(encoding="utf-8")
        self.assertIn('/usr/share/plymouth/themes/$brand/$brand.plymouth', assertion)


if __name__ == "__main__":
    unittest.main()


class CustomerBrandKitTests(unittest.TestCase):
    def test_locked_wallpaper_policy_ships_dconf_locks(self) -> None:
        import json
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "manifest.yml"
            manifest.write_text(json.dumps({
                "schema_version": 1, "name": "acme", "version": "2.1.0", "base": "debian", "suite": "trixie",
                "arch": "amd64", "profile": "workstation", "regions": ["fr"], "brand": "example-acme",
            }), encoding="utf-8")
            deb = render_brand.build(manifest, Path(directory) / "out")
            files = _listing(deb)
            self.assertIn("/etc/dconf/db/local.d/locks/example-acme", files)
            self.assertIn("/usr/share/plymouth/themes/example-acme/example-acme.plymouth", files)
            text = (deb.parent / "pkg/usr/share/example-acme/branding/os-release").read_text(encoding="utf-8")
            self.assertIn('PRETTY_NAME="Acme Workstation 2.1.0"', text)
            self.assertIn('ID_LIKE="debian"', text)
            self.assertIn("DEBIAN_CODENAME=trixie", text)
            self.assertTrue(deb.name.startswith("example-acme-branding_2.1.0_"))


class BrandKitOverlapTests(unittest.TestCase):
    """The brand kit and the product packages must never ship the same path."""

    def test_no_product_package_ships_a_brand_kit_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            deb = render_brand.build(ROOT / "manifest.yml", Path(directory))
            brand_files = set(_listing(deb))
        shipped: dict[str, str] = {}
        for source in sorted(p for p in (ROOT / "packages").iterdir() if (p / "control").is_file()):
            for tree in ("assets", "templates"):
                base = source / tree
                if base.is_dir():
                    for path in base.rglob("*"):
                        if path.is_file():
                            shipped["/" + str(path.relative_to(base))] = source.name
            includes = source / "includes.txt"
            if includes.is_file():
                for line in includes.read_text(encoding="utf-8").splitlines():
                    if line and not line.startswith("#") and line.split("\t")[0] == "file":
                        shipped[line.split("\t")[2]] = source.name
        overlap = {path: owner for path, owner in shipped.items() if path in brand_files}
        self.assertEqual({}, overlap)
