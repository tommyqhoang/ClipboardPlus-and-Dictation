"""The recording Session's steps, each alone."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import dictation as d


class SessionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        env = patch.dict(
            os.environ,
            {
                "XDG_CONFIG_HOME": str(root / "c"),
                "XDG_CACHE_HOME": str(root / "k"),
                "XDG_RUNTIME_DIR": str(root / "r"),
                "DICTATION_MODEL": str(root / "model.bin"),
            },
        )
        env.start()
        self.addCleanup(env.stop)
        (root / "model.bin").write_bytes(b"m")
        self.paths = d.Paths()
        self.config = d.Config(self.paths)
        self.fd = os.open(os.devnull, os.O_RDONLY)
        self.session = d.Session(self.config, self.paths, self.fd, "tok")
        self.addCleanup(self.session.executor.shutdown)

    def test_a_dead_recorder_at_start_reports_its_own_words(self):
        recorder = MagicMock()
        recorder.poll.return_value = 1

        def start(*args, **kwargs):
            kwargs["stderr"].write(b"arecord: audio open error: Device busy\n")
            return recorder

        self.config.values["backend"] = "http"  # No speech engine to start.
        with (
            patch.object(d.permissions, "microphone_blocked", return_value=None),
            patch.object(d.subprocess, "Popen", side_effect=start),
            patch.object(d, "start_overlay", return_value=None),
            patch.object(d.time, "sleep"),
            patch.object(d.desktop, "recorder_command", return_value=["arecord"]),
            patch.object(d, "fallback_devices", return_value=iter(["default"])),
            self.assertRaises(d.DictationError) as caught,
        ):
            self.session.start_recorder()
        self.assertIn("Microphone could not start", str(caught.exception))
        self.assertIn("Device busy", str(caught.exception))

    def test_an_unavailable_microphone_falls_back_to_the_system_default(self):
        dead, live = MagicMock(stdin=None), MagicMock(stdin=None)
        dead.poll.return_value = 1
        live.poll.return_value = None
        used = []

        def start(command, **kwargs):
            used.append(command)
            return dead if len(used) == 1 else live

        self.config.values["backend"] = "http"
        self.config.values["audio_backend"] = "alsa"
        self.config.values["device"] = "plughw:CARD=Gone"
        with (
            patch.object(d.permissions, "microphone_blocked", return_value=None),
            patch.object(d.subprocess, "Popen", side_effect=start),
            patch.object(d, "start_overlay", return_value=None),
            patch.object(d.time, "sleep"),
            patch.object(d, "notify"),
        ):
            self.session.start_recorder()
        self.assertIs(self.session.recorder, live)
        self.assertIn("-D", used[1])
        self.assertEqual(used[1][used[1].index("-D") + 1], "default")

    def test_fallback_devices_add_other_cards_and_skip_windows(self):
        self.config.values["audio_backend"] = "alsa"
        listing = MagicMock(stdout=b"null\nplughw:CARD=USB,DEV=0\n    USB mic\n")
        with patch.object(d.subprocess, "run", return_value=listing):
            self.assertEqual(
                list(d.fallback_devices(self.config, "default")),
                ["default", "pipewire", "pulse", "plughw:CARD=USB,DEV=0"],
            )
        with patch.object(d.desktop, "audio_backend", return_value="dshow"):
            self.assertEqual(list(d.fallback_devices(self.config, "Mic")), ["Mic"])

    def test_a_known_blocked_microphone_never_starts_the_recorder(self):
        with (
            patch.object(d.permissions, "microphone_blocked", return_value="Allow it in Settings."),
            patch.object(d.subprocess, "Popen") as popen,
            self.assertRaisesRegex(d.DictationError, "Allow it"),
        ):
            self.session.start_recorder()
        popen.assert_not_called()

    def test_a_stopped_microphone_says_where_the_audio_is(self):
        recorder = MagicMock(returncode=2)
        recorder.poll.return_value = 2
        self.session.recorder = recorder
        self.session.recorder_errors.write_bytes(b"device unplugged\n")
        with self.assertRaises(d.DictationError) as caught:
            self.session.recorder_ended()
        text = str(caught.exception)
        self.assertIn("device unplugged", text)
        self.assertIn("audio is saved", text)
        self.assertIn("--transcribe", text)

    def test_a_clean_exit_just_ends_the_recording(self):
        recorder = MagicMock(returncode=0)
        recorder.poll.return_value = 0
        self.session.recorder = recorder
        self.assertTrue(self.session.recorder_ended())
        recorder.poll.return_value = None
        self.assertFalse(self.session.recorder_ended())

    def test_failures_are_notified_even_with_the_pill_on_screen(self):
        self.session.pill = MagicMock()
        with (
            patch.object(d, "notify") as told,
            patch.object(d.cues, "play") as sound,
            patch.object(d.telemetry, "capture"),
        ):
            self.session.fail(d.DictationError("The microphone stopped."))
        told.assert_called_once_with(self.config, "The microphone stopped.")
        sound.assert_not_called()  # The pill plays it.
        self.assertEqual(json.loads(self.paths.state.read_text())["phase"], "error")

    def test_without_a_pill_the_worker_plays_the_error_beep(self):
        with (
            patch.object(d, "notify"),
            patch.object(d.cues, "play") as sound,
            patch.object(d.telemetry, "capture"),
        ):
            self.session.fail(d.DictationError("x"))
        sound.assert_called_once_with("error", "errors")

    def test_a_pill_that_cannot_open_hands_over_to_notifications(self):
        pill = MagicMock(returncode=4)
        pill.poll.return_value = 4
        self.session.pill = pill
        with patch.object(d, "notify") as told, patch.object(d.cues, "play") as sound:
            self.session.check_pill()
            self.session.check_pill()  # Only once.
            self.session.tell("More")
        self.assertTrue(self.session.pill_failed)
        self.assertEqual(told.call_args_list[0].kwargs, {"session": False})
        self.assertEqual(told.call_args_list[-1].kwargs, {"session": False})
        self.assertEqual(told.call_count, 2)
        sound.assert_called_once_with("start", "errors")

    def test_a_running_pill_keeps_notifications_quiet(self):
        pill = MagicMock()
        pill.poll.return_value = None
        self.session.pill = pill
        with patch.object(d, "notify") as told:
            self.session.check_pill()
            self.session.tell("Recording")
        told.assert_called_once_with(self.config, "Recording", session=True)

    def test_time_limit_warns_once_then_ends(self):
        self.config.values["max_seconds"] = 20
        with patch.object(d, "notify") as told:
            self.assertFalse(self.session.time_is_up())
            self.assertFalse(self.session.time_is_up())
        self.assertEqual(told.call_count, 1)
        self.session.started -= 25
        with patch.object(d, "notify"):
            self.assertTrue(self.session.time_is_up())

    def test_stop_requests_are_matched_by_token(self):
        d.atomic(self.paths.control, json.dumps({"token": "other", "action": "stop"}))
        self.assertFalse(self.session.stop_requested())
        d.atomic(self.paths.control, json.dumps({"token": "tok", "action": "cancel"}))
        self.assertTrue(self.session.stop_requested())
        self.assertTrue(self.session.cancelled)


if __name__ == "__main__":
    unittest.main()
