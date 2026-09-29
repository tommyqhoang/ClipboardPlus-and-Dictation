"""The window-independent operations behind the app (lib/app_service.py)."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import app_service
import cues
import desktop
import dictation as d
import hotkeys
import permissions
from test_app import ServiceCase


class SettingsTests(ServiceCase):
    def test_options_are_saved_at_once_and_only_known_ones(self):
        for key in ("overlay", "live", "auto_paste", "notifications"):
            self.service.set_option(key, False)
            self.assertIs(d.read_json(self.paths.config)[key], False)
        self.service.set_option("overlay", True)
        self.assertIs(d.read_json(self.paths.config)["overlay"], True)
        with self.assertRaises(ValueError):
            self.service.set_option("backend", True)

    def test_sound_cues_are_saved_and_validated(self):
        for mode in cues.MODES:
            self.service.set_sounds(mode)
            self.assertEqual(d.read_json(self.paths.config)["sounds"], mode)
            self.assertEqual(d.Config(self.paths).s("sounds"), mode)
        with self.assertRaises(ValueError):
            self.service.set_sounds("loud")

    def test_voice_is_saved_but_not_mid_recording(self):
        self.service.set_voice("auto", " USB Mic ")
        saved = d.read_json(self.paths.config)
        self.assertEqual((saved["language"], saved["device"]), ("auto", "USB Mic"))
        for language, device in (("fr", "x"), ("en", "  ")):
            with self.assertRaises(d.DictationError):
                self.service.set_voice(language, device)
        with patch.object(d, "busy", return_value=True), self.assertRaises(d.DictationError):
            self.service.set_voice("en", "x")
        with (
            patch.object(desktop, "platform_name", return_value="windows"),
            self.assertRaisesRegex(d.DictationError, "pick the microphone"),
        ):
            self.service.set_voice("en", "default")

    def test_saving_one_setting_keeps_the_others(self):
        self.service.set_voice("auto", "USB Mic")
        self.service.set_option("live", False)
        self.service.set_sounds("all")
        saved = d.read_json(self.paths.config)
        self.assertEqual(
            (saved["language"], saved["live"], saved["sounds"]), ("auto", False, "all")
        )


class SetupStateTests(ServiceCase):
    def test_a_clipboard_only_setup_needs_no_model(self):
        hotkeys.Preferences(self.paths).save(features=hotkeys.Features(False, True))
        self.assertTrue(self.service.ready())

    def test_dictation_needs_a_model_and_a_marker_to_be_complete(self):
        hotkeys.Preferences(self.paths).save(features=hotkeys.Features(True, False))
        self.assertFalse(self.service.ready())
        self.assertFalse(self.service.completed())
        self.service.complete()
        self.assertFalse(self.service.completed())  # Marked, but still not ready.
        with patch.object(d.Config, "check"):
            self.assertTrue(self.service.completed())

    def test_a_broken_settings_file_is_not_ready_rather_than_a_crash(self):
        d.private_dir(self.paths.config.parent)
        self.paths.config.write_text("{not json", encoding="utf-8")
        hotkeys.Preferences(self.paths).save(features=hotkeys.Features(True, False))
        self.assertFalse(self.service.ready())


class PreparingTests(ServiceCase):
    def test_a_download_is_checked_first_and_a_failure_changes_nothing(self):
        with (
            patch.object(
                app_service.onboarding,
                "preflight_download",
                side_effect=d.DictationError("You appear to be offline"),
            ),
            patch.object(app_service.onboarding, "download_model") as download,
            self.assertRaisesRegex(d.DictationError, "offline"),
        ):
            self.service.prepare("en", "USB Mic", "")
        download.assert_not_called()
        self.assertFalse(self.paths.config.exists())

    def test_a_model_file_needs_no_network_check(self):
        model = self.folder / "m.bin"
        model.write_bytes(b"lmgg-fixture")
        with (
            patch.object(app_service.onboarding, "preflight_download") as preflight,
            patch.object(d.Config, "check"),
        ):
            self.service.prepare("en", "USB Mic", str(model))
        preflight.assert_not_called()

    def test_progress_and_pause_reach_the_download(self):
        model = self.folder / "m.bin"
        model.write_bytes(b"lmgg-fixture")
        progress, pause = MagicMock(), MagicMock()
        with (
            patch.object(app_service.onboarding, "download_model", return_value=model) as download,
            patch.object(d.Config, "check"),
        ):
            self.service.prepare("en", "USB Mic", "", progress, pause=pause)
        self.assertEqual(download.call_args.args[2:], (progress, pause))

    def test_the_wrong_file_is_refused(self):
        wrong = self.folder / "wrong.bin"
        wrong.write_bytes(b"not a model")
        with self.assertRaisesRegex(d.DictationError, "not a whisper.cpp GGML"):
            self.service.prepare("en", "USB Mic", str(wrong))


class MicrophoneListTests(ServiceCase):
    def listing(self, output, code=0, backend="alsa"):
        values = dict(d.Config(self.paths).values, audio_backend=backend)
        result = subprocess.CompletedProcess([], code, stdout=output.encode(), stderr=b"")
        return (
            patch.object(app_service.d, "Config", return_value=SimpleNamespace(values=values)),
            patch.object(app_service.subprocess, "run", return_value=result),
        )

    def test_alsa_offers_the_default_first_and_hides_the_noise(self):
        text = (
            "null\n    Discard all samples\ndefault\n    Default device\n"
            "pipewire\n    PipeWire Sound Server\nsurround51:CARD=X\n    5.1\n"
            "plughw:CARD=USB,DEV=0\n    USB Mic, USB Audio\n"
        )
        first, second = self.listing(text)
        with first, second:
            devices = self.service.microphones()
        self.assertEqual(devices, ["default", "pipewire", "plughw:CARD=USB,DEV=0"])
        self.assertEqual(self.service.microphone_names["default"], "System default (recommended)")
        self.assertEqual(
            self.service.microphone_names["plughw:CARD=USB,DEV=0"], "USB Mic, USB Audio"
        )

    def test_windows_and_macos_listings(self):
        first, second = self.listing(
            '[dshow] "Headset Mic" (audio)\n"Webcam" (video)\n', backend="dshow"
        )
        with first, second:
            self.assertEqual(self.service.microphones(), ["Headset Mic"])
        first, second = self.listing(
            "AVFoundation video devices:\n[0] Camera\nAVFoundation audio devices:\n[0] MacBook Mic\n",
            backend="avfoundation",
        )
        with first, second:
            self.assertEqual(self.service.microphones(), ["MacBook Mic"])

    def test_no_microphone_points_at_the_permission_for_this_system(self):
        first, second = self.listing("", backend="dshow")
        with first, second, patch.object(permissions, "session_kind", return_value="windows"):
            with self.assertRaises(d.DictationError) as caught:
                self.service.microphones()
        self.assertIn("No microphones found", str(caught.exception))
        self.assertIn("ms-settings:privacy-microphone", str(caught.exception))

    def test_a_failed_alsa_listing_and_a_missing_tool(self):
        first, second = self.listing("", code=1)
        with first, second, self.assertRaisesRegex(d.DictationError, "discovery failed"):
            self.service.microphones()
        with (
            patch.object(app_service.subprocess, "run", side_effect=FileNotFoundError),
            self.assertRaisesRegex(d.DictationError, "Couldn’t check microphones"),
        ):
            self.service.microphones()


class PermissionHelpTests(ServiceCase):
    def test_linux_has_no_settings_pages_to_send_you_to(self):
        with patch.object(desktop, "platform_name", return_value="linux"):
            self.assertEqual(self.service.permission_help(), [])

    def test_macos_lists_only_what_is_missing(self):
        with (
            patch.object(desktop, "platform_name", return_value="macos"),
            patch.object(permissions, "accessibility_trusted", return_value=False),
            patch.object(permissions, "microphone_status", return_value="denied"),
        ):
            found = self.service.permission_help()
        self.assertEqual(
            [title for title, _, _ in found], ["Allow Accessibility", "Allow the microphone"]
        )
        self.assertEqual(found[0][2], permissions.ACCESSIBILITY_URL)
        self.assertEqual(found[1][2], permissions.MAC_MICROPHONE_URL)
        with (
            patch.object(desktop, "platform_name", return_value="macos"),
            patch.object(permissions, "accessibility_trusted", return_value=True),
            patch.object(permissions, "microphone_status", return_value="authorized"),
        ):
            self.assertEqual(self.service.permission_help(), [])


class MicrophoneTestTests(ServiceCase):
    def make(self, levels):
        process = MagicMock()
        process.poll.return_value = None
        process.stdout.read.side_effect = levels + [b""]
        with patch.object(app_service.subprocess, "Popen", return_value=process):
            return app_service.MicrophoneTest(self.paths, "default", seconds=0.2), process

    def test_verdicts_follow_how_loud_it_was(self):
        loud = b"\x00\x40" * 800
        quiet = b"\x02\x00" * 800
        for chunks, outcome in (
            ([], "none"),
            ([quiet] * 3, "quiet"),
            ([b"\xc8\x00" * 800] * 3, "faint"),
            ([loud] * 3, "good"),
        ):
            test, _ = self.make(list(chunks))
            for _ in range(100):
                if test.readings >= len(chunks):
                    break
                import time

                time.sleep(0.01)
            outcome_now = test.outcome()
            self.assertEqual(outcome_now, outcome)
            self.assertTrue(test.verdict())
            test.stop()

    def test_stopping_ends_the_process_and_survives_a_stuck_one(self):
        test, process = self.make([])
        test.stop()
        process.terminate.assert_called()
        process.stdout.close.assert_called()
        stuck, process = self.make([])
        process.wait.side_effect = subprocess.TimeoutExpired("arecord", 2)
        stuck.stop()  # Must not raise.
        process.kill.assert_called()

    def test_it_will_not_start_over_a_recording_or_without_the_tool(self):
        with patch.object(d, "busy", return_value=True), self.assertRaises(d.DictationError):
            app_service.MicrophoneTest(self.paths, "default")
        with (
            patch.object(app_service.subprocess, "Popen", side_effect=OSError),
            self.assertRaisesRegex(d.DictationError, "could not be opened"),
        ):
            app_service.MicrophoneTest(self.paths, "default")


class ActionTests(ServiceCase):
    def test_copy_uses_the_saved_transcript(self):
        d.atomic(self.paths.text, "hello")
        with patch.object(d, "copy_text") as copy:
            self.service.action("copy")
        copy.assert_called_once()

    def test_other_actions_go_to_the_dictation_dispatcher(self):
        with patch.object(d, "dispatch") as dispatch:
            self.service.action("toggle")
        self.assertEqual(dispatch.call_args.args[2], "toggle")

    def test_concise_returns_the_saved_draft(self):
        d.private_dir(self.paths.cache)
        d.atomic(self.paths.cache / "concise.json", json.dumps({"text": "short"}))
        with patch("rewriting.run") as run:
            self.assertEqual(self.service.concise(), "short")
        run.assert_called_once()


class AccountStateTests(ServiceCase):
    def test_an_unlinked_account_is_disconnected(self):
        self.assertEqual(self.service.clipboard_plus_state(), app_service.Account("disconnected"))

    def test_a_linked_account_reports_its_sync_and_asks_to_reconnect_on_auth(self):
        report = {"sync": "ok", "synced": 12.5}
        with (
            patch.object(self.service, "clipboard_plus_linked", return_value=True),
            patch.object(self.service, "clipboard_plus_email", return_value="me@example.com"),
            patch.object(app_service.clipservice, "read_status", return_value=report),
        ):
            state = self.service.clipboard_plus_state()
            self.assertEqual(
                (state.kind, state.email, state.sync, state.synced),
                ("connected", "me@example.com", "ok", 12.5),
            )
            report["sync"] = "auth"
            self.assertEqual(self.service.clipboard_plus_state().kind, "reconnect")

    def test_sync_now_leaves_a_request_for_the_service(self):
        self.service.sync_clipboard_now()
        self.assertTrue((self.paths.runtime / app_service.clipservice.SYNC_NOW).exists())


if __name__ == "__main__":
    unittest.main()
