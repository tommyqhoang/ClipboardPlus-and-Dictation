"""Tray behavior with a stand-in for pystray; no desktop session needed."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

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
        self.notification_titles = []
        self.stopped = False

    def update_menu(self):
        self.updates += 1

    def notify(self, message, title):
        self.notifications.append(message)
        self.notification_titles.append(title)

    def stop(self):
        self.stopped = True


class LinuxIcon(Icon):
    """pystray's GTK/AppIndicator icon: every change writes a new temp file."""

    def __init__(self, *args):
        self.removed = 0
        self._icon_path = None
        super().__init__(*args)

    def _update_fs_icon(self):
        self._icon_path = "/tmp/temporary"

    def _remove_fs_icon(self):
        self.removed += 1

    @property
    def icon(self):
        return self._icon

    @icon.setter
    def icon(self, value):
        self._icon = value
        self._remove_fs_icon()
        self._update_fs_icon()


class Picture:
    def __init__(self, name):
        self.name = name

    def save(self, path, kind):
        Path(path).write_text(f"{kind}:{self.name}")


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
        self.tray.history_key = FakeHotKey()
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

    def test_linux_tray_icons_are_steady_files_that_are_never_deleted(self):
        # GNOME's top bar sometimes read pystray's deleted temp file and showed nothing.
        pystray = SimpleNamespace(MenuItem=Item, Menu=Menu, Icon=LinuxIcon)
        linux = tray.Tray(pystray, SimpleNamespace(open=lambda path: Picture(Path(path).name)))
        icon = linux.icon
        idle = str(self.paths.runtime / "tray-idle.png")
        recording = str(self.paths.runtime / "tray-recording.png")
        self.assertEqual(icon._icon_path, idle)
        self.assertEqual(Path(recording).read_text(), "PNG:tray-recording.png")
        removed = icon.removed
        icon.icon = linux.images["recording"]
        self.assertEqual(icon._icon_path, recording)
        icon.icon = linux.images["idle"]
        self.assertEqual(icon._icon_path, idle)
        self.assertEqual(icon.removed, removed)  # Nothing is deleted any more.
        self.assertTrue(Path(idle).exists() and Path(recording).exists())

    def test_menu_matches_macos_and_uses_shared_icon(self):
        labels = [
            entry.text if isinstance(entry.text, str) else "dynamic" for entry in self.items()
        ]
        for expected in (
            "Cancel Recording",
            "Copy Last Transcript",
            "Record New Shortcut…",
            "Open at Login",
            "Clipboard+ Website…",
            "Settings…",
            f"Quit {hotkeys.APP_NAME}",
        ):
            self.assertIn(expected, labels)
        top = [entry.text for entry in self.tray.icon.menu if isinstance(entry, Item)]
        for tucked in ("Open at Login", "Clipboard+ Website…"):
            self.assertNotIn(tucked, top)  # Under More, keeping the menu short.
        self.assertEqual(self.tray.icon.icon, "whisper-dictation.png")
        with patch("webbrowser.open") as browser:
            self.item("Clipboard+ Website").action()
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
        with patch.object(tray.desktop, "platform_name", return_value="windows"):
            self.tray.apply(taken)
        self.assertEqual(self.tray.shortcut, preset)
        self.assertIn("already in use", self.tray.icon.notifications[-1])

    def test_tick_tracks_recording_and_shortcut_window(self):
        with patch.object(self.tray.service, "ready", return_value=True):
            self.tray.hotkey_ok = True
            self.tray.tick()
            self.assertIn("anywhere to dictate", self.tray.status_text())
            self.assertIn("Start Dictation", self.tray.toggle_text())
            self.assertFalse(self.visible("Cancel Recording"))
            d.atomic(self.paths.state, '{"phase":"recording","started_at":0}')
            with patch.object(d, "busy", return_value=True):
                self.tray.tick()
            self.assertEqual(self.tray.icon.icon, "tray-recording.png")
            self.assertIn("recording", self.tray.icon.title)
            self.assertEqual(self.tray.status_text(), "Recording…")
            self.assertIn("Stop and Transcribe", self.tray.toggle_text())
            self.assertTrue(self.visible("Cancel Recording"))
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

    def test_the_history_shortcut_is_registered_while_clipboard_is_on(self):
        hotkeys.Preferences(self.paths).save(features=hotkeys.Features(True, True))
        self.tray.sync_history_shortcut()
        self.assertEqual(self.tray.history_key.registered, [hotkeys.DEFAULT_HISTORY])
        self.tray.sync_history_shortcut()  # Unchanged: not registered again.
        self.assertEqual(len(self.tray.history_key.registered), 1)
        self.assertIn(hotkeys.DEFAULT_HISTORY.label(), self.tray.history_text())
        hotkeys.Preferences(self.paths).save(features=hotkeys.Features(True, False))
        self.tray.clip._stamp = None
        self.tray.sync_history_shortcut()
        self.assertEqual(self.tray.history_key.registered[-1], None)
        self.tray.open_history()
        self.assertIn("--clipboard", self.popen.call_args.args[0])

    def test_gnome_hotkey_binds_toggle_command(self):
        taken = hotkeys.Conflict("Old dictation", hotkeys.GNOME_LIST_PREFIX + "custom0/")
        with (
            patch.object(hotkeys, "gnome_shortcut", return_value=True) as bind,
            patch.object(hotkeys, "gnome_conflict", return_value=taken),
        ):
            gnome = tray.GnomeHotKey(lambda: None)
            self.assertTrue(gnome.register(hotkeys.DEFAULT))
            self.assertEqual(gnome.conflict, taken)
            self.assertTrue(gnome.register(None))
            self.assertIsNone(gnome.conflict)
        self.assertEqual(bind.call_args.args[1].name, "dictate-toggle")

    def test_a_conflict_is_recorded_and_announced_once(self):
        taken = hotkeys.Conflict("Old dictation", hotkeys.GNOME_LIST_PREFIX + "custom0/")
        self.tray.hotkey.conflict = taken
        self.tray.hotkey_ok = True
        self.tray.record_dictation()
        self.tray.record_dictation()
        self.assertEqual(hotkeys.shortcut_conflict(self.paths), taken)
        told = [n for n in self.tray.icon.notifications if "Old dictation" in n]
        self.assertEqual(len(told), 1)

    def test_main_refuses_macos(self):
        with patch.object(tray.desktop, "platform_name", return_value="macos"):
            self.assertEqual(tray.main(), 1)

    def test_main_without_a_display_exits_quietly_instead_of_crashing(self):
        with (
            patch.object(tray.desktop, "platform_name", return_value="linux"),
            patch.dict(os.environ, {"DISPLAY": "", "WAYLAND_DISPLAY": ""}),
            patch.object(tray.telemetry, "capture") as capture,
        ):
            self.assertEqual(tray.main(), 1)
        capture.assert_not_called()
        # The lock is released: a later start (at login) is not blocked.
        again = tray.desktop.lock(self.paths.runtime / "menubar.lock")
        self.assertIsNotNone(again)
        os.close(again)

    def test_main_with_an_unreachable_display_exits_quietly_but_real_errors_surface(self):
        real_import = __import__

        def importing(error):
            def fake(name, *args, **kwargs):
                if name == "pystray":
                    raise error
                return real_import(name, *args, **kwargs)

            return fake

        unreachable = type("DisplayConnectionError", (Exception,), {})("no X server")
        with (
            patch.object(tray.desktop, "platform_name", return_value="linux"),  # macOS: menubar.py.
            patch.object(tray.desktop, "has_display", return_value=True),
            patch.object(tray.telemetry, "install"),
            patch("builtins.__import__", importing(unreachable)),
        ):
            self.assertEqual(tray.main(), 1)
        with (
            patch.object(tray.desktop, "platform_name", return_value="linux"),  # macOS: menubar.py.
            patch.object(tray.desktop, "has_display", return_value=True),
            patch.object(tray.telemetry, "install"),
            patch("builtins.__import__", importing(ImportError("pystray missing"))),
            self.assertRaises(ImportError),
        ):
            tray.main()
        again = tray.desktop.lock(self.paths.runtime / "menubar.lock")
        self.assertIsNotNone(again)  # Released either way.
        os.close(again)

    def test_main_runs_the_tray_icon(self):
        fake = MagicMock()
        (self.paths.runtime / "menubar-quit").write_text("quit")  # Left by the last quit.
        with (
            patch.object(tray.desktop, "platform_name", return_value="linux"),  # macOS: menubar.py.
            patch.object(tray.desktop, "has_display", return_value=True),
            patch.object(tray.telemetry, "install") as install,
            # Neither is installed where only the standard library is (Python 3.10/3.14 CI).
            patch.dict(sys.modules, {"pystray": fake, "PIL": MagicMock()}),
            patch.object(tray, "Tray") as made,
        ):
            self.assertEqual(tray.main(), 0)
        install.assert_called_once_with("tray")
        made.assert_called_once()
        made.return_value.icon.run.assert_called_once_with(setup=made.return_value.started)
        self.assertFalse((self.paths.runtime / "menubar-quit").exists())

    def use_features(self, dictation, clipboard):
        hotkeys.Preferences(self.paths).save(features=hotkeys.Features(dictation, clipboard))
        stamp = self.tray.preferences.path.stat().st_mtime
        os.utime(self.tray.preferences.path, (stamp + 5, stamp + 5))

    def visible(self, text):
        """Whether any menu entry with this label is shown (some labels have two variants)."""
        entries = [
            entry
            for entry in self.items()
            if isinstance(entry.text, str)
            and entry.text.startswith(text)
            or callable(entry.text)
            and entry.text(None).startswith(text)
        ]
        return any(entry.options.get("visible", lambda _: True)(None) for entry in entries)

    def test_menu_shows_only_the_chosen_features(self):
        dictation_items = ("Start Dictation", "Copy Last Transcript", "Shortcut:")
        clipboard_items = ("Clipboard History…", "Pause Clipboard Capture")
        self.use_features(True, False)
        self.assertTrue(all(self.visible(t) for t in dictation_items))
        self.assertFalse(any(self.visible(t) for t in clipboard_items))
        self.use_features(False, True)
        self.assertFalse(any(self.visible(t) for t in dictation_items))
        self.assertTrue(all(self.visible(t) for t in clipboard_items))
        self.use_features(True, True)
        self.assertTrue(all(self.visible(t) for t in dictation_items + clipboard_items))

    def test_pause_and_resume_from_the_menu(self):
        self.use_features(False, True)
        pause = self.item("Pause Clipboard Capture")
        self.assertFalse(self.visible("Resume Clipboard Capture"))
        one_hour, until_resumed = [entry for entry in pause.action]
        self.tray.clip._wall = lambda: 1000.0
        one_hour.action()
        self.assertEqual(self.tray.preferences.clipboard().paused_until, 4600.0)
        self.assertFalse(self.visible("Pause Clipboard Capture"))
        self.assertTrue(self.visible("Resume Clipboard Capture"))
        self.item("Resume Clipboard Capture").action()
        self.assertEqual(self.tray.preferences.clipboard().paused_until, 0.0)
        until_resumed.action()
        self.assertEqual(self.tray.preferences.clipboard().paused_until, -1)

    def test_clipboard_history_opens_the_window_on_that_page(self):
        self.use_features(False, True)
        self.item("Clipboard History…").action()
        self.assertEqual(self.popen.call_args.args[0][-1], "--clipboard")
        self.assertTrue(self.popen.call_args.args[0][-2].endswith("app.py"))

    def test_tick_keeps_the_clipboard_service_running(self):
        self.use_features(True, True)
        started = Mock()
        started.poll.return_value = None
        self.tray.clip._popen = Mock(return_value=started)
        with patch.object(self.tray.service, "ready", return_value=True):
            for _ in range(3):
                self.tray.tick()
        self.assertEqual(self.tray.clip._popen.call_count, 1)
        self.assertTrue(self.tray.clip._popen.call_args.args[0][1].endswith("clipservice.py"))

    def test_quitting_the_tray_asks_its_clipboard_service_to_quit(self):
        self.use_features(True, True)
        started = Mock()
        started.poll.return_value = None
        self.tray.clip._popen = Mock(return_value=started)
        self.tray.clip.supervise()
        self.tray.quit()
        self.assertTrue((self.paths.runtime / "clip-quit").exists())

    def test_the_dictation_shortcut_follows_the_chosen_features(self):
        self.tray.hotkey_ok = True
        self.tray.dictation_registered = True
        self.use_features(False, True)
        self.tray.sync_dictation_shortcut()
        self.assertIsNone(self.tray.hotkey.registered[-1])
        self.assertFalse(self.tray.dictation_registered)
        self.tray.sync_dictation_shortcut()  # Idempotent.
        self.assertEqual(len(self.tray.hotkey.registered), 1)
        self.use_features(True, True)
        self.tray.sync_dictation_shortcut()
        self.assertEqual(self.tray.hotkey.registered[-1], self.tray.shortcut)
        self.assertTrue(self.tray.dictation_registered)

    def test_preference_changes_are_seen_even_at_the_same_mtime(self):
        # Coarse filesystem clocks (some Windows setups) give two quick saves one
        # mtime; the stamp must also cover the file's size or the tray keeps old
        # settings.
        self.use_features(False, True)
        self.tray.sync_dictation_shortcut()
        stamp = self.tray.preferences.path.stat().st_mtime
        self.use_features(True, True)
        os.utime(self.tray.preferences.path, (stamp, stamp))
        self.tray.sync_dictation_shortcut()
        self.assertEqual(self.tray.hotkey.registered[-1], self.tray.shortcut)

    def test_status_shows_the_clipboard_when_dictation_is_off(self):
        self.use_features(False, True)
        with patch.object(self.tray.clip, "status_line", return_value="Clipboard: 3 items"):
            self.assertEqual(self.tray.status_text(), "Clipboard: 3 items")

    def test_the_menu_is_rebuilt_only_when_something_changed(self):
        self.use_features(True, True)
        self.tray.clip._popen = Mock(return_value=Mock(poll=Mock(return_value=None)))
        with patch.object(self.tray.service, "ready", return_value=True):
            self.tray.tick()
            baseline = self.tray.icon.updates
            for _ in range(5):
                self.tray.tick()
            self.assertEqual(self.tray.icon.updates, baseline)
            self.tray.clip.pause(3600)
            self.tray.tick()
        self.assertEqual(self.tray.icon.updates, baseline + 1)

    def test_changing_another_preference_does_not_reregister_the_shortcut(self):
        self.tray.hotkey_ok = True
        with patch.object(self.tray.service, "ready", return_value=True):
            self.tray.tick()
            before = len(self.tray.hotkey.registered)
            self.tray.preferences.save(open_at_login=False)
            os.utime(self.tray.preferences.path, (10**9, 10**9 + 5))
            self.tray.tick()
        self.assertEqual(len(self.tray.hotkey.registered), before)

    def test_the_tray_is_named_after_the_app(self):
        self.assertEqual(self.tray.icon.title, hotkeys.APP_NAME)
        self.tray.notify("Hello")
        self.assertEqual(self.tray.icon.notification_titles[-1], hotkeys.APP_NAME)
        with (
            patch.object(self.tray.service, "ready", return_value=True),
            patch.object(
                tray.workflow,
                "snapshot",
                return_value={"phase": "recording", "active": True, "elapsed_seconds": 1},
            ),
        ):
            self.tray.tick()
        self.assertTrue(self.tray.icon.title.startswith(hotkeys.APP_NAME))

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
        with patch.object(tray.desktop, "platform_name", return_value="linux"):
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
