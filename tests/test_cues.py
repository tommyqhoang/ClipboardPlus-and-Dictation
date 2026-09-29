"""Audible cues, the pill's accessible name, and falling back to notifications."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import cues
import dictation as d
import overlay
import support  # noqa: F401 - one Tk root per process on macOS (see there)

TOKEN = "cue-token"


class CueTests(unittest.TestCase):
    def test_modes_choose_which_cues_play(self):
        self.assertFalse(any(cues.wanted(kind, "off") for kind in cues.KINDS))
        self.assertEqual([k for k in cues.KINDS if cues.wanted(k, "errors")], ["error"])
        self.assertTrue(all(cues.wanted(kind, "all") for kind in cues.KINDS))
        self.assertFalse(cues.wanted("nonsense", "all"))
        self.assertEqual(cues.mode_from("all"), "all")
        self.assertEqual(cues.mode_from("loud"), cues.DEFAULT_MODE)
        self.assertEqual(cues.DEFAULT_MODE, "errors")  # Errors beep unless turned off.

    def test_nothing_plays_when_the_mode_says_no(self):
        spawn = Mock(return_value=True)
        self.assertFalse(cues.play("start", "errors", spawn=spawn))
        self.assertFalse(cues.play("error", "off", spawn=spawn))
        spawn.assert_not_called()

    def test_macos_uses_afplay(self):
        spawn = Mock(return_value=True)
        with (
            patch.object(cues.shutil, "which", return_value="/usr/bin/afplay"),
            patch.object(cues.Path, "exists", return_value=True),
        ):
            self.assertTrue(cues.play("error", "errors", platform="macos", spawn=spawn))
        self.assertEqual(spawn.call_args.args[0], ["afplay", cues.MAC_SOUNDS["error"]])

    def test_linux_prefers_paplay_then_canberra_then_the_bell(self):
        spawn = Mock(return_value=True)
        with (
            patch.object(cues.shutil, "which", side_effect=lambda n: n),
            patch.object(cues.Path, "exists", return_value=True),
        ):
            cues.play("stop", "all", platform="linux", spawn=spawn)
        self.assertEqual(spawn.call_args.args[0][0], "paplay")
        self.assertTrue(spawn.call_args.args[0][1].endswith("complete.oga"))
        spawn.reset_mock()
        with (
            patch.object(
                cues.shutil, "which", side_effect=lambda n: n if n == "canberra-gtk-play" else None
            ),
            patch.object(cues.Path, "exists", return_value=False),
        ):
            cues.play("error", "all", platform="linux", spawn=spawn)
        self.assertEqual(spawn.call_args.args[0], ["canberra-gtk-play", "-i", "dialog-error"])
        spawn.reset_mock()
        with (
            patch.object(cues.shutil, "which", return_value=None),
            patch.object(cues, "_bell", return_value=True) as bell,
        ):
            self.assertTrue(cues.play("error", "all", platform="linux", spawn=spawn))
        bell.assert_called_once()
        spawn.assert_not_called()

    def test_windows_beeps_through_winsound(self):
        winsound = Mock()
        with patch.dict(sys.modules, {"winsound": winsound}):
            self.assertTrue(cues.play("error", "errors", platform="windows"))
        # Beep runs on a thread so the session never waits for it.
        import time

        for _ in range(50):
            if winsound.Beep.called:
                break
            time.sleep(0.01)
        winsound.Beep.assert_called_once_with(*cues.WINDOWS_BEEPS["error"])

    def test_the_bell_only_rings_a_terminal(self):
        fake = Mock()
        fake.isatty.return_value = False
        with patch.object(cues.sys, "__stderr__", fake):
            self.assertFalse(cues._bell())
        fake.isatty.return_value = True
        with patch.object(cues.sys, "__stderr__", fake):
            self.assertTrue(cues._bell())
        fake.write.assert_called_once_with("\a")

    def test_a_missing_player_never_raises(self):
        with patch.object(cues.subprocess, "Popen", side_effect=OSError):
            self.assertFalse(cues._spawn(["nothing"]))

    def test_the_setting_is_validated(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.dict(
                os.environ,
                {
                    "XDG_CONFIG_HOME": folder + "/c",
                    "XDG_CACHE_HOME": folder + "/k",
                    "XDG_RUNTIME_DIR": folder + "/r",
                    "DICTATION_SOUNDS": "loud",
                },
            ):
                with self.assertRaisesRegex(d.DictationError, "sounds must be"):
                    d.Config(d.Paths())
                os.environ["DICTATION_SOUNDS"] = "all"
                self.assertEqual(d.Config(d.Paths()).s("sounds"), "all")


class UnsupportedPillTests(unittest.TestCase):
    def reason(self, environment, platform="linux"):
        with patch.object(overlay.desktop, "platform_name", return_value=platform):
            return overlay.pill_unsupported(environment)

    def test_reasons_a_pill_cannot_float(self):
        self.assertIn("no X11", self.reason({"WAYLAND_DISPLAY": "wayland-0"}))
        self.assertIn("tile", self.reason({"DISPLAY": ":0", "SWAYSOCK": "/run/s"}))
        self.assertIn("tile", self.reason({"DISPLAY": ":0", "HYPRLAND_INSTANCE_SIGNATURE": "x"}))
        self.assertIn("DICTATION_PILL", self.reason({"DISPLAY": ":0", "DICTATION_PILL": "0"}))
        self.assertEqual(self.reason({"DISPLAY": ":0"}), "")
        self.assertEqual(self.reason({}, platform="macos"), "")

    def test_main_leaves_quietly_for_notifications(self):
        with (
            patch.object(sys, "argv", ["overlay.py", TOKEN]),
            patch.object(overlay.telemetry, "install"),
            patch.object(overlay, "pill_unsupported", return_value="why"),
            patch.object(overlay.tk, "Tk") as tk_root,
        ):
            self.assertEqual(overlay.main(), overlay.EXIT_UNSUPPORTED)
        tk_root.assert_not_called()
        with (
            patch.object(sys, "argv", ["overlay.py", TOKEN]),
            patch.object(overlay.telemetry, "install"),
            patch.object(overlay, "pill_unsupported", return_value=""),
            patch.object(overlay.tk, "Tk", side_effect=tk.TclError("no display")),
        ):
            self.assertEqual(overlay.main(), overlay.EXIT_NO_DISPLAY)


class PillCueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            probe = tk.Tk()
            probe.destroy()
        except Exception as exc:  # noqa: BLE001
            raise unittest.SkipTest(f"Tk/display unavailable: {exc}")

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        env = patch.dict(
            os.environ,
            {
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_CACHE_HOME": str(root / "cache"),
                "XDG_RUNTIME_DIR": str(root / "runtime"),
                "DICTATION_SOUNDS": "all",
            },
        )
        env.start()
        self.addCleanup(env.stop)
        self.paths = d.Paths()
        self.now = 10.0
        self.state("starting")
        self.root = tk.Tk()
        self.pill = overlay.Overlay(self.root, self.paths, TOKEN, clock=lambda: self.now)
        self.addCleanup(lambda: self.pill.closed or self.pill.close())

    def state(self, phase, **extra):
        d.atomic(self.paths.state, json.dumps({"phase": phase, "token": TOKEN, **extra}))

    def step(self):
        self.now += 0.2
        self.pill.tick()

    def test_start_stop_and_error_are_played_once_each(self):
        with patch.object(overlay.cues, "play") as play:
            self.state("recording")
            self.step()
            self.step()
            self.state("idle", result="copied")
            self.step()
            self.step()
        self.assertEqual([c.args[0] for c in play.call_args_list], ["start", "stop"])
        self.assertEqual(play.call_args.args[1], "all")

    def test_an_error_is_played_with_the_configured_mode(self):
        with patch.object(overlay.cues, "play") as play:
            self.state("error", message="Microphone could not start.")
            self.step()
        play.assert_called_once_with("error", "all")

    def test_the_window_and_accessible_name_follow_the_state(self):
        with patch.object(overlay.cues, "play"):
            self.state("recording")
            self.step()
            name = self.pill.accessible_name
            self.assertTrue(name.startswith("Clipboard+ dictation: recording"))
            self.assertEqual(self.root.title(), name)
            self.state("error", message="Microphone could not start.")
            self.step()
        self.assertIn("Microphone could not start.", self.pill.accessible_name)
        self.assertIn("stopped", self.root.title())
        for mode, words in (
            ("transcribing", "transcribing"),
            ("copied", "copied"),
            ("empty", "no speech"),
            ("cancelled", "cancelled"),
        ):
            self.pill.mode = mode
            self.assertIn(words, self.pill.accessible_name)


if __name__ == "__main__":
    unittest.main()
