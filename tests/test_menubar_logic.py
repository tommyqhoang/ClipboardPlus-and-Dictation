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

    def test_registration_failures_are_told_apart(self):
        self.assertIsNone(logic.registration_failure(0))
        conflict = logic.registration_failure(-9878)
        self.assertEqual(conflict.kind, "conflict")
        self.assertIn("another app", conflict.message("⌃⇧D", "/log"))
        self.assertEqual(logic.registration_failure(-9879).kind, "invalid")
        other = logic.registration_failure(-50)
        self.assertEqual(other.kind, "error")
        self.assertIn("-50", other.message("⌃⇧D", "/logs/menubar.log"))
        self.assertIn("/logs/menubar.log", other.message("⌃⇧D", "/logs/menubar.log"))
        # A handler that could not be installed explains every shortcut, whatever the status.
        handler = logic.registration_failure(0, -30)
        self.assertEqual(handler.kind, "handler")
        self.assertIn("listen for shortcuts", handler.message("⌃⇧D", "/log"))

    def test_a_guarded_callback_logs_the_traceback_and_returns_no_error(self):
        log = MagicMock()

        def boom():
            raise ValueError("bad")

        self.assertEqual(logic.guarded(boom, log, "dictation")(), 0)
        log.exception.assert_called_once()
        calls = []
        self.assertEqual(logic.guarded(lambda: calls.append(1), log, "history")(), 0)
        self.assertEqual(calls, [1])


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

    def hotkey(self, register_status=0, install_status=0):
        """A GlobalHotKey over a stand-in for the Carbon framework."""
        import ctypes

        carbon = MagicMock()
        carbon.InstallEventHandler.return_value = install_status
        carbon.RegisterEventHotKey.return_value = register_status
        carbon.UnregisterEventHotKey.return_value = 0
        carbon.GetEventParameter.return_value = 0
        patcher = patch.object(ctypes, "CDLL", return_value=carbon)
        patcher.start()
        self.addCleanup(patcher.stop)
        calls = []
        key = self.menubar.GlobalHotKey(lambda: calls.append("dictation"))
        return key, carbon, calls

    def press(self, key, carbon, hotkey_id, status=0):
        def fill(_event, _param, _type, _actual, _size, _out, target):
            target._obj.id = hotkey_id
            return status

        carbon.GetEventParameter.side_effect = fill
        return key.handler(None, None, None)

    def test_a_shortcut_that_registers_reports_success(self):
        key, carbon, _ = self.hotkey()
        self.assertTrue(key.register(hotkeys.DEFAULT))
        self.assertEqual(key.last_status, 0)
        self.assertEqual(key.reason(self.menubar.DICTATION_ID, "⌃⇧D"), "")
        code, mods = carbon.RegisterEventHotKey.call_args.args[:2]
        self.assertEqual((code, mods), logic.carbon_hotkey(hotkeys.DEFAULT))

    def test_a_taken_shortcut_is_a_conflict_and_other_errors_are_not(self):
        key, _, _ = self.hotkey(register_status=-9878)
        self.assertFalse(key.register(hotkeys.DEFAULT))
        self.assertEqual(key.last_status, -9878)
        self.assertIn("another app", key.reason(self.menubar.DICTATION_ID, "⌃⇧D"))
        key, _, _ = self.hotkey(register_status=-9879)
        self.assertFalse(key.register(hotkeys.DEFAULT))
        self.assertIn("doesn’t accept", key.reason(self.menubar.DICTATION_ID, "⌃⇧D"))
        key, _, _ = self.hotkey(register_status=-50)
        self.assertFalse(key.register(hotkeys.DEFAULT))
        reason = key.reason(self.menubar.DICTATION_ID, "⌃⇧D")
        self.assertNotIn("another app", reason)
        self.assertIn("menubar.log", reason)

    def test_a_handler_that_will_not_install_is_reported_not_blamed_on_the_keys(self):
        key, carbon, _ = self.hotkey(install_status=-30)
        self.assertEqual(key.install_status, -30)
        self.assertFalse(key.register(hotkeys.DEFAULT))
        carbon.RegisterEventHotKey.assert_not_called()
        self.assertIn("listen for shortcuts", key.reason(self.menubar.DICTATION_ID, "⌃⇧D"))

    def test_registering_again_clears_the_old_failure_and_unregisters_first(self):
        key, carbon, _ = self.hotkey(register_status=-9878)
        key.register(hotkeys.DEFAULT)
        carbon.RegisterEventHotKey.return_value = 0
        self.assertTrue(key.register(hotkeys.DEFAULT))
        self.assertEqual(key.reason(self.menubar.DICTATION_ID, "⌃⇧D"), "")
        key.register(hotkeys.DEFAULT)
        carbon.UnregisterEventHotKey.assert_called()

    def test_each_shortcut_id_reaches_its_own_action(self):
        key, carbon, calls = self.hotkey()
        key.on(self.menubar.HISTORY_ID, lambda: calls.append("history"))
        self.assertEqual(self.press(key, carbon, self.menubar.DICTATION_ID), 0)
        self.assertEqual(self.press(key, carbon, self.menubar.HISTORY_ID), 0)
        self.assertEqual(calls, ["dictation", "history"])

    def test_an_error_in_a_shortcut_action_is_logged_and_carbon_gets_no_error(self):
        key, carbon, _ = self.hotkey()

        def boom():
            raise RuntimeError("bad")

        with patch.object(self.menubar, "log") as log:
            key.on(self.menubar.HISTORY_ID, boom)
            self.assertEqual(self.press(key, carbon, self.menubar.HISTORY_ID), 0)
        self.assertTrue(log.exception.called)

    def test_an_unreadable_event_still_returns_no_error(self):
        key, carbon, calls = self.hotkey()
        with patch.object(self.menubar, "log") as log:
            self.assertEqual(self.press(key, carbon, 0, status=-50), 0)
        log.warning.assert_called()
        self.assertEqual(calls, ["dictation"])  # As before: an unreadable id is dictation.
        carbon.GetEventParameter.side_effect = RuntimeError("boom")
        with patch.object(self.menubar, "log") as log:
            self.assertEqual(key.handler(None, None, None), 0)
        log.exception.assert_called()

    def test_a_press_is_acknowledged_and_the_work_is_deferred_to_the_run_loop(self):
        me = self.controller(
            shortcut=hotkeys.DEFAULT,
            history=hotkeys.DEFAULT_HISTORY,
            performSelector_withObject_afterDelay_inModes_=MagicMock(),
        )
        self.menubar.Controller.hotkey_pressed(me, "dictation")
        selector = me.performSelector_withObject_afterDelay_inModes_.call_args.args[0]
        self.assertEqual(selector, "dictationRequested:")
        self.menubar.Controller.hotkey_pressed(me, "history")
        self.assertEqual(
            me.performSelector_withObject_afterDelay_inModes_.call_args.args[0],
            "historyRequested:",
        )
        for kind, shortcut in (
            ("dictation", hotkeys.DEFAULT),
            ("history", hotkeys.DEFAULT_HISTORY),
        ):
            self.assertTrue(hotkeys.heard_recently(me.paths, kind, shortcut))

    def test_the_deferred_work_shows_the_alert_only_after_the_callback_returned(self):
        me = self.controller(pressed=MagicMock(), open_window=MagicMock())
        self.menubar.Controller.dictationRequested_(me, None)
        me.pressed.assert_called_once()
        self.menubar.Controller.historyRequested_(me, None)
        me.open_window.assert_called_once_with("--clipboard")

    def test_registration_results_are_shared_with_the_window_with_the_reason(self):
        me = self.controller(hotkey=MagicMock())
        me.hotkey.reason.return_value = "It is taken."
        self.menubar.Controller.record_registration(me, "history", hotkeys.DEFAULT_HISTORY, False)
        self.assertFalse(hotkeys.shortcut_working(me.paths, hotkeys.HISTORY_STATUS))
        self.assertEqual(hotkeys.shortcut_message(me.paths, hotkeys.HISTORY_STATUS), "It is taken.")
        self.menubar.Controller.record_registration(me, "history", hotkeys.DEFAULT_HISTORY, True)
        self.assertTrue(hotkeys.shortcut_working(me.paths, hotkeys.HISTORY_STATUS))
        self.assertEqual(hotkeys.shortcut_message(me.paths, hotkeys.HISTORY_STATUS), "")

    def follower(self, recording, dictation=True, chosen=None):
        me = self.controller(
            capturing=False,
            history=hotkeys.DEFAULT_HISTORY,
            shortcut=hotkeys.DEFAULT,
            hotkey=MagicMock(),
            hotkey_ok=True,
            clip=MagicMock(),
            preferences=MagicMock(),
            apply_shortcut=MagicMock(),
            apply_features=MagicMock(),
            record_registration=MagicMock(),
            window_is_recording=MagicMock(return_value=recording),
        )
        me.hotkey.register.return_value = True
        me.clip.features.return_value = hotkeys.Features(dictation, True)
        me.preferences.shortcut.return_value = chosen or hotkeys.DEFAULT
        return me

    def test_either_recorder_pauses_both_shortcuts_and_the_history_one_returns(self):
        me = self.follower(True)
        self.menubar.Controller.follow_window_shortcut(me)
        me.hotkey.unregister.assert_any_call()
        me.hotkey.unregister.assert_any_call(self.menubar.HISTORY_ID)
        self.assertIsNone(me.history)
        self.assertTrue(me.capturing)
        me.window_is_recording.return_value = False
        self.menubar.Controller.follow_window_shortcut(me)
        self.assertFalse(me.capturing)
        me.apply_features.assert_called_once()  # Registers the history shortcut's choice.
        me.record_registration.assert_called_once_with("dictation", me.shortcut, True)

    def test_a_new_dictation_shortcut_is_applied_when_recording_ends(self):
        chosen = hotkeys.Shortcut(("ctrl", "alt"), "K")
        me = self.follower(False, chosen=chosen)
        me.capturing = True
        self.menubar.Controller.follow_window_shortcut(me)
        me.apply_shortcut.assert_called_once_with(chosen)

    def test_the_history_shortcut_is_not_registered_while_recording(self):
        me = self.follower(True)
        me.capturing = True
        me.history = None
        me.dictation_registered = True
        me.clip.history_shortcut.return_value = hotkeys.DEFAULT_HISTORY
        me.popover = MagicMock()
        me.popover.isShown.return_value = False
        self.menubar.Controller.apply_features(me)
        me.hotkey.register.assert_not_called()

    def test_a_flag_left_by_a_closed_window_is_cleared(self):
        me = self.controller(capturing=False)
        for name in hotkeys.CAPTURE_FLAGS.values():
            (me.paths.runtime / name).write_text("capturing")
        self.assertFalse(self.menubar.Controller.window_is_recording(me))
        self.assertFalse(
            any((me.paths.runtime / n).exists() for n in hotkeys.CAPTURE_FLAGS.values())
        )

    def test_a_flag_from_an_open_window_is_honoured(self):
        import os

        me = self.controller(capturing=False)
        (me.paths.runtime / "history-shortcut-capture").write_text("capturing")
        window = self.menubar.desktop.lock(me.paths.runtime / "app.lock")
        self.addCleanup(os.close, window)
        self.assertTrue(self.menubar.Controller.window_is_recording(me))


if __name__ == "__main__":
    unittest.main()
