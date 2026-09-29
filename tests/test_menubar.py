"""macOS menu bar classes must load through the real PyObjC bridge."""

from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))


class MenubarImportTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "darwin", "PyObjC runs on macOS")
    def test_hover_table_and_controller_register_without_selector_errors(self):
        menubar = importlib.import_module("menubar")
        self.assertIsNotNone(menubar.HoverTableView)
        self.assertIsNotNone(menubar.Controller)

    @unittest.skipUnless(sys.platform == "darwin", "PyObjC runs on macOS")
    def test_hover_details_update_immediately_and_clear_on_exit(self):
        menubar = importlib.import_module("menubar")
        controller = SimpleNamespace(
            hovered_row=-1,
            rows=[object()],
            detail_label=Mock(),
            table=Mock(),
            row_detail=Mock(return_value="Text · Desktop · Sep 28, 2026, 1:05 PM"),
        )
        menubar.Controller.on_hover_row(controller, 0)
        controller.detail_label.setStringValue_.assert_called_with(
            "Text · Desktop · Sep 28, 2026, 1:05 PM"
        )
        menubar.Controller.on_hover_row(controller, -1)
        controller.detail_label.setStringValue_.assert_called_with("")

    @unittest.skipUnless(sys.platform == "darwin", "PyObjC runs on macOS")
    def test_hover_time_uses_system_date_and_time_styles(self):
        menubar = importlib.import_module("menubar")
        item = SimpleNamespace(kind="text", source="desktop", created_at=1234)
        with patch.object(menubar, "NSDateFormatter") as factory:
            formatter = factory.alloc().init()
            formatter.stringFromDate_.return_value = "Sep 28, 2026, 1:05 PM"
            result = menubar.Controller.row_detail(None, item)
            formatter.setTimeStyle_.assert_called_once_with(menubar.NSDateFormatterShortStyle)
            formatter.setDateStyle_.assert_called_once_with(menubar.NSDateFormatterMediumStyle)
            self.assertIn("1:05 PM", result)


if __name__ == "__main__":
    unittest.main()
