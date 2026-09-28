"""What the tray and the menu bar do about the clipboard service, tested once for both."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import clipcontrol
import clipservice
import desktop
import dictation as d
import hotkeys


class ControlCase(unittest.TestCase):
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
        self.prefs = hotkeys.Preferences(self.paths)
        self.now = 1000.0
        self.wall = 5_000_000.0
        self.started: list[list[str]] = []
        self.processes: list[Mock] = []

    def popen(self, command, **_):
        self.started.append(list(command))
        process = Mock()
        process.poll.return_value = None
        self.processes.append(process)
        return process

    def control(self) -> clipcontrol.ClipboardControl:
        return clipcontrol.ClipboardControl(
            self.paths,
            self.prefs,
            command=["/venv/python", str(clipcontrol.HERE / "clipservice.py")],
            popen=self.popen,
            clock=lambda: self.now,
            wall=lambda: self.wall,
        )

    def enable(self, dictation=True, clipboard=True):
        self.prefs.save(features=hotkeys.Features(dictation, clipboard))


class HistoryShortcutTests(ControlCase):
    def test_the_history_shortcut_follows_the_clipboard_feature_and_its_setting(self):
        self.enable(clipboard=False)
        control = self.control()
        control.changed()
        self.assertIsNone(control.history_shortcut())
        self.enable(clipboard=True)
        self.assertTrue(control.changed())
        self.assertEqual(control.history_shortcut(), hotkeys.DEFAULT_HISTORY)
        self.assertFalse(control.changed())
        self.prefs.save(history_shortcut=False)
        control._stamp = None  # The file's time may not have moved within this test.
        self.assertTrue(control.changed())
        self.assertIsNone(control.history_shortcut())


class SupervisionTests(ControlCase):
    def test_the_service_is_started_once_while_it_runs(self):
        self.enable()
        control = self.control()
        for _ in range(5):
            control.supervise()
            self.now += 1
        self.assertEqual(len(self.started), 1)
        self.assertEqual(self.started[0][0], "/venv/python")
        self.assertTrue(self.started[0][1].endswith("clipservice.py"))

    def test_default_command_comes_from_desktop_relaunch(self):
        self.enable()
        with patch.object(
            clipcontrol.desktop, "relaunch", return_value=["/opt/Clipboard+/clipservice"]
        ):
            control = clipcontrol.ClipboardControl(
                self.paths,
                self.prefs,
                popen=self.popen,
                clock=lambda: self.now,
                wall=lambda: self.wall,
            )
            control.supervise()
        self.assertEqual(self.started[0], ["/opt/Clipboard+/clipservice"])

    def test_it_is_never_started_while_the_feature_is_off(self):
        self.enable(clipboard=False)
        control = self.control()
        control.supervise()
        self.assertEqual(self.started, [])

    def test_a_service_that_exits_is_restarted(self):
        self.enable()
        control = self.control()
        control.supervise()
        self.processes[0].poll.return_value = 0
        self.now += clipcontrol.RESTART_SECONDS + 1
        control.supervise()
        self.assertEqual(len(self.started), 2)

    def test_a_crash_loop_backs_off(self):
        self.enable()
        control = self.control()
        for _ in range(12):
            control.supervise()
            for process in self.processes:
                process.poll.return_value = 1
            self.now += clipcontrol.RESTART_SECONDS + 0.1
        self.assertLess(len(self.started), 6)  # Not one start per tick.

    def test_a_service_already_running_elsewhere_is_left_alone(self):
        self.enable()
        held = desktop.lock(self.paths.runtime / "clipservice.lock")
        self.addCleanup(os.close, held)
        self.control().supervise()
        self.assertEqual(self.started, [])

    def test_turning_the_feature_on_later_starts_it(self):
        self.enable(clipboard=False)
        control = self.control()
        control.supervise()
        self.assertEqual(self.started, [])
        self.enable(clipboard=True)
        os.utime(self.prefs.path, (self.wall, self.wall + 10))
        control.supervise()
        self.assertEqual(len(self.started), 1)

    def test_stop_asks_the_service_to_quit(self):
        self.enable()
        control = self.control()
        control.supervise()
        control.stop()
        self.assertTrue((self.paths.runtime / "clip-quit").exists())

    def test_stop_does_nothing_when_no_service_was_started(self):
        self.control().stop()
        self.assertFalse((self.paths.runtime / "clip-quit").exists())


class PauseTests(ControlCase):
    def test_pause_for_an_hour_then_resume(self):
        self.enable()
        control = self.control()
        control.pause(3600)
        self.assertEqual(self.prefs.clipboard().paused_until, self.wall + 3600)
        self.assertTrue(control.paused())
        self.wall += 3601
        self.assertFalse(control.paused())
        control.pause(None)
        self.assertEqual(self.prefs.clipboard().paused_until, -1)
        self.assertTrue(control.paused())
        control.resume()
        self.assertFalse(control.paused())

    def test_pausing_keeps_the_other_clipboard_settings(self):
        self.prefs.save(
            clipboard=hotkeys.ClipboardSettings(keep_items=200, keep_days=7, images=False)
        )
        self.control().pause(60)
        saved = self.prefs.clipboard()
        self.assertEqual((saved.keep_items, saved.keep_days, saved.images), (200, 7, False))

    def test_changes_are_noticed_once(self):
        self.enable()
        control = self.control()
        self.assertTrue(control.changed())  # First look.
        self.assertFalse(control.changed())
        control.pause(60)
        self.assertTrue(control.changed())
        self.assertFalse(control.changed())


class StatusLineTests(ControlCase):
    def line(self, **status):
        self.enable()
        clipservice.write_status(self.paths, clock=lambda: self.wall, **status)
        return self.control().status_line(clock=lambda: self.wall)

    def test_the_line_describes_the_service(self):
        self.assertEqual(self.line(state="capturing", count=42), "Clipboard: 42 items")
        self.assertEqual(self.line(state="capturing", count=1), "Clipboard: 1 item")
        self.assertEqual(self.line(state="capturing", count=0), "Clipboard: nothing copied yet")
        self.assertEqual(self.line(state="paused", count=3), "Clipboard: paused")
        self.assertEqual(
            self.line(state="error", message="No clipboard access."),
            "Clipboard: No clipboard access.",
        )

    def test_a_service_that_has_not_reported_is_starting(self):
        self.enable()
        self.assertEqual(self.control().status_line(), "Clipboard: starting…")

    def test_no_line_when_the_feature_is_off(self):
        self.enable(clipboard=False)
        self.assertEqual(self.control().status_line(), "")


if __name__ == "__main__":
    unittest.main()
