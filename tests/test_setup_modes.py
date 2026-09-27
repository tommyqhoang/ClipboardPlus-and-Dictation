"""First-run feature choice and the Settings page for each mode."""

from __future__ import annotations

import gc
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import clipstore
import hotkeys
from test_app import ServiceCase


class ModeCase(ServiceCase):
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
        super().setUp()
        import app

        self.gui = app
        self.root = app.tk.Tk()
        self.root.withdraw()
        self.window = app.App(self.root, self.service)  # Not set up yet: the welcome page.
        self.addCleanup(self.close)
        self.prefs = hotkeys.Preferences(self.paths)

    def close(self):
        if self.window.page != "closed":
            self.window.destroy()
        self.window = self.root = None
        gc.collect()

    def texts(self):
        found, stack = [], [self.window.frame]
        while stack:
            widget = stack.pop()
            stack.extend(widget.winfo_children())
            if widget.winfo_class() in ("TLabel", "TRadiobutton", "TCheckbutton"):
                found.append(str(widget.cget("text")))
        return found

    def press(self, label):
        stack = [self.window.frame, self.window.bar_actions]
        while stack:
            widget = stack.pop()
            stack.extend(widget.winfo_children())
            if widget.winfo_class() == "TButton" and widget.cget("text") == label:
                widget.invoke()
                return
        self.fail(f"No button {label!r} on the {self.window.page} page")


class SetupFlowTests(ModeCase):
    def test_get_started_asks_what_the_user_wants_to_use(self):
        self.assertEqual(self.window.page, "welcome")
        self.window.begin_setup()
        self.assertEqual(self.window.page, "features")
        texts = " ".join(self.texts())
        for option in ("Dictation", "Clipboard history", "Both"):
            self.assertIn(option, texts)

    def test_clipboard_only_skips_dictation_setup_entirely(self):
        self.window.after_features("clipboard")
        self.assertEqual(self.window.page, "clipboard-optin")
        self.press("Turn on")
        self.assertEqual(self.prefs.features(), hotkeys.Features(False, True))
        self.assertEqual(self.window.page, "tutorial")
        joined = " ".join(self.texts())
        self.assertNotIn("Microphone", joined)
        self.assertIn("Clipboard History", joined)
        self.service.complete()
        self.assertTrue(self.service.completed())  # No speech model needed.

    def test_both_runs_dictation_setup_then_the_optin(self):
        self.window.after_features("both")
        self.assertEqual(self.window.page, "settings")
        self.window.after_dictation_setup()
        self.assertEqual(self.window.page, "clipboard-optin")
        self.press("Turn on")
        self.assertEqual(self.prefs.features(), hotkeys.Features(True, True))
        self.assertEqual(self.window.page, "tutorial")

    def test_every_setup_screen_counts_the_same_steps(self):
        self.window.after_features("both")
        self.assertEqual(self.window.step.get(), "Step 2 of 4")
        # The feature choice was just made; offering it again here stranded setup.
        self.assertNotIn("What you use", self.texts())
        self.window.after_dictation_setup()
        self.assertEqual(self.window.step.get(), "Step 3 of 4")
        self.press("Turn on")
        self.assertEqual(self.window.step.get(), "Step 4 of 4")

    def test_a_clipboard_only_setup_counts_three_steps(self):
        self.window.after_features("clipboard")
        self.assertEqual(self.window.step.get(), "Step 2 of 3")
        self.press("Turn on")
        self.assertEqual(self.window.step.get(), "Step 3 of 3")

    def test_dictation_only_has_no_clipboard_step(self):
        self.window.after_features("dictation")
        self.assertEqual(self.window.page, "settings")
        self.window.after_dictation_setup()
        self.assertEqual(self.window.page, "tutorial")
        self.assertEqual(self.prefs.features(), hotkeys.Features(True, False))

    def test_not_now_with_both_keeps_dictation_and_leaves_clipboard_off(self):
        self.window.after_features("both")
        self.window.after_dictation_setup()
        self.press("Not now")
        self.assertEqual(self.prefs.features(), hotkeys.Features(True, False))
        self.assertEqual(self.window.page, "tutorial")

    def test_not_now_in_a_clipboard_only_setup_goes_back_to_the_choice(self):
        self.window.after_features("clipboard")
        self.press("Not now")
        self.assertEqual(self.window.page, "features")
        self.assertTrue(any("at least one" in t.lower() for t in self.texts()))


class SettingsTests(ModeCase):
    def open_settings(self):
        with (
            patch.object(self.service, "completed", return_value=True),
            patch.object(self.service, "microphones", return_value=[]),
        ):
            self.window.settings()

    def test_a_clipboard_only_user_sees_clipboard_options_not_dictation_ones(self):
        self.prefs.save(features=hotkeys.Features(False, True))
        self.open_settings()
        joined = " ".join(self.texts())
        self.assertIn("Keep up to this many items", joined)
        self.assertNotIn("Microphone", joined)

    def test_a_dictation_only_user_is_not_shown_clipboard_options(self):
        self.prefs.save(features=hotkeys.Features(True, False))
        self.open_settings()
        joined = " ".join(self.texts())
        self.assertIn("Microphone", joined)
        self.assertNotIn("Keep the newest", joined)

    def test_changing_the_features_applies_at_once_and_redraws(self):
        self.prefs.save(features=hotkeys.Features(True, True))
        self.open_settings()
        with (
            patch.object(self.service, "completed", return_value=True),
            patch.object(self.service, "microphones", return_value=[]),
        ):
            self.window.apply_mode("clipboard")
        self.assertEqual(self.prefs.features(), hotkeys.Features(False, True))
        self.assertNotIn("Microphone", " ".join(self.texts()))

    def test_clipboard_options_are_saved(self):
        self.prefs.save(features=hotkeys.Features(False, True))
        with patch.object(self.gui.messagebox, "askyesno", return_value=True):
            self.window.save_clipboard_options(keep_items=500, keep_days=7, images=False)
        saved = self.prefs.clipboard()
        self.assertEqual((saved.keep_items, saved.keep_days, saved.images), (500, 7, False))

    def test_cancel_retention_reduction_preserves_preferences(self):
        previous = self.prefs.clipboard()
        with patch.object(self.gui.messagebox, "askyesno", return_value=False) as ask:
            self.assertFalse(self.window.save_clipboard_options(100, 7, False))
        ask.assert_called_once()
        self.assertEqual(self.prefs.clipboard(), previous)

    def test_setup_privacy_switch_persists_immediately(self):
        self.window.choose_features()
        self.assertIn("Privacy", self.texts())
        self.assertFalse(self.window.share_usage.get())  # Nothing is shared by default.
        widgets = [self.window.frame]
        while widgets:
            widget = widgets.pop()
            if (
                widget.winfo_class() == "TCheckbutton"
                and widget.cget("text") == "Share anonymous crash reports and usage statistics"
            ):
                toggle = widget
                break
            widgets.extend(widget.winfo_children())
        else:
            self.fail("Privacy switch was not rendered")
        toggle.invoke()
        self.assertTrue(self.prefs.share_usage())
        toggle.invoke()
        self.assertFalse(self.prefs.share_usage())

    def test_deleting_all_clipboard_data_asks_first_and_never_touches_the_cloud(self):
        store = clipstore.Store(self.paths.clipboard)
        self.addCleanup(store.close)
        item = store.add_text("private", now=1.0)
        store.link(item.id, "cloud-1", False)
        with patch.object(self.gui.messagebox, "askyesno", return_value=False):
            self.window.delete_clipboard_data()
        self.assertEqual(store.count(), 1)
        with patch.object(self.gui.messagebox, "askyesno", return_value=True):
            self.window.delete_clipboard_data()
        self.assertEqual(store.count(), 0)
        self.assertEqual(store.tombstones(), [])


if __name__ == "__main__":
    unittest.main()
