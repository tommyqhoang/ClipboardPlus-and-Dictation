"""Settings, Home and welcome pages on a real Tk (needs a display: tests/with-xvfb.sh)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import app_settings
import dictation as d
import hotkeys
from test_clipui import PageCase


class PureTests(unittest.TestCase):
    def test_languages_round_trip(self):
        for code in ("en", "auto"):
            self.assertEqual(app_settings.language_code(app_settings.language_label(code)), code)

    def test_provider_is_matched_by_url_or_falls_back(self):
        providers = {"A": ("https://a/x", "m"), "B": ("https://b/x", "m"), "Other": ("", "")}
        self.assertEqual(app_settings.provider_name("https://b/x", True, providers), "B")
        self.assertEqual(app_settings.provider_name("https://zzz", True, providers), "Other")
        self.assertEqual(app_settings.provider_name("", False, providers), "A")

    def test_home_text_for_each_shortcut_state(self):
        conflict = hotkeys.Conflict("the desktop’s “close” shortcut")
        title, text = app_settings.home_text("Ctrl+Shift+D", conflict, False, "linux")
        self.assertIn("taken by something else", title)
        self.assertIn("The desktop’s", text)
        title, text = app_settings.home_text("Ctrl+Shift+D", None, True, "macos")
        self.assertEqual(title, "Press Ctrl+Shift+D to dictate")
        self.assertIn("Command", text)
        title, text = app_settings.home_text("Ctrl+Shift+D", None, False, "linux")
        self.assertEqual(title, "Set up your shortcut")
        self.assertIn("dictate-toggle", text)
        title, text = app_settings.home_text("Ctrl+Shift+D", None, False, "linux", "Add this line.")
        self.assertIn("Add this line.", text)
        self.assertNotIn("dictate-toggle", text)
        self.assertEqual(app_settings.home_text("F9", None, False, "windows")[0], "F9 is taken")


class HomeAndSettingsTests(PageCase):
    def labels(self):
        return " | ".join(self.texts())

    def test_home_shows_why_the_shortcut_could_not_be_set(self):
        hotkeys.record_status(self.paths, False)
        hotkeys.record_message(self.paths, "Sway said no. Add to ~/.config/sway/config: bindsym x")
        with (
            patch.object(self.service, "completed", return_value=True),
            # Only Linux desktops show why a shortcut could not be set.
            patch("desktop.platform_name", return_value="linux"),
        ):
            self.window.home()
        self.assertIn("Sway said no", self.labels())

    def test_settings_offers_sound_cues_and_saves_the_choice(self):
        with patch.object(self.service, "completed", return_value=True):
            self.window.settings()
        self.assertIn("Sound cues", self.labels())
        combos = []
        stack = [self.window.root]
        while stack:
            widget = stack.pop()
            stack.extend(widget.winfo_children())
            if widget.winfo_class() == "TCombobox" and "Errors only" in str(widget.cget("values")):
                combos.append(widget)
        self.assertEqual(len(combos), 1)
        with patch("app.cues.play") as play:
            combos[0].set("Start, stop and errors")
            combos[0].event_generate("<<ComboboxSelected>>")
        self.assertEqual(d.read_json(self.paths.config)["sounds"], "all")
        play.assert_called_once_with("error", "all")

    def test_the_privacy_switch_uses_the_consent_api_when_present(self):
        record, has = MagicMock(), MagicMock(return_value=False)
        with (
            patch.object(sys.modules["telemetry"], "set_consent", record, create=True),
            patch.object(sys.modules["telemetry"], "has_consent", has, create=True),
            patch.object(self.service, "completed", return_value=True),
        ):
            self.window.settings()
            self.assertFalse(self.window.share_usage.get())
            self.window.share_usage.set(True)
            # The checkbutton's command saves what the box now says.
            stack = [self.window.root]
            while stack:
                widget = stack.pop()
                stack.extend(widget.winfo_children())
                if widget.winfo_class() == "TCheckbutton" and "usage statistics" in str(
                    widget.cget("text")
                ):
                    widget.invoke()  # Toggles it back off, then saves.
                    break
        record.assert_called_with(False)


class WelcomeTests(PageCase):
    def test_the_first_screen_asks_for_consent_and_defaults_to_no(self):
        record = MagicMock()
        with (
            patch.object(sys.modules["telemetry"], "set_consent", record, create=True),
            patch.object(self.service, "completed", return_value=False),
        ):
            self.window.welcome()
            self.assertFalse(self.window.welcome_consent.get())
            self.window.begin_setup()
        record.assert_called_once_with(False)
        with (
            patch.object(sys.modules["telemetry"], "set_consent", record, create=True),
            patch.object(self.service, "completed", return_value=False),
        ):
            self.window.welcome()
            self.window.welcome_consent.set(True)
            self.window.begin_setup()
        record.assert_called_with(True)

    def test_without_the_consent_api_getting_started_still_works(self):
        with patch.object(self.service, "completed", return_value=False):
            self.window.welcome()
            with patch.object(sys.modules["telemetry"], "set_consent", None, create=True):
                self.window.begin_setup()
        self.assertEqual(self.window.page, "features")


if __name__ == "__main__":
    unittest.main()
