"""Tray behavior with a stand-in for pystray; no desktop session needed."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import dictation as d
import hotkeys
import tray


class Item:
    def __init__(self, text, action, **options):
        self.text, self.action, self.options = text, action, options


class Menu(tuple):
    SEPARATOR = "separator"

    def __new__(cls, *items):
        return super().__new__(cls, items)


class Icon:
    def __init__(self, name, icon, title, menu):
        self.icon, self.title, self.menu = icon, title, menu
        self.updates = 0
        self.notifications = []
        self.stopped = False

    def update_menu(self):
        self.updates += 1

    def notify(self, message, title):
        self.notifications.append(message)

    def stop(self):
        self.stopped = True


class FakeHotKey:
    def __init__(self, *_):
        self.registered = []
        self.refuse = set()

    def register(self, shortcut):
        self.registered.append(shortcut)
        return shortcut not in self.refuse


class TrayTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        folder = Path(temporary.name)
        environment = patch.dict(
            os.environ,
            {
                "XDG_CONFIG_HOME": str(folder / "config"),
                "XDG_CACHE_HOME": str(folder / "cache"),
                "XDG_RUNTIME_DIR": str(folder / "runtime"),
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.paths = d.Paths()
        d.private_dir(self.paths.runtime)
        pystray = SimpleNamespace(MenuItem=Item, Menu=Menu, Icon=Icon)
        image = SimpleNamespace(open=lambda path: Path(path).name)
        self.tray = tray.Tray(pystray, image)
        self.tray.hotkey = FakeHotKey()
        popen = patch.object(tray.subprocess, "Popen")
        self.popen = popen.start()
        self.addCleanup(popen.stop)

    def items(self, menu=None):
        for entry in menu if menu is not None else self.tray.icon.menu:
            if isinstance(entry, Item):
                yield entry
                if isinstance(entry.action, Menu):
                    yield from self.items(entry.action)

    def item(self, text):
        for entry in self.items():
            label = entry.text(None) if callable(entry.text) else entry.text
            if label.startswith(text):
                return entry
        raise AssertionError(text)

    def test_menu_matches_macos_and_uses_shared_icon(self):
        labels = [
            entry.text if isinstance(entry.text, str) else "dynamic" for entry in self.items()
        ]
        for expected in (
            "Cancel Recording",
            "Copy Last Transcript",
            "Record New Shortcut…",
            "Open at Login",
            "Clipboard History (Clipboard+)…",
            "Settings…",
            "Quit Whisper Dictation",
        ):
            self.assertIn(expected, labels)
        self.assertEqual(self.tray.icon.icon, "whisper-dictation.png")
        with patch("webbrowser.open") as browser:
            self.item("Clipboard History").action()
        browser.assert_called_once_with(hotkeys.CLIPBOARD_PLUS)

    def test_shortcut_runs_engine_or_opens_setup(self):
        with patch.object(self.tray.service, "ready", return_value=False):
            self.item("Start Dictation").action()
        self.assertIn("--setup", self.popen.call_args.args[0])
        with patch.object(self.tray.service, "ready", return_value=True):
            self.tray.pressed()
        self.assertTrue(self.popen.call_args.args[0][-1].endswith("dictation.py"))
        self.item("Cancel Recording").action()
        self.assertEqual(self.popen.call_args.args[0][-1], "--cancel")
        self.item("Copy Last Transcript").action()
        self.assertEqual(self.popen.call_args.args[0][-1], "--copy-last")
        self.item("Record New Shortcut").action()
        self.assertEqual(self.popen.call_args.args[0][-1], "--shortcut")
        self.item("Settings").action()
        self.assertEqual(self.popen.call_args.args[0][-1], "--settings")

    def test_presets_and_taken_shortcuts(self):
        preset = hotkeys.PRESETS[1]
        self.item(preset.label()).action()
        self.assertEqual(self.tray.shortcut, preset)
        self.assertEqual(hotkeys.Preferences(self.paths).shortcut(), preset)
        self.assertTrue(self.item(preset.label()).options["checked"](None))
        taken = hotkeys.PRESETS[2]
        self.tray.hotkey.refuse.add(taken)
        self.tray.apply(taken)
        self.assertEqual(self.tray.shortcut, preset)
        self.assertIn("already in use", self.tray.icon.notifications[-1])

    def test_tick_tracks_recording_and_shortcut_window(self):
        with patch.object(self.tray.service, "ready", return_value=True):
            self.tray.hotkey_ok = True
            self.tray.tick()
            self.assertIn("anywhere to dictate", self.tray.status_text())
            self.assertIn("Start Dictation", self.tray.toggle_text())
            d.atomic(self.paths.state, '{"phase":"recording","started_at":0}')
            with patch.object(d, "busy", return_value=True):
                self.tray.tick()
            self.assertEqual(self.tray.icon.icon, "tray-recording.png")
            self.assertIn("recording", self.tray.icon.title)
            self.assertEqual(self.tray.status_text(), "Recording…")
            self.assertIn("Stop and Transcribe", self.tray.toggle_text())
            self.assertTrue(self.item("Cancel Recording").options["enabled"](None))
            d.atomic(self.paths.state, '{"phase":"transcribing"}')
            with patch.object(d, "busy", return_value=True):
                self.tray.tick()
            self.assertEqual(self.tray.status_text(), "Transcribing…")
            self.assertEqual(self.tray.toggle_text(), "Transcribing…")
            self.tray.hotkey_ok = False
            d.atomic(self.paths.state, '{"phase":"idle"}')
            self.tray.tick()
            self.assertIn("keyboard settings", self.tray.status_text())
        with patch.object(self.tray.service, "ready", return_value=False):
            self.assertIn("Finish setup", self.tray.status_text())
        # The shortcut window pauses the old shortcut, then applies the new one.
        window = tray.desktop.lock(self.paths.runtime / "app.lock")  # The window is open.
        self.addCleanup(os.close, window)
        capture = self.paths.runtime / "shortcut-capture"
        capture.write_text("capturing")
        self.tray.tick()
        self.assertIsNone(self.tray.hotkey.registered[-1])
        capture.unlink()
        self.tray.tick()
        self.assertEqual(self.tray.hotkey.registered[-1], self.tray.shortcut)
        capture.write_text("capturing")
        self.tray.tick()
        hotkeys.Preferences(self.paths).save(shortcut=hotkeys.PRESETS[3])
        capture.unlink()
        self.tray.tick()
        self.assertEqual(self.tray.shortcut, hotkeys.PRESETS[3])
        with patch.object(tray.workflow, "snapshot", side_effect=OSError):
            self.tray.tick()
        (self.paths.runtime / "menubar-quit").write_text("quit")
        self.tray.tick()
        self.assertTrue(self.tray.icon.stopped)

    def test_login_item_and_startup(self):
        with patch.object(hotkeys, "set_login_item") as login:
            self.item("Open at Login").action()
            login.assert_called_once()
            self.assertFalse(login.call_args.args[0])
            self.assertTrue(login.call_args.args[1][-1].endswith("tray.py"))
        self.assertFalse(self.item("Open at Login").options["checked"](None))
        icon = Mock()
        with (
            patch.object(tray.desktop, "platform_name", return_value="linux"),
            patch.object(tray, "GnomeHotKey", FakeHotKey),
            patch.object(hotkeys, "set_login_item"),
            patch.object(self.tray, "tick", side_effect=lambda: self.tray.quit()),
        ):
            self.tray.started(icon)
        self.assertTrue(icon.visible)
        self.assertTrue(self.tray.hotkey_ok)
        self.assertIn("--setup", self.popen.call_args.args[0])
        self.tray.icon.notify = Mock(side_effect=NotImplementedError)
        self.tray.notify("ignored when unsupported")

    def test_gnome_hotkey_binds_toggle_command(self):
        with patch.object(hotkeys, "gnome_shortcut", return_value=True) as bind:
            gnome = tray.GnomeHotKey(lambda: None)
            self.assertTrue(gnome.register(hotkeys.DEFAULT))
            self.assertTrue(gnome.register(None))
        self.assertEqual(bind.call_args.args[1].name, "dictate-toggle")

    def test_main_refuses_macos(self):
        with patch.object(tray.desktop, "platform_name", return_value="macos"):
            self.assertEqual(tray.main(), 1)

    def test_recording_does_not_redraw_the_icon_or_menu_every_second(self):
        # AppIndicator rewrites the icon file and builds a new GTK menu on each update,
        # which flickers the top bar and closes an open menu.
        def recording(seconds):
            return {"phase": "recording", "active": True, "elapsed_seconds": seconds}

        with (
            patch.object(self.tray.service, "ready", return_value=True),
            patch.object(tray.desktop, "platform_name", return_value="linux"),
        ):
            self.tray.hotkey_ok = True
            with patch.object(tray.workflow, "snapshot", return_value=recording(1)):
                self.tray.tick()
            updates = self.tray.icon.updates
            for seconds in range(2, 8):
                with patch.object(tray.workflow, "snapshot", return_value=recording(seconds)):
                    self.tray.tick()
            self.assertEqual(self.tray.icon.updates, updates)

    def test_capture_left_by_a_closed_window_does_not_disable_the_shortcut(self):
        (self.paths.runtime / "shortcut-capture").write_text("capturing")
        self.tray.tick()  # No window holds app.lock: the flag is stale.
        self.assertFalse((self.paths.runtime / "shortcut-capture").exists())
        self.assertNotIn(None, self.tray.hotkey.registered)

    def test_registration_result_is_shared_and_explained(self):
        self.tray.hotkey.refuse.add(hotkeys.PRESETS[1])
        with (
            patch.object(tray.desktop, "platform_name", return_value="linux"),
            patch.object(tray.shutil, "which", return_value=None),
        ):
            self.tray.apply(hotkeys.PRESETS[1])
        self.assertIn("keyboard settings", self.tray.icon.notifications[-1])
        self.tray.apply(hotkeys.PRESETS[2])
        self.assertTrue(hotkeys.shortcut_working(self.paths))

    def test_second_launch_shows_the_running_apps_window(self):
        # Clicking the launcher while the tray runs must surface the window, not do nothing.
        held = tray.desktop.lock(self.paths.runtime / "menubar.lock")
        self.addCleanup(os.close, held)
        with patch.object(tray.desktop, "platform_name", return_value="linux"):
            self.assertEqual(tray.main(), 0)
        self.assertEqual(
            self.popen.call_args.args[0][1], str(Path(tray.__file__).with_name("app.py"))
        )


if __name__ == "__main__":
    unittest.main()
