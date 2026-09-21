"""Control files written by the package builder are always ones dpkg can parse,
whatever the brand kit left empty."""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load_builder():
    spec = importlib.util.spec_from_file_location("build_packages", ROOT / "tools" / "build_packages.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class ControlFieldTests(unittest.TestCase):
    def test_empty_fields_are_dropped_and_the_file_ends_cleanly(self) -> None:
        builder = load_builder()
        control = ("Package: synos-live-layers\nVersion: 1\nArchitecture: all\n"
                   "Maintainer: Acme <>\nDepends: dracut\nHomepage: \n"
                   "Description: Acme Live root\n more words\n")
        out = builder.finish_control(control, {"BRAND_ID": "acme"})
        self.assertNotIn("Homepage", out)
        self.assertIn("Maintainer: Acme <acme@localhost>\n", out)
        self.assertIn("Description: Acme Live root\n more words\n", out)
        self.assertTrue(out.endswith("\n") and not out.endswith("\n\n"))
        for line in out.splitlines():
            self.assertFalse(line.endswith(":"), line)

    def test_a_complete_control_is_left_as_it_is(self) -> None:
        builder = load_builder()
        control = "Package: x\nMaintainer: Acme <help@acme.example>\nHomepage: https://acme.example\nDescription: x\n"
        self.assertEqual(control, builder.finish_control(control, {"BRAND_ID": "acme"}))

    def test_every_recipe_control_survives_an_empty_brand_kit(self) -> None:
        builder = load_builder()
        subs = {key: "" for key in ("BRAND_URL_HOME", "BRAND_URL_SUPPORT", "BRAND_URL_BUG", "BRAND_URL_PRIVACY")}
        subs.update({"BRAND_NAME": "Acme", "BRAND_VENDOR": "Acme", "BRAND_ID": "acme"})
        for control in sorted((ROOT / "packages").glob("*/control")):
            text = control.read_text(encoding="utf-8")
            for key, value in subs.items():
                text = text.replace("${" + key + "}", value)
            out = builder.finish_control(text, subs)
            self.assertNotIn("<>", out, control)
            self.assertTrue(all(not l.endswith(":") for l in out.splitlines()), control)
            self.assertTrue(out.endswith("\n"), control)


if __name__ == "__main__":
    unittest.main()
