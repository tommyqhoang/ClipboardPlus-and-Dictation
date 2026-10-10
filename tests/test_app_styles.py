"""The window theme: the modern amber one loads, and the classic one is the fallback."""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import app_styles

LIB = Path(app_styles.__file__).parent


class ThemeFilesTests(unittest.TestCase):
    """The shipped theme is data; these need no display."""

    def test_the_theme_and_its_sprite_sheet_ship_together(self):
        self.assertTrue(app_styles.THEME_FILE.is_file())
        self.assertTrue(app_styles.THEME_FILE.with_suffix(".png").is_file())
        text = app_styles.THEME_FILE.read_text(encoding="utf-8")
        self.assertIn(f"theme create {app_styles.THEME_NAME}", text)
        self.assertIn("clipboardplus-theme.png", text)

    def test_the_theme_keeps_its_license_notice(self):
        self.assertIn("MIT License, Copyright (c) rdbende", app_styles.THEME_FILE.read_text())

    def test_the_accent_is_the_brand_amber_not_sun_valley_blue(self):
        text = app_styles.THEME_FILE.read_text(encoding="utf-8").lower()
        self.assertIn(app_styles.ACCENT, text)
        self.assertNotIn("#005fb8", text)
        self.assertNotIn("#2f60d8", text)

    def test_every_sprite_the_styles_use_is_defined(self):
        text = app_styles.THEME_FILE.read_text(encoding="utf-8")
        defined = set(re.findall(r"^\s+([a-z][\w-]*) \d+ \d+ \d+ \d+ \\?$", text, re.M))
        used = set(re.findall(r"\$I\(([\w-]+)\)", text))
        self.assertFalse(used - defined, f"sprites used but never defined: {used - defined}")


class ThemeLoadingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import tkinter as tk

            probe = tk.Tk()
            probe.withdraw()
            probe.destroy()
        except Exception as exc:  # noqa: BLE001
            raise unittest.SkipTest(f"Tk/display unavailable: {exc}")

    def setUp(self):
        import tkinter as tk
        from tkinter import ttk

        self.root = tk.Tk()
        self.root.withdraw()
        self.ttk = ttk
        self.fonts = app_styles.make_fonts()

    def tearDown(self):
        self.root.destroy()

    def test_the_modern_theme_is_used_and_styles_borrow_its_buttons(self):
        tk_version = self.root.tk.call("info", "patchlevel")
        self.assertTrue(
            app_styles.apply(self.root, self.fonts),
            f"Tk {tk_version}: {app_styles.load_error}",
        )
        style = self.ttk.Style(self.root)
        self.assertEqual(style.theme_use(), app_styles.THEME_NAME)
        for name in ("Primary.TButton", "Danger.TButton", "Tab.TButton", "Link.TButton"):
            self.assertTrue(style.layout(name), name)
        self.assertIn("AccentButton.button", str(style.layout("Primary.TButton")))
        self.assertIn("DangerButton.button", str(style.layout("Danger.TButton")))
        # Every themed widget class builds without a missing-element error.
        for widget in ("Button", "Checkbutton", "Radiobutton", "Entry", "Combobox", "Progressbar"):
            getattr(self.ttk, widget)(self.root).destroy()
        self.ttk.Scrollbar(self.root, orient="vertical").destroy()
        self.ttk.Frame(self.root, style="Panel.TFrame").destroy()
        self.root.update_idletasks()

    def test_applying_the_theme_again_reuses_it_instead_of_failing(self):
        # Sourcing the theme twice raises "Theme clipboardplus already exists"; on macOS
        # that happened for every window after the first and fell back to the old look.
        self.assertTrue(app_styles.apply(self.root, self.fonts))
        self.assertTrue(app_styles.apply(self.root, self.fonts), app_styles.load_error)
        self.assertEqual(self.ttk.Style(self.root).theme_use(), app_styles.THEME_NAME)

    def test_the_scrollbar_has_no_arrow_buttons(self):
        app_styles.apply(self.root, self.fonts)
        layout = str(self.ttk.Style(self.root).layout("Vertical.TScrollbar"))
        self.assertIn("thumb", layout)
        self.assertNotIn("arrow", layout)

    def test_a_missing_theme_falls_back_to_the_classic_one(self):
        with patch.object(app_styles, "THEME_FILE", LIB / "no-such-theme.tcl"):
            self.assertFalse(app_styles.apply(self.root, self.fonts))
        style = self.ttk.Style(self.root)
        self.assertEqual(style.theme_use(), "clam")
        self.assertEqual(style.lookup("Primary.TButton", "background"), app_styles.ACCENT)


if __name__ == "__main__":
    unittest.main()
