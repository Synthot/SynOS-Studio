"""Every regions/<id>.yml is real data: it resolves in the renderer, its locales exist in glibc,
its keyboard layout in XKB and its time zone in the zone database (each checked when that
database is present on the machine running the tests), and YAML does not turn its id into a
boolean (Norway's `no`)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import render_manifest as rm  # noqa: E402

SUPPORTED = Path("/usr/share/i18n/SUPPORTED")
XKB = Path("/usr/share/X11/xkb/rules/base.lst")
ZONEINFO = Path("/usr/share/zoneinfo")
GROUPS = {"europe", "americas", "asia-pacific", "middle-east-africa"}


def xkb_layouts() -> set[str]:
    layouts, section = set(), None
    for line in XKB.read_text(encoding="utf-8").splitlines():
        if line.startswith("!"):
            section = line[1:].strip()
        elif section == "layout" and line.strip():
            layouts.add(line.split()[0])
    return layouts


class RegionFilesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ids = sorted(p.stem for p in (ROOT / "regions").glob("*.yml"))
        self.assertGreaterEqual(len(self.ids), 60)

    def test_every_region_resolves_and_is_well_formed(self) -> None:
        regions = rm.resolve_regions(self.ids)   # raises on a bad id, name, locale, keyboard or timezone
        by_id = {r["id"]: r for r in regions}
        for rid in self.ids:
            region = by_id[rid]
            self.assertIsInstance(region["id"], str, f"{rid}: YAML must not read the id as a boolean")
            self.assertIsInstance(region["keyboard"], str, f"{rid}: keyboard must be a quoted string")
            self.assertIn(region.get("group"), GROUPS, f"{rid}: group")
            self.assertIn(region.get("paper"), ("a4", "letter"), f"{rid}: paper")
            self.assertIsInstance(region.get("eid", []), list, f"{rid}: eid")
            self.assertTrue(region.get("compliance"), f"{rid}: compliance (use 'none' explicitly)")
            self.assertTrue(region["_locales"], f"{rid}: at least one locale")
        rows = rm.live_region_rows(regions)
        self.assertEqual(len({row[0] for row in rows}), len(rows), "live rows have unique codes")

    @unittest.skipUnless(SUPPORTED.is_file(), "glibc locale list not on this machine")
    def test_locales_exist_in_glibc(self) -> None:
        supported = {line.split()[0].replace(".UTF-8", "") for line in SUPPORTED.read_text(encoding="utf-8").splitlines() if "UTF-8" in line}
        for region in rm.resolve_regions(self.ids):
            for locale in region["_locales"]:
                self.assertIn(locale, supported, f"{region['id']}: {locale}")

    @unittest.skipUnless(XKB.is_file(), "XKB rules not on this machine")
    def test_keyboards_exist_in_xkb(self) -> None:
        layouts = xkb_layouts()
        for region in rm.resolve_regions(self.ids):
            self.assertIn(region["keyboard"], layouts, region["id"])

    @unittest.skipUnless(ZONEINFO.is_dir(), "zoneinfo not on this machine")
    def test_timezones_exist(self) -> None:
        for region in rm.resolve_regions(self.ids):
            self.assertTrue((ZONEINFO / region["timezone"]).is_file(), f"{region['id']}: {region['timezone']}")


if __name__ == "__main__":
    unittest.main()
