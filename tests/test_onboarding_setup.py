"""Setup that never blocks, checks before downloading, tests the microphone, asks consent."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import dictation
import onboarding


class SetupBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = patch.dict(
            os.environ,
            {
                "XDG_CONFIG_HOME": str(self.root / "config"),
                "XDG_CACHE_HOME": str(self.root / "cache"),
                "XDG_RUNTIME_DIR": str(self.root / "runtime"),
            },
        )
        env.start()
        self.addCleanup(env.stop)
        # Windows refuses the "default" microphone; a named one keeps these tests the same everywhere.
        defaults = patch.dict(dictation.DEFAULTS, {"device": "Test Mic"})
        defaults.start()
        self.addCleanup(defaults.stop)
        self.paths = dictation.Paths()
        self.model = self.root / "custom.bin"
        self.model.write_bytes(b"lmgg-test")

    def run_setup(self, options, *, interactive=False, mic=(True, "ok"), typed=()):
        with (
            patch.object(onboarding, "is_interactive", return_value=interactive),
            patch("builtins.input", side_effect=typed) as asked,
            patch.object(onboarding.subprocess, "run"),
            patch.object(onboarding, "check_microphone", return_value=mic) as tested,
            patch.object(dictation.Config, "check"),
            contextlib.redirect_stdout(io.StringIO()) as out,
        ):
            onboarding.run(self.paths, options)
        return asked, tested, out.getvalue()


class NonInteractiveTests(SetupBase):
    def test_without_a_terminal_it_never_asks(self):
        options = onboarding.SetupOptions(model=str(self.model), device="USB Mic")
        asked, tested, out = self.run_setup(options)
        asked.assert_not_called()
        tested.assert_called_once()
        saved = json.loads(self.paths.config.read_text())
        self.assertEqual(saved["device"], "USB Mic")
        self.assertEqual(saved["language"], "en")
        self.assertFalse(saved["live"])
        self.assertIn("No terminal", out)

    def test_flags_choose_the_language_and_skip_the_mic_test(self):
        options = onboarding.SetupOptions(
            model=str(self.model), language="auto", skip_mic_test=True
        )
        _, tested, _ = self.run_setup(options)
        tested.assert_not_called()
        self.assertEqual(json.loads(self.paths.config.read_text())["language"], "auto")

    def test_a_failed_mic_test_only_warns_when_unattended(self):
        options = onboarding.SetupOptions(model=str(self.model))
        _, _, out = self.run_setup(options, mic=(False, "No sound"))
        self.assertIn("Continuing without", out)
        self.assertTrue(self.paths.config.exists())

    def test_a_closed_stdin_stops_asking(self):
        options = onboarding.SetupOptions(model=str(self.model), interactive=True)
        with patch.object(onboarding, "is_interactive", return_value=True):
            asked, _, _ = self.run_setup(options, typed=EOFError)
        self.assertTrue(asked.called)
        self.assertTrue(self.paths.config.exists())

    def test_the_command_line_runs_unattended_and_offline(self):
        # Setup insists on a recorder, a clipboard tool and a transcriber. CI machines have
        # none of them (and each OS names them differently), so stand-in scripts are used.
        stub = self.root / "tool.py"
        stub.write_text("import sys\nsys.exit(0)\n")
        tools = {
            "DICTATION_AUDIO_BACKEND": "alsa",
            "DICTATION_CLIPBOARD_BACKEND": "wayland",
            "DICTATION_ARECORD": str(stub),
            "DICTATION_WL_COPY": str(stub),
            "DICTATION_WHISPER_BIN": str(stub),
        }
        result = subprocess.run(
            [
                sys.executable,
                str(Path(dictation.__file__)),
                "--setup",
                "--model-file",
                str(self.model),
                "--skip-mic-test",
                "--mic",
                "Test Mic",
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
            env={**os.environ, "DICTATION_TELEMETRY": "0", **tools},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Step 1 of 5", result.stdout)
        self.assertNotIn("\x1b", result.stdout)  # No terminal control codes for a screen reader.


class ConsentTests(SetupBase):
    def test_consent_is_asked_explicitly_and_defaults_to_no(self):
        setter = MagicMock()
        options = onboarding.SetupOptions(model=str(self.model))
        with patch.object(onboarding.telemetry, "set_consent", setter, create=True):
            self.run_setup(options, interactive=True, typed=["1", "", "", "", "", "", ""])
        setter.assert_called_once_with(False)

    def test_yes_turns_sharing_on_and_a_flag_answers_for_you(self):
        setter = MagicMock()
        with patch.object(onboarding.telemetry, "set_consent", setter, create=True):
            self.run_setup(
                onboarding.SetupOptions(model=str(self.model), share_usage=True), interactive=False
            )
        setter.assert_called_once_with(True)

    def test_unattended_without_a_flag_still_says_no(self):
        setter = MagicMock()
        with patch.object(onboarding.telemetry, "set_consent", setter, create=True):
            self.run_setup(onboarding.SetupOptions(model=str(self.model)))
        setter.assert_called_once_with(False)

    def test_no_consent_api_means_no_step(self):
        with patch.object(onboarding.telemetry, "set_consent", None, create=True):
            _, _, out = self.run_setup(onboarding.SetupOptions(model=str(self.model)))
        self.assertNotIn("Step 5", out)


class PreflightTests(SetupBase):
    def test_offline_says_how_to_continue(self):
        with self.assertRaisesRegex(dictation.DictationError, "offline.*--model PATH"):
            onboarding.preflight_download(self.root / "models", online=lambda: False)

    def test_low_disk_space_reports_both_numbers(self):
        usage = MagicMock(free=50_000_000)
        with (
            patch.object(onboarding.shutil, "disk_usage", return_value=usage),
            self.assertRaisesRegex(dictation.DictationError, "160 MB needed, 50 MB free"),
        ):
            onboarding.preflight_download(self.root / "models", online=lambda: True)

    def test_enough_space_passes_and_unknown_space_does_not_block(self):
        onboarding.preflight_download(self.root / "models", needed=1, online=lambda: True)
        with patch.object(onboarding.shutil, "disk_usage", side_effect=OSError):
            onboarding.check_disk_space(self.root / "models")

    def test_the_download_itself_refuses_a_full_disk(self):
        usage = MagicMock(free=1_000)
        with (
            patch.object(onboarding.shutil, "disk_usage", return_value=usage),
            patch.object(onboarding, "open_url") as opened,
            self.assertRaisesRegex(dictation.DictationError, "free disk space"),
        ):
            onboarding.download_model(self.root / "models", "en")
        opened.assert_not_called()

    def test_connectable_reports_a_dead_network(self):
        with patch.object(onboarding, "connect", side_effect=OSError("unreachable")):
            self.assertFalse(onboarding._connectable("example.org", 443))
        sock = MagicMock()
        with patch.object(onboarding, "connect", return_value=sock):
            self.assertTrue(onboarding._connectable("example.org", 443))
        sock.close.assert_called_once()


class MicrophoneTests(unittest.TestCase):
    values = dictation.DEFAULTS | {"audio_backend": "alsa", "arecord": "arecord"}

    def record(self, **result):
        done = subprocess.CompletedProcess([], result.pop("code", 0), **result)
        return patch.object(onboarding.subprocess, "run", return_value=done)

    def test_a_recording_passes(self):
        with self.record(stdout=b"\x01\x00" * 4000, stderr=b""):
            worked, message = onboarding.check_microphone(self.values)
        self.assertTrue(worked)
        self.assertIn("recorded a short test", message)

    def test_silence_passes_with_a_warning(self):
        with self.record(stdout=b"\x00\x00" * 4000, stderr=b""):
            worked, message = onboarding.check_microphone(self.values)
        self.assertTrue(worked)
        self.assertIn("muted", message)

    def test_nothing_recorded_fails_with_the_tools_reason(self):
        with self.record(code=1, stdout=b"", stderr=b"arecord: device busy\n"):
            worked, message = onboarding.check_microphone(self.values)
        self.assertFalse(worked)
        self.assertIn("device busy", message)

    def test_a_hang_and_a_missing_tool_fail_cleanly(self):
        with patch.object(
            onboarding.subprocess, "run", side_effect=subprocess.TimeoutExpired("arecord", 7)
        ):
            self.assertFalse(onboarding.check_microphone(self.values)[0])
        with patch.object(onboarding.subprocess, "run", side_effect=FileNotFoundError("arecord")):
            self.assertFalse(onboarding.check_microphone(self.values)[0])

    def test_a_denied_mac_permission_fails_before_recording(self):
        with (
            patch.object(onboarding.permissions, "microphone_blocked", return_value="denied"),
            patch.object(onboarding.subprocess, "run") as run,
        ):
            self.assertEqual(onboarding.check_microphone(self.values), (False, "denied"))
        run.assert_not_called()

    def test_the_test_is_bounded_to_a_short_recording(self):
        with self.record(stdout=b"\x01\x00" * 4000, stderr=b"") as run:
            onboarding.check_microphone(self.values)
        self.assertEqual(run.call_args.args[0][-1], "1")  # arecord -d 1

    def test_declining_a_broken_microphone_leaves_settings_alone(self):
        case = SetupBase()
        case.setUp()
        self.addCleanup(case.doCleanups)  # Its env and defaults patches too.
        options = onboarding.SetupOptions(model=str(case.model), interactive=True)
        with self.assertRaisesRegex(dictation.DictationError, "did not record"):
            case.run_setup(
                options, interactive=True, mic=(False, "No sound"), typed=["1", "", "", "n"]
            )
        self.assertFalse(case.paths.config.exists())


if __name__ == "__main__":
    unittest.main()
