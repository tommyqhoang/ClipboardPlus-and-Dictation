"""macOS menu bar classes must load through the real PyObjC bridge."""

from __future__ import annotations

import importlib
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

HAS_PYOBJC = importlib.util.find_spec("objc") is not None


class MenubarImportTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "darwin" and HAS_PYOBJC, "PyObjC runs on macOS")
    def test_hover_table_and_controller_register_without_selector_errors(self):
        menubar = importlib.import_module("menubar")
        self.assertIsNotNone(menubar.HoverTableView)
        self.assertIsNotNone(menubar.Controller)

    @unittest.skipUnless(sys.platform == "darwin" and HAS_PYOBJC, "PyObjC runs on macOS")
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

    @unittest.skipUnless(sys.platform == "darwin" and HAS_PYOBJC, "PyObjC runs on macOS")
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

    @unittest.skipUnless(sys.platform == "darwin" and HAS_PYOBJC, "PyObjC runs on macOS")
    def test_native_context_menu_has_valid_targets_and_expected_actions(self):
        # Build real Cocoa objects without showing a window, starting capture,
        # registering hotkeys, or touching the user's settings.
        menubar = importlib.import_module("menubar")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            paths = SimpleNamespace(
                config=root / "config.json", cache=root, runtime=root, text=root / "last.txt"
            )
            with patch.object(menubar.d, "Paths", return_value=paths):
                controller = menubar.Controller.alloc().init()
            controller.preferences.save(features=menubar.hotkeys.Features(True, True))
            with patch.object(
                menubar.workflow, "snapshot", return_value={"phase": "idle", "active": False}
            ):
                menu = controller.context_menu()
            entries = {entry.title(): entry for entry in menu.itemArray()}
            for label in ("Clipboard History…", "Settings…", "Open at Login", "Quit Clipboard+"):
                self.assertTrue(entries[label].isEnabled())
                self.assertEqual(entries[label].target(), controller)
                self.assertTrue(controller.respondsToSelector_(entries[label].action()))
            pause = entries["Pause Clipboard Capture"].submenu()
            self.assertEqual(
                [item.title() for item in pause.itemArray()], ["For 1 Hour", "Until I Resume"]
            )

    @unittest.skipUnless(sys.platform == "darwin" and HAS_PYOBJC, "PyObjC runs on macOS")
    def test_reopening_an_existing_menubar_requests_icon_restore_and_opens_window(self):
        menubar = importlib.import_module("menubar")
        with tempfile.TemporaryDirectory() as folder:
            paths = SimpleNamespace(runtime=Path(folder))
            with (
                patch.object(menubar.d, "Paths", return_value=paths),
                patch.object(menubar.desktop, "platform_name", return_value="macos"),
                patch.object(menubar.desktop, "lock", return_value=None),
                patch.object(menubar, "open_app_window") as open_window,
                patch.dict(menubar.os.environ, {}, clear=True),
            ):
                self.assertEqual(menubar.main(), 0)
            self.assertEqual((paths.runtime / menubar.STATUS_ITEM_RESTORE).read_text(), "restore")
            open_window.assert_called_once_with()

    @unittest.skipUnless(sys.platform == "darwin" and HAS_PYOBJC, "PyObjC runs on macOS")
    def test_restore_status_item_replaces_the_lost_native_item(self):
        menubar = importlib.import_module("menubar")
        controller = SimpleNamespace(popover=Mock(), item=object(), view=("idle",))
        controller.install_status_item = Mock()
        status_bar = Mock()
        native_status_bar = SimpleNamespace(systemStatusBar=status_bar)
        with patch.object(menubar, "NSStatusBar", native_status_bar):
            menubar.Controller.restore_status_item(controller)
        controller.popover.close.assert_called_once_with()
        status_bar.return_value.removeStatusItem_.assert_called_once_with(controller.item)
        controller.install_status_item.assert_called_once_with()
        self.assertIsNone(controller.view)


if __name__ == "__main__":
    unittest.main()
