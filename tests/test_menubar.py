"""macOS menu bar classes must load through the real PyObjC bridge."""

from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))


class MenubarImportTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "darwin", "PyObjC runs on macOS")
    def test_hover_table_and_controller_register_without_selector_errors(self):
        menubar = importlib.import_module("menubar")
        self.assertIsNotNone(menubar.HoverTableView)
        self.assertIsNotNone(menubar.Controller)


if __name__ == "__main__":
    unittest.main()
