"""The floating recording pill and how the dictation worker hands over to it."""

from __future__ import annotations

import array
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import dictation as d
import overlay

TOKEN = "abc123"
XRANDR = """Monitors: 2
 0: +*DP-1 2560/600x1440/340+1080+249  DP-1
 1: +HDMI-1 1080/480x1920/270+0+0  HDMI-1
"""


def pcm(amplitude: int, count: int = 800) -> bytes:
    return array.array("h", [amplitude if i % 2 else -amplitude for i in range(count)]).tobytes()


class HelperTests(unittest.TestCase):
    def test_levels_follow_the_voice(self):
        self.assertEqual(overlay.level(b""), 0.0)
        self.assertEqual(overlay.level(pcm(0)), 0.0)
        quiet, speech, shout = (
            overlay.level(pcm(30)),
            overlay.level(pcm(3000)),
            overlay.level(pcm(30000)),
        )
        self.assertLess(quiet, 0.1)
        self.assertTrue(0.3 < speech < 1.0)
        self.assertEqual(shout, 1.0)
        self.assertEqual(overlay.level(pcm(3000) + b"\x01"), speech)  # A torn last byte.

    def test_the_primary_monitor_is_chosen(self):
        self.assertEqual(overlay.primary_monitor(XRANDR), (1080, 249, 2560, 1440))
        without = XRANDR.replace("+*DP-1", "+DP-1")
        self.assertEqual(overlay.primary_monitor(without), (1080, 249, 2560, 1440))
        self.assertIsNone(overlay.primary_monitor(""))

    def test_states_map_to_what_the_pill_shows(self):
        def show(**state):
            return overlay.view({"token": TOKEN, **state}, TOKEN).mode

        self.assertEqual(show(phase="recording"), "recording")
        self.assertEqual(show(phase="transcribing"), "transcribing")
        self.assertEqual(show(phase="cancelling"), "cancelled")
        self.assertEqual(show(phase="idle", result="copied"), "copied")
        self.assertEqual(show(phase="idle", result="empty"), "empty")
        self.assertEqual(show(phase="idle"), "cancelled")
        error = overlay.view({"token": TOKEN, "phase": "error", "message": "No mic"}, TOKEN)
        self.assertEqual((error.mode, error.message), ("error", "No mic"))
        self.assertEqual(overlay.view({"token": "other", "phase": "recording"}, TOKEN).mode, "gone")

    def test_small_helpers(self):
        self.assertEqual(overlay.elapsed(67.9), "1:07")
        self.assertEqual(overlay._mix("#000000", "#ffffff", 0.5), "#808080")
        self.assertEqual(overlay._mix("#000000", "#ffffff", 3), "#ffffff")


class WorkerHandoverTests(unittest.TestCase):
    def test_ready_means_the_pill_is_up(self):
        script = "import sys; print('ready', flush=True); sys.stdin.read()"
        process = subprocess.Popen(
            [sys.executable, "-c", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE
        )
        self.addCleanup(process.wait)
        self.addCleanup(process.stdin.close)
        self.assertTrue(d.overlay_ready(process))

    def test_silence_or_an_early_exit_falls_back_to_notifications(self):
        self.assertFalse(d.overlay_ready(None))
        quiet = subprocess.Popen(
            [sys.executable, "-c", "raise SystemExit(3)"], stdout=subprocess.PIPE
        )
        self.assertFalse(d.overlay_ready(quiet))
        quiet.wait()
        quiet.stdout.close()
        stuck = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(5)"], stdout=subprocess.PIPE
        )
        self.addCleanup(stuck.stdout.close)
        self.addCleanup(stuck.wait)
        self.addCleanup(stuck.kill)
        self.assertFalse(d.overlay_ready(stuck, timeout=0.2))

    def test_the_installed_private_python_is_preferred(self):
        with tempfile.TemporaryDirectory() as folder:
            prefix = Path(folder)
            (prefix / "lib").mkdir()
            module = prefix / "lib/dictation.py"
            with patch.object(d, "__file__", str(module)):
                self.assertEqual(d.overlay_python(), sys.executable)
                private = prefix / "share/whisper-dictation/venv/bin/python"
                private.parent.mkdir(parents=True)
                private.touch()
                with patch.object(d.desktop, "platform_name", return_value="linux"):
                    self.assertEqual(d.overlay_python(), str(private))

    def test_turned_off_it_is_never_started(self):
        config = Mock(b=Mock(return_value=False))
        with patch.object(d.subprocess, "Popen") as popen:
            self.assertIsNone(d.start_overlay(config, TOKEN))
        popen.assert_not_called()


class OverlayWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import tkinter as tk

            probe = tk.Tk()
            probe.destroy()
        except Exception as exc:  # noqa: BLE001
            raise unittest.SkipTest(f"Tk/display unavailable: {exc}")

    def setUp(self):
        import tkinter as tk

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        env = patch.dict(
            os.environ,
            {
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_CACHE_HOME": str(root / "cache"),
                "XDG_RUNTIME_DIR": str(root / "runtime"),
            },
        )
        env.start()
        self.addCleanup(env.stop)
        self.paths = d.Paths()
        self.now = 100.0
        self.launched: list[list[str]] = []
        self.state("recording")
        self.root = tk.Tk()
        with patch.object(overlay.shutil, "which", return_value=None):
            self.pill = overlay.Overlay(
                self.root, self.paths, TOKEN, clock=lambda: self.now, launch=self.launched.append
            )
        self.addCleanup(self.close)

    def close(self):
        if not self.pill.closed:
            self.pill.close()

    def state(self, phase, **extra):
        d.atomic(self.paths.state, json.dumps({"phase": phase, "token": TOKEN, **extra}))

    def frames(self, seconds):
        for _ in range(int(seconds / 0.05)):
            self.now += 0.05
            self.pill.tick()
            if self.pill.closed:
                return

    def test_the_bars_rise_with_the_voice_and_the_timer_runs(self):
        self.paths.audio.write_bytes(pcm(0, 16000))
        self.frames(0.5)
        calm = max(self.pill.heights)
        self.assertEqual(self.pill.mode, "recording")
        with self.paths.audio.open("ab") as audio:
            audio.write(pcm(8000))
        self.frames(0.2)
        self.assertGreater(max(self.pill.heights), calm + 0.3)
        self.frames(1.0)
        self.assertTrue(self.pill._words(self.now)[1].startswith("0:01"))

    def test_a_live_draft_grows_the_pill(self):
        self.paths.preview.write_text("hello there general kenobi", encoding="utf-8")
        self.frames(0.3)
        self.assertEqual(self.pill.height, overlay.DRAFT_HEIGHT)
        self.assertEqual(self.pill.draft, "hello there general kenobi")

    def test_transcribing_then_copied_then_it_goes_away(self):
        self.frames(1.0)
        self.state("transcribing")
        self.frames(0.3)
        self.assertEqual(self.pill.mode, "transcribing")
        self.assertIn("Recorded 0:01", self.pill._words(self.now)[1])
        self.state("idle", result="copied")
        self.frames(0.5)
        self.assertEqual(self.pill._words(self.now)[0], "Copied to the clipboard")
        self.assertFalse(self.pill.closed)
        self.frames(overlay.HOLD["copied"] + 1)
        self.assertTrue(self.pill.closed)

    def test_the_buttons_stop_or_cancel_the_recording(self):
        self.frames(0.2)
        (left, top, right, bottom) = self.pill._buttons()["stop"]
        self.pill._click(Mock(x=(left + right) // 2, y=(top + bottom) // 2))
        self.assertTrue(self.launched[-1][-1].endswith("dictation.py"))
        self.assertEqual(self.pill.mode, "transcribing")
        self.pill.mode = "recording"
        (left, top, right, bottom) = self.pill._buttons()["cancel"]
        self.pill._click(Mock(x=left + 2, y=top + 14))
        self.assertEqual(self.launched[-1][-1], "--cancel")
        self.pill._click(Mock(x=5, y=5))  # Not on a button: nothing happens.
        self.assertEqual(len(self.launched), 2)

    def test_errors_show_their_message_and_another_session_closes_it(self):
        self.state("error", message="Microphone could not start.")
        self.frames(0.3)
        self.assertEqual(
            self.pill._words(self.now), ("Dictation stopped", "Microphone could not start.")
        )
        d.atomic(self.paths.state, json.dumps({"phase": "recording", "token": "next"}))
        self.frames(overlay.HOLD["error"] + 1)
        self.assertTrue(self.pill.closed)


if __name__ == "__main__":
    unittest.main()
