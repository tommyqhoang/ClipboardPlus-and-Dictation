from __future__ import annotations

import json
import os
import plistlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import dictation as d
import hotkeys


class HotkeyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        environment = patch.dict(
            os.environ,
            {
                "XDG_CONFIG_HOME": str(self.folder / "config"),
                "XDG_RUNTIME_DIR": str(self.folder / "runtime"),
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.preferences = hotkeys.Preferences(d.Paths())

    def test_same_shortcut_on_every_platform(self):
        shortcut = hotkeys.DEFAULT
        self.assertEqual(shortcut.label("macos"), "⇧⌘D")
        self.assertEqual(shortcut.label("windows"), "Win+Shift+D")
        self.assertEqual(shortcut.label("linux"), "Super+Shift+D")
        self.assertEqual(hotkeys.Shortcut(("cmd",), "F5").label("windows"), "Win+F5")
        self.assertEqual(hotkeys.Shortcut(("cmd",), "F5").label("linux"), "Super+F5")
        self.assertEqual(shortcut.carbon_modifiers(), 0x200 | 0x100)
        self.assertEqual(shortcut.mac_key_code(), 2)
        self.assertEqual(shortcut.windows_key(), ord("D"))
        self.assertEqual(shortcut.windows_modifiers(), 0x4 | 0x8 | hotkeys.MOD_NOREPEAT)
        self.assertEqual(shortcut.gnome(), "<Shift><Super>d")
        space = hotkeys.Shortcut(("alt",), "Space")
        self.assertEqual((space.windows_key(), space.gnome()), (0x20, "<Alt>space"))
        f12 = hotkeys.Shortcut((), "F12")
        self.assertEqual((f12.windows_key(), f12.gnome(), f12.mac_key_code()), (0x7B, "F12", 111))
        self.assertEqual(len(hotkeys.MAC_KEY_CODES), len(hotkeys.KEYS))
        self.assertEqual(len(set(hotkeys.PRESETS)), len(hotkeys.PRESETS))
        for preset in hotkeys.PRESETS:
            self.assertEqual(preset.problem(), "")

    def test_rejects_shortcuts_that_capture_typing(self):
        self.assertIn("Include", hotkeys.Shortcut((), "D").problem())
        self.assertIn("Include", hotkeys.Shortcut(("shift",), "D").problem())
        self.assertIn("Most apps", hotkeys.Shortcut(("cmd",), "V").problem())
        self.assertIn("Most apps", hotkeys.Shortcut(("ctrl",), "C").problem())
        self.assertIn("letter", hotkeys.Shortcut(("ctrl",), "Home").problem())
        self.assertEqual(hotkeys.Shortcut((), "F5").problem(), "")
        self.assertEqual(hotkeys.Shortcut(("cmd",), "D").problem(), "")

    def test_captured_keys(self):
        flags = hotkeys.EVENT_FLAGS["shift"] | hotkeys.EVENT_FLAGS["cmd"]
        self.assertEqual(hotkeys.from_mac_event(2, flags, "d"), hotkeys.DEFAULT)
        self.assertEqual(hotkeys.from_mac_event(49, flags, " ").key, "Space")
        # Every modifier survives, whatever order the event flags are read in.
        every = sum(hotkeys.EVENT_FLAGS.values())
        self.assertEqual(hotkeys.from_mac_event(2, every, "d").modifiers, hotkeys.MODIFIER_ORDER)
        self.assertEqual(hotkeys.from_mac_event(200, 0, "é").key, "É")
        self.assertIsNone(hotkeys.from_tk("Control_L", set()))
        self.assertEqual(hotkeys.from_tk("d", {"cmd", "shift"}), hotkeys.DEFAULT)
        self.assertEqual(hotkeys.from_tk("space", {"alt"}), hotkeys.PRESETS[1])
        self.assertEqual(hotkeys.from_tk("F9", set()).key, "F9")

    def test_preferences_round_trip_and_fallbacks(self):
        self.assertEqual(self.preferences.shortcut(), hotkeys.DEFAULT)
        self.assertEqual(self.preferences.stamp(), 0.0)
        self.assertTrue(self.preferences.open_at_login())
        custom = hotkeys.Shortcut(("alt",), "Space")
        self.preferences.save(shortcut=custom)
        self.preferences.save(open_at_login=False)
        self.assertEqual(self.preferences.shortcut(), custom)
        self.assertFalse(self.preferences.open_at_login())
        self.assertGreater(self.preferences.stamp(), 0)
        for broken in (
            '{"shortcut": "nope"}',
            '{"shortcut": {"modifiers": "ctrl", "key": "D"}}',
            '{"shortcut": {"modifiers": [], "key": "D"}}',
            "not json",
        ):
            self.preferences.path.write_text(broken)
            self.assertEqual(self.preferences.shortcut(), hotkeys.DEFAULT)
            self.assertTrue(self.preferences.open_at_login())
        # Files written by the first macOS version also carried key_code.
        self.preferences.path.write_text(
            '{"shortcut": {"key_code": 49, "modifiers": ["alt"], "key": "Space"}}'
        )
        self.assertEqual(self.preferences.shortcut(), custom)

    def test_login_items_per_platform(self):
        command = ["/usr/bin/open", str(self.folder / "Whisper Dictation.app")]
        hotkeys.set_login_item(True, command, self.folder, "macos")
        agent = plistlib.loads(hotkeys.agent_path(self.folder).read_bytes())
        self.assertEqual(agent["Label"], hotkeys.AGENT_LABEL)
        self.assertEqual(agent["ProgramArguments"], command)
        self.assertTrue(agent["RunAtLoad"])
        hotkeys.set_login_item(False, command, self.folder, "macos")
        self.assertFalse(hotkeys.agent_path(self.folder).exists())
        tray = ["/venv/bin/python", "/app with space/tray.py"]
        hotkeys.set_login_item(True, tray, self.folder, "linux")
        entry = hotkeys.autostart_path(self.folder).read_text()
        self.assertIn("Exec=/venv/bin/python '/app with space/tray.py'", entry)
        hotkeys.set_login_item(False, tray, self.folder, "linux")
        self.assertFalse(hotkeys.autostart_path(self.folder).exists())
        registry = MagicMock()
        key = registry.CreateKey.return_value.__enter__.return_value
        hotkeys.set_login_item(
            True, ["C:/py/pythonw.exe", "C:/a b/tray.py"], None, "windows", registry
        )
        registry.SetValueEx.assert_called_once_with(
            key, "WhisperDictation", 0, registry.REG_SZ, 'C:/py/pythonw.exe "C:/a b/tray.py"'
        )
        registry.DeleteValue.side_effect = FileNotFoundError
        hotkeys.set_login_item(False, [], None, "windows", registry)
        registry.DeleteValue.assert_called_once_with(key, "WhisperDictation")

    def test_gnome_shortcut_binds_command(self):
        calls = []

        def run(args, **_):
            calls.append(args[1:])
            output = "['/other/']" if args[1] == "get" else ""
            return Mock(returncode=0, stdout=output, stderr="")

        with patch.object(hotkeys.shutil, "which", return_value="/usr/bin/gsettings"):
            self.assertTrue(hotkeys.gnome_shortcut(hotkeys.DEFAULT, Path("/bin/toggle"), run))
            self.assertIn(
                ["set", *hotkeys.GNOME_LIST, f"['/other/', {hotkeys.GNOME_PATH!r}]"], calls
            )
            self.assertEqual(calls[-1][-2:], ["binding", "<Shift><Super>d"])
            failing = Mock(return_value=Mock(returncode=1, stdout="", stderr="no schema"))
            self.assertFalse(hotkeys.gnome_shortcut(hotkeys.DEFAULT, Path("/t"), failing))
        with patch.object(hotkeys.shutil, "which", return_value=None):
            self.assertFalse(hotkeys.gnome_shortcut(hotkeys.DEFAULT, Path("/t")))

    def test_gnome_shortcut_pauses_and_removes_only_its_own_binding(self):
        calls = []
        state = {"command": "'/bin/toggle'", "list": f"['/other/', {hotkeys.GNOME_PATH!r}]"}

        def run(args, **_):
            calls.append(args[1:])
            output = state["list"] if args[-1] == "custom-keybindings" else state["command"]
            return Mock(returncode=0, stdout=output if args[1] == "get" else "", stderr="")

        with patch.object(hotkeys.shutil, "which", return_value="/usr/bin/gsettings"):
            # Pausing (while a new shortcut is recorded) clears the keys but keeps the entry.
            self.assertTrue(hotkeys.gnome_shortcut(None, Path("/bin/toggle"), run))
            self.assertEqual(calls[-1][-2:], ["binding", ""])
            calls.clear()
            hotkeys.gnome_remove(Path("/other/toggle"), run)  # Someone else's command.
            self.assertFalse(any(call[0] in ("set", "reset-recursively") for call in calls))
            hotkeys.gnome_remove(Path("/bin/toggle"), run)
            self.assertIn(["set", *hotkeys.GNOME_LIST, "['/other/']"], calls)
            self.assertEqual(calls[-1][0], "reset-recursively")

    def write_raw(self, text: str) -> None:
        """Put arbitrary text in the preferences file, as a hand edit or crash would."""
        self.preferences.path.parent.mkdir(parents=True, exist_ok=True)
        self.preferences.path.write_text(text)

    def test_features_default_to_dictation_only_and_round_trip(self):
        self.assertEqual(self.preferences.features(), hotkeys.Features(True, False))
        for features in (hotkeys.Features(False, True), hotkeys.Features(True, True)):
            self.preferences.save(features=features)
            self.assertEqual(self.preferences.features(), features)

    def test_features_ignore_corrupt_values_and_never_turn_everything_off(self):
        for raw in ("nonsense", {"dictation": "yes"}, {"dictation": False, "clipboard": False}, []):
            self.write_raw(json.dumps({"features": raw}))
            self.assertEqual(self.preferences.features(), hotkeys.Features(True, False), raw)
        self.write_raw("{not json")
        self.assertEqual(self.preferences.features(), hotkeys.Features(True, False))

    def test_saving_features_keeps_the_other_preferences(self):
        self.preferences.save(shortcut=hotkeys.PRESETS[1], open_at_login=False)
        self.preferences.save(features=hotkeys.Features(True, True))
        self.assertEqual(self.preferences.shortcut(), hotkeys.PRESETS[1])
        self.assertFalse(self.preferences.open_at_login())

    def test_clipboard_settings_defaults_round_trip_and_clamping(self):
        self.assertEqual(self.preferences.clipboard(), hotkeys.ClipboardSettings())
        settings = hotkeys.ClipboardSettings(
            keep_items=200, keep_days=7, images=False, paused_until=-1
        )
        self.preferences.save(clipboard=settings)
        self.assertEqual(self.preferences.clipboard(), settings)
        self.preferences.save(clipboard=hotkeys.ClipboardSettings(keep_items=1, keep_days=0))
        self.assertEqual(self.preferences.clipboard().keep_items, 50)
        self.assertEqual(self.preferences.clipboard().keep_days, 1)
        self.preferences.save(
            clipboard=hotkeys.ClipboardSettings(keep_items=10**9, keep_days=10**9)
        )
        self.assertEqual(self.preferences.clipboard().keep_items, 10000)
        self.assertEqual(self.preferences.clipboard().keep_days, 3650)

    def test_clipboard_settings_ignore_wrong_types(self):
        self.write_raw(
            json.dumps({"clipboard": {"keep_items": "many", "keep_days": None, "images": "no"}})
        )
        self.assertEqual(self.preferences.clipboard(), hotkeys.ClipboardSettings())

    def test_pause_state(self):
        settings = hotkeys.ClipboardSettings(paused_until=-1)
        self.assertTrue(settings.paused(now=100.0))  # Until resumed.
        self.assertTrue(hotkeys.ClipboardSettings(paused_until=200.0).paused(now=100.0))
        self.assertFalse(hotkeys.ClipboardSettings(paused_until=200.0).paused(now=300.0))
        self.assertFalse(hotkeys.ClipboardSettings().paused(now=100.0))

    def test_helpers(self):
        with patch("webbrowser.open") as browser:
            hotkeys.open_link()
        browser.assert_called_once_with("https://clipboardplus.apercallc.com")
        self.assertTrue(Path(hotkeys.python_for_gui()).name.startswith("python"))


if __name__ == "__main__":
    unittest.main()
