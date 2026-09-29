"""The menu bar's decisions, and menubar.py itself loaded against a stand-in for PyObjC."""

from __future__ import annotations

import importlib
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import hotkeys
import menubar_logic as logic


class PureLogicTests(unittest.TestCase):
    def test_the_clock(self):
        self.assertEqual(
            [logic.clock(s) for s in (0, 9, 65, 600)], ["0:00", "0:09", "1:05", "10:00"]
        )

    def test_status_button(self):
        self.assertEqual(logic.status_button("recording", True, 75), ("recording", " 1:15"))
        self.assertEqual(logic.status_button("transcribing", True, 0), ("idle", " …"))
        self.assertEqual(logic.status_button("idle", False, 0), ("idle", ""))

    def test_popover_header(self):
        recording = logic.popover_header("recording", True, 5, True, True, "⌃⇧D")
        self.assertEqual(recording, logic.Header("Recording… 0:05", "Stop", True, show_cancel=True))
        busy = logic.popover_header("transcribing", True, 0, True, True, "⌃⇧D")
        self.assertEqual((busy.status, busy.button, busy.enabled), ("Transcribing…", "…", False))
        self.assertEqual(
            logic.popover_header("idle", False, 0, False, True, "⌃⇧D").status,
            "Finish setup to start",
        )
        self.assertEqual(
            logic.popover_header("idle", False, 0, True, True, "⌃⇧D").status,
            "Press ⌃⇧D anywhere to dictate",
        )
        taken = logic.popover_header("idle", False, 0, True, False, "⌃⇧D")
        self.assertIn("is taken", taken.status)
        self.assertEqual((taken.button, taken.enabled, taken.show_cancel), ("Start", True, False))

    def test_search_text_is_normalised(self):
        self.assertEqual(logic.normalize_query("  hello \n\t  world  "), "hello world")
        self.assertEqual(logic.normalize_query(""), "")
        self.assertEqual(len(logic.normalize_query("x" * 1000)), logic.QUERY_LIMIT)

    def test_the_empty_list_says_why(self):
        self.assertEqual(logic.empty_message(False, ""), "Clipboard history isn’t available.")
        self.assertEqual(logic.empty_message(True, "abc"), "No matches.")
        self.assertEqual(logic.empty_message(True, ""), "Nothing copied yet.")

    def test_keyboard_selection_stays_in_the_list(self):
        self.assertEqual(logic.next_selection(0, -1, 5), 0)
        self.assertEqual(logic.next_selection(4, 1, 5), 4)
        self.assertEqual(logic.next_selection(2, 1, 5), 3)
        self.assertEqual(logic.next_selection(3, 0, 2), 1)  # A shorter list after a search.
        self.assertEqual(logic.next_selection(3, 1, 0), 0)

    def test_hover_details(self):
        text = SimpleNamespace(kind="text", source="desktop", width=0, height=0)
        image = SimpleNamespace(kind="image", source="cloud", width=640, height=480)
        other = SimpleNamespace(kind="text", source="import", width=0, height=0)
        self.assertEqual(logic.row_detail(text, "Sep 28"), "Text · Desktop · Sep 28")
        self.assertEqual(logic.row_detail(image, "Sep 28"), "Image 640×480 · Cloud · Sep 28")
        self.assertEqual(logic.row_detail(other, "x"), "Text · import · x")

    def test_search_field_keys(self):
        expected = {
            "moveDown:": "down",
            "moveUp:": "up",
            "insertNewline:": "activate",
            "cancelOperation:": "close",
            "moveToBeginningOfParagraph:": "select_all",
        }
        for selector, action in expected.items():
            self.assertEqual(logic.key_action(selector), action)
        self.assertEqual(logic.key_action("deleteBackward:"), "")

    def test_hotkey_codes_come_from_the_shortcut(self):
        shortcut = hotkeys.Shortcut(("ctrl", "shift"), "D")
        code, modifiers = logic.carbon_hotkey(shortcut)
        self.assertEqual(code, 2)  # kVK_ANSI_D
        self.assertEqual(modifiers, 0x1000 | 0x200)
        self.assertEqual(logic.carbon_hotkey(hotkeys.Shortcut(("cmd",), "Space")), (49, 0x100))
        self.assertEqual(logic.fourcc("WDct"), 0x57446374)

    def test_accessibility_is_explained_once_and_only_when_it_is_off(self):
        self.assertTrue(logic.needs_accessibility_explanation(False, False))
        self.assertFalse(logic.needs_accessibility_explanation(False, True))
        self.assertFalse(logic.needs_accessibility_explanation(True, False))
        self.assertFalse(logic.needs_accessibility_explanation(None, False))


def fake_pyobjc() -> dict[str, types.ModuleType]:
    """Modules that stand in for objc, AppKit and Foundation: every name is a mock, and the
    classes menubar.py inherits from are real (empty) classes."""

    class Module(types.ModuleType):
        def __getattr__(self, name: str) -> object:
            if name.startswith("__"):
                raise AttributeError(name)
            value = MagicMock(name=name)
            setattr(self, name, value)
            return value

    objc = Module("objc")
    objc.python_method = lambda function: function  # type: ignore[attr-defined]
    appkit, foundation = Module("AppKit"), Module("Foundation")
    for module, names in (
        (appkit, ("NSTableView", "NSView", "NSViewController")),
        (foundation, ("NSObject",)),
    ):
        for name in names:
            setattr(module, name, type(name, (), {}))
    return {"objc": objc, "AppKit": appkit, "Foundation": foundation}


class MenubarBoundaryTests(unittest.TestCase):
    """menubar.py run on any system, with PyObjC replaced at the import boundary."""

    def setUp(self):
        modules = fake_pyobjc()
        patcher = patch.dict(sys.modules, modules)
        patcher.start()
        self.addCleanup(patcher.stop)
        sys.modules.pop("menubar", None)
        self.menubar = importlib.import_module("menubar")
        self.addCleanup(sys.modules.pop, "menubar", None)

    def controller(self, **fields):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        paths = SimpleNamespace(
            config=Path(temp.name) / "config.json", cache=Path(temp.name), runtime=Path(temp.name)
        )
        return SimpleNamespace(paths=paths, **fields)

    def test_it_loads_without_a_mac(self):
        self.assertTrue(hasattr(self.menubar, "Controller"))
        self.assertEqual(self.menubar.fourcc("WDct"), 0x57446374)

    def test_the_search_box_keys_drive_the_popover(self):
        me = self.controller(
            move_selection=MagicMock(), activate_selected=MagicMock(), popover=MagicMock()
        )
        text_view = MagicMock()
        handle = self.menubar.Controller.control_textView_doCommandBySelector_
        self.assertTrue(handle(me, None, text_view, "moveDown:"))
        me.move_selection.assert_called_with(1)
        self.assertTrue(handle(me, None, text_view, "moveUp:"))
        me.move_selection.assert_called_with(-1)
        self.assertTrue(handle(me, None, text_view, "insertNewline:"))
        me.activate_selected.assert_called_once()
        self.assertTrue(handle(me, None, text_view, "cancelOperation:"))
        me.popover.close.assert_called_once()
        self.assertTrue(handle(me, None, text_view, "moveToBeginningOfParagraph:"))
        text_view.selectAll_.assert_called_once_with(None)
        self.assertFalse(handle(me, None, text_view, "deleteBackward:"))

    def test_selection_moves_and_clamps(self):
        me = self.controller(rows=[1, 2, 3], selected=2, table=MagicMock())
        me.select_row = lambda row: self.menubar.Controller.select_row(me, row)
        self.menubar.Controller.move_selection(me, 5)
        self.assertEqual(me.selected, 2)
        self.menubar.Controller.move_selection(me, -9)
        self.assertEqual(me.selected, 0)

    def test_an_empty_search_says_why(self):
        store = MagicMock()
        store.list.return_value = []
        me = self.controller(
            open_store=lambda: store,
            table=MagicMock(),
            detail_label=MagicMock(),
            empty_label=MagicMock(),
            select_row=MagicMock(),
        )
        self.menubar.Controller.run_query(me, "  zzz  ")
        store.list.assert_called_once_with(query="zzz", limit=self.menubar.POPOVER_ROWS)
        me.empty_label.setStringValue_.assert_called_with("No matches.")
        me.open_store = lambda: None
        self.menubar.Controller.run_query(me, "")
        me.empty_label.setStringValue_.assert_called_with("Clipboard history isn’t available.")

    def test_the_header_follows_the_session(self):
        me = self.controller(
            clip=SimpleNamespace(features=lambda: SimpleNamespace(dictation=True)),
            shortcut=hotkeys.Shortcut(("ctrl", "shift"), "D"),
            hotkey_ok=True,
            ready=lambda: True,
            cancel_button=MagicMock(),
            header_status=MagicMock(),
            header_button=MagicMock(),
        )
        snapshot = {"phase": "recording", "active": True, "elapsed_seconds": 7}
        with patch.object(self.menubar.workflow, "snapshot", return_value=snapshot):
            self.menubar.Controller.refresh_popover_header(me)
        me.header_status.setStringValue_.assert_called_with("Recording… 0:07")
        me.header_button.setTitle_.assert_called_with("Stop")
        me.cancel_button.setHidden_.assert_called_with(False)

    def test_accessibility_is_explained_before_the_first_paste_only(self):
        alert = self.menubar.NSAlert.alloc.return_value.init.return_value
        alert.runModal.return_value = self.menubar.NSAlertFirstButtonReturn
        me = self.controller()
        me.paths.cache.mkdir(parents=True, exist_ok=True)
        with (
            patch.object(self.menubar.permissions, "accessibility_trusted", return_value=False),
            patch.object(self.menubar.permissions, "open_settings") as opened,
        ):
            self.menubar.Controller.explain_accessibility(me)
            self.menubar.Controller.explain_accessibility(me)  # Told once.
        alert.runModal.assert_called_once()
        self.assertIn("Accessibility", alert.setInformativeText_.call_args.args[0])
        opened.assert_called_once_with(self.menubar.permissions.ACCESSIBILITY_URL)

    def test_no_explanation_when_trusted_or_when_paste_is_off(self):
        alert = self.menubar.NSAlert.alloc.return_value.init.return_value
        me = self.controller()
        with patch.object(self.menubar.permissions, "accessibility_trusted", return_value=True):
            self.menubar.Controller.explain_accessibility(me)
        me.paths.config.write_text('{"auto_paste": false}', encoding="utf-8")
        with patch.object(self.menubar.permissions, "accessibility_trusted", return_value=False):
            self.menubar.Controller.explain_accessibility(me)
        alert.runModal.assert_not_called()


if __name__ == "__main__":
    unittest.main()
