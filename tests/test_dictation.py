from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import dictation as d

ROOT = Path(__file__).resolve().parents[1]


class DictationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("DICTATION_")}
        self.env.update(
            {
                "XDG_CONFIG_HOME": str(self.root / "config"),
                "XDG_CACHE_HOME": str(self.root / "cache"),
                "XDG_RUNTIME_DIR": str(self.root / "runtime"),
                "DICTATION_TEST_ROOT": str(self.root),
                "DICTATION_AUDIO_BACKEND": "alsa",
                "DICTATION_CLIPBOARD_BACKEND": "wayland",
                "DICTATION_OVERLAY": "0",  # No pill or app window pops up during these tests.
            }
        )
        for name, variable in (
            ("arecord", "ARECORD"),
            ("whisper", "WHISPER_BIN"),
            ("clipboard", "WL_COPY"),
            ("notify", "NOTIFY"),
        ):
            tool = self.root / (name + ".py")
            shutil.copyfile(ROOT / "tests/fixtures/audio-tool", tool)
            tool.chmod(0o755)
            self.env["DICTATION_" + variable] = str(tool)
        model = self.root / "model.bin"
        model.write_bytes(b"model")
        self.env["DICTATION_MODEL"] = str(model)
        self.env["DICTATION_MAX_SECONDS"] = "8"
        self.patch = patch.dict(os.environ, self.env, clear=True)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.paths = d.Paths()
        self.config = d.Config(self.paths)
        self.addCleanup(self.stop_worker)

    def stop_worker(self):
        if d.busy(self.paths):
            current = d.read_json(self.paths.state)
            d.atomic(
                self.paths.control, json.dumps({"token": current.get("token"), "action": "cancel"})
            )
            deadline = time.monotonic() + 12
            while d.busy(self.paths) and time.monotonic() < deadline:
                time.sleep(0.05)
        self.assertFalse(d.busy(self.paths), "Worker leaked after test")

    def test_new_dictation_settings_enable_the_visible_switches(self):
        self.assertTrue(d.DEFAULTS["auto_paste"])
        self.assertTrue(d.DEFAULTS["overlay"])
        self.assertTrue(d.DEFAULTS["live"])

    def test_windows_read_retries_a_briefly_locked_state_file(self):
        state = self.paths.state
        state.write_text('{"phase":"recording"}')
        with (
            patch.object(d.sys, "platform", "win32"),
            patch.object(
                Path, "read_text", side_effect=[PermissionError(), '{"phase":"recording"}']
            ),
            patch.object(d.time, "sleep") as sleep,
        ):
            self.assertEqual(d.read_json(state)["phase"], "recording")
        sleep.assert_called_once_with(0.01)

    def cli(self, *args, ok=True):
        result = subprocess.run(
            [sys.executable, str(ROOT / "lib/dictation.py"), *args],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=30,  # --full runs distro installs beside this suite.
        )
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def wait_phase(self, phase):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            state = d.read_json(self.paths.state)
            if state.get("phase") == phase and (phase == "recording" or not d.busy(self.paths)):
                return state
            time.sleep(0.03)
        self.fail(f"Expected {phase}, got {d.read_json(self.paths.state)}")

    def start(self):
        self.cli()
        self.wait_phase("recording")

    def test_lifecycle_and_copy_last(self):
        d.atomic(self.paths.cache / "concise.json", '{"text":"old draft"}')
        self.start()
        begin = time.monotonic()
        self.cli()
        self.wait_phase("idle")
        self.assertLess(time.monotonic() - begin, 3)
        self.assertEqual(self.paths.text.read_text(), "Hello\n\nworld")
        self.assertFalse(self.paths.audio.exists())
        self.assertFalse((self.paths.cache / "concise.json").exists())
        self.cli("--copy-last")
        self.assertEqual((self.root / "clipboard.txt").read_text(), self.paths.text.read_text())

    def test_foreground_record_and_start_do_not_toggle_existing_session(self):
        self.env["DICTATION_MAX_SECONDS"] = "1"
        result = self.cli("--record")
        self.assertIn("[RECORDING]", result.stdout)
        self.assertIn("Last saved transcript", result.stdout)
        self.assertFalse(d.busy(self.paths))
        self.env["DICTATION_MAX_SECONDS"] = "8"
        self.start()
        d.dispatch(self.config, self.paths, "start")
        self.assertEqual(d.read_json(self.paths.state)["phase"], "recording")
        self.cli("--cancel")
        self.wait_phase("idle")

    def test_failure_retains_audio_and_retry(self):
        (self.root / "fail").touch()
        self.paths.text.write_text("previous")
        self.start()
        self.cli()
        self.wait_phase("error")
        self.assertEqual(self.paths.text.read_text(), "previous")
        self.assertTrue(self.paths.audio.exists())
        self.assertNotEqual(self.cli(ok=False).returncode, 0)
        (self.root / "fail").unlink()
        self.cli("--transcribe")
        self.assertFalse(self.paths.audio.exists())

    def test_a_press_during_a_long_action_says_so_but_a_double_press_does_not(self):
        held = d.lock(self.paths.runtime / "command.lock")
        self.addCleanup(os.close, held)
        began = self.paths.runtime / "command-started"
        began.write_text("x")
        with patch.object(d, "notify") as told:
            d.dispatch(self.config, self.paths, "toggle")  # Just started: a double press.
            told.assert_not_called()
            old = time.time() - 30
            os.utime(began, (old, old))
            d.dispatch(self.config, self.paths, "toggle")
            self.assertIn("Still finishing", told.call_args.args[1])

    def test_session_notifications_follow_the_pill_and_the_setting(self):
        with (
            patch.object(d.desktop, "available", return_value=True),
            patch.object(d.subprocess, "run") as run,
        ):
            self.config.values.update(overlay=True, notifications=False)
            d.notify(self.config, "Recording.", session=True)
            run.assert_not_called()  # The pill already says it.
            d.notify(self.config, "Microphone could not start.")
            self.assertEqual(run.call_count, 1)  # Errors always reach the user.
            self.config.values.update(notifications=True)
            d.notify(self.config, "Recording.", session=True)
            self.assertEqual(run.call_count, 2)
            self.config.values.update(overlay=False, notifications=False)
            d.notify(self.config, "Recording.", session=True)
            self.assertEqual(run.call_count, 3)  # No pill: notifications are the feedback.

    def test_discarding_a_failed_recording_clears_its_error(self):
        (self.root / "fail").touch()
        self.start()
        self.cli()
        self.wait_phase("error")
        d.dispatch(self.config, self.paths, "discard")
        self.assertFalse(self.paths.audio.exists())
        self.assertEqual(d.read_json(self.paths.state)["phase"], "idle")

    def test_finishing_always_says_it_is_ready_to_paste(self):
        self.paths.audio.write_bytes(b"\0" * 3200)
        with (
            patch.object(d, "transcribe", return_value="hello"),
            patch.object(d, "notify") as told,
        ):
            self.assertEqual(d.finish(self.config, self.paths), "copied")
        self.assertIn("Ready to paste", told.call_args.args[1])
        self.paths.audio.write_bytes(b"\0" * 3200)
        with patch.object(d, "transcribe", return_value=""), patch.object(d, "notify") as told:
            self.assertEqual(d.finish(self.config, self.paths), "empty")
        self.assertIn("No speech", told.call_args.args[1])

    def test_auto_paste_respects_the_setting_and_retry_never_pastes(self):
        with (
            patch.object(d, "transcribe", return_value="hello"),
            patch.object(d, "copy_text"),
            patch.object(d, "record_transcript"),
            patch.object(d, "paste_text", return_value=True) as paste,
            patch.object(d, "notify") as told,
        ):
            for enabled, foreground, expected in (
                (False, True, False),
                (True, False, False),
                (True, True, True),
            ):
                self.paths.audio.write_bytes(b"audio")
                self.config.values["auto_paste"] = enabled
                paste.reset_mock()
                d.finish(self.config, self.paths, auto_paste=foreground)
                self.assertEqual(paste.called, expected)
            self.assertIn("Pasted.", told.call_args.args[1])
            self.paths.audio.write_bytes(b"audio")
            self.assertEqual(d.finish(self.config, self.paths, auto_paste=True), "pasted")
            paste.return_value = False
            self.paths.audio.write_bytes(b"audio")
            self.assertEqual(d.finish(self.config, self.paths, auto_paste=True), "copied")
            self.assertIn("Ctrl+V", told.call_args.args[1])  # Says how to finish by hand.

    def test_paste_uses_platform_helper_and_never_falls_back_across_wayland(self):
        with (
            patch.object(d.desktop, "platform_name", return_value="linux"),
            patch.dict(os.environ, {"WAYLAND_DISPLAY": "wayland-0"}),
            patch.object(d.subprocess, "run", side_effect=FileNotFoundError) as run,
        ):
            self.assertFalse(d.paste_text(self.config))
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0][0], "wtype")

    def test_messages_speak_in_app_terms(self):
        with (
            patch.object(d.desktop, "available", return_value=True),
            # notify-send takes the message as its last argument (Windows reads stdin).
            patch.object(d.desktop, "platform_name", return_value="linux"),
            patch.object(d.subprocess, "run") as run,
        ):
            d.notify(None, "Settings unreadable")  # Unreadable settings still notify.
        self.assertEqual(run.call_args.args[0][-1], "Settings unreadable")
        (self.root / "copy-fail").touch()
        with self.assertRaisesRegex(d.DictationError, "Couldn’t put that on the clipboard"):
            d.copy_text(self.config, self.paths, "history item")
        self.config.values["model"] = str(self.root / "no-model.bin")  # Setup unfinished.
        with patch.object(d, "open_app") as window:
            with self.assertRaisesRegex(d.DictationError, "finish setup"):
                d.dispatch(self.config, self.paths, "toggle")
        window.assert_called_once_with(self.config)
        self.config.values["model"] = self.env["DICTATION_MODEL"]
        self.paths.audio.write_bytes(b"\0" * 3200)
        with patch.object(d, "open_app") as window:
            with self.assertRaisesRegex(d.DictationError, "wasn’t transcribed yet"):
                d.dispatch(self.config, self.paths, "toggle")
        window.assert_called_once_with(self.config)

    def test_busy_does_not_overwrite_audio(self):
        (self.root / "slow").touch()
        self.start()
        self.cli()
        # A fixed sleep raced the worker: wait until the stop is picked up and the
        # slow transcription is running, when a new press must be told it is busy.
        deadline = time.monotonic() + 10
        while d.read_json(self.paths.state).get("phase") != "transcribing":
            if time.monotonic() > deadline:
                self.fail(f"Expected transcribing, got {d.read_json(self.paths.state)}")
            time.sleep(0.03)
        before = self.paths.audio.read_bytes()
        self.assertIn("busy", self.cli().stdout)
        self.assertEqual(before, self.paths.audio.read_bytes())
        self.wait_phase("idle")

    def test_cancel(self):
        self.start()
        self.cli("--cancel")
        self.wait_phase("idle")
        self.assertFalse(self.paths.audio.exists())
        self.assertFalse(self.paths.text.exists())

    def test_ffmpeg_graceful_stop(self):
        tool = self.root / "ffmpeg.py"
        shutil.copyfile(ROOT / "tests/fixtures/audio-tool", tool)
        self.env.update(DICTATION_AUDIO_BACKEND="avfoundation", DICTATION_FFMPEG=str(tool))
        self.start()
        begin = time.monotonic()
        self.cli()
        self.wait_phase("idle")
        self.assertLess(time.monotonic() - begin, 3)
        self.assertTrue(self.paths.text.exists())

    def test_recorder_shutdown_handles_exit_race_and_hung_process(self):
        recorder = Mock(stdin=None)
        recorder.poll.return_value = None
        recorder.terminate.side_effect = ProcessLookupError("already exited")
        recorder.wait.side_effect = [subprocess.TimeoutExpired("recorder", 1), None]
        d.stop_recorder(recorder)
        recorder.kill.assert_called_once()
        self.assertEqual(recorder.wait.call_count, 2)

        hung = Mock(stdin=None)
        hung.poll.return_value = None
        hung.wait.side_effect = subprocess.TimeoutExpired("recorder", 1)
        d.stop_recorder(hung)
        self.assertEqual(hung.wait.call_count, 2)  # Never wait forever for a bad driver.

    def test_live_preview_and_final(self):
        self.env["DICTATION_LIVE"] = "1"
        self.env["DICTATION_LIVE_INTERVAL"] = "1"
        self.start()
        deadline = time.monotonic() + 5
        while not self.paths.preview.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(self.paths.preview.exists())
        self.assertFalse((self.root / "clipboard.txt").exists())
        self.cli()
        self.wait_phase("idle")
        self.assertFalse(self.paths.preview.exists())
        self.assertTrue(self.paths.text.exists())

    def test_start_failure_and_stale_state(self):
        (self.root / "recorder-fail").touch()
        started = self.cli(ok=False)  # The worker can fail before or after startup returns.
        if started.returncode:
            self.assertIn("Microphone could not start", started.stderr)
        self.assertRegex(
            self.wait_phase("error")["message"],
            "Microphone could not start|The microphone stopped",
        )
        d.atomic(self.paths.state, '{"phase":"recording","token":"old"}')
        self.assertEqual(json.loads(self.cli("--status").stdout)["phase"], "interrupted")

    def test_copy_failure_recoverable(self):
        (self.root / "copy-fail").touch()
        self.paths.audio.write_bytes(b"\x10\x00" * 5000)
        with self.assertRaises(d.DictationError):
            d.finish(self.config, self.paths)
        self.assertTrue(self.paths.audio.exists())
        self.assertTrue(self.paths.text.exists())

    def test_linux_auto_clipboard_uses_x11_without_wayland(self):
        self.config.values["clipboard_backend"] = "auto"
        with (
            patch.object(d.desktop, "platform_name", return_value="linux"),
            patch.dict(os.environ, {"DISPLAY": ":7"}, clear=True),
            patch.object(d.subprocess, "run") as run,
        ):
            d.copy_text(self.config, self.paths, "Hello X11")
            self.assertEqual(
                run.call_args.args[0],
                ["xclip", "-selection", "clipboard", "-in", "-t", "UTF8_STRING"],
            )
            self.assertEqual(run.call_args.kwargs["input"], b"Hello X11")

    def test_linux_image_copy_uses_x11_without_wayland(self):
        image = self.root / "saved.png"
        image.write_bytes(b"PNG fixture")
        run = Mock()
        self.config.values["clipboard_backend"] = "auto"
        with (
            patch.object(d.desktop, "platform_name", return_value="linux"),
            patch.dict(os.environ, {"DISPLAY": ":7"}, clear=True),
        ):
            d.desktop.copy_image(self.config.values, image, run=run)
            self.assertEqual(
                run.call_args.args[0],
                ["xclip", "-selection", "clipboard", "-in", "-t", "image/png"],
            )
            self.assertEqual(run.call_args.kwargs["input"], b"PNG fixture")
            self.assertEqual(run.call_args.kwargs["stdout"], subprocess.DEVNULL)
            self.assertEqual(run.call_args.kwargs["stderr"], subprocess.DEVNULL)

    def test_explicit_x11_image_copy_honors_backend_on_mixed_desktop(self):
        image = self.root / "saved.png"
        image.write_bytes(b"PNG fixture")
        run = Mock()
        self.config.values["clipboard_backend"] = "x11"
        with (
            patch.object(d.desktop, "platform_name", return_value="linux"),
            patch.dict(os.environ, {"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":7"}),
        ):
            d.desktop.copy_image(self.config.values, image, run=run)
        self.assertEqual(run.call_args.args[0][0], "xclip")

    def test_macos_image_path_is_passed_as_data_to_applescript(self):
        image = self.root / 'image " quoted.png'
        run = Mock()
        with (
            patch.object(d.desktop, "platform_name", return_value="macos"),
        ):
            d.desktop.copy_image(self.config.values, image, run=run)
            command = run.call_args.args[0]
            self.assertNotIn(str(image), command[2])
            self.assertEqual(command[3], str(image))

    def test_live_preview_uses_short_independent_timeout(self):
        self.config.values.update(backend="http", live=True, live_interval=1, timeout=120)
        token = "preview-limit"
        fd = d.lock(self.paths.runtime / "session.lock")
        self.assertIsNotNone(fd)
        preview_started = threading.Event()
        observed = []

        def fake_transcribe(_config, _pcm, _cache, *, timeout=None):
            observed.append(timeout)
            preview_started.set()
            return "draft" if timeout is not None else "final"

        def stop_after_preview():
            self.assertTrue(preview_started.wait(5))
            d.atomic(self.paths.control, json.dumps({"token": token, "action": "stop"}))

        stopper = threading.Thread(target=stop_after_preview, daemon=True)
        stopper.start()
        previous_term, previous_int = (
            signal.getsignal(signal.SIGTERM),
            signal.getsignal(signal.SIGINT),
        )
        try:
            with (
                patch.object(d, "transcribe", side_effect=fake_transcribe),
                patch.object(d, "start_overlay", return_value=None),
                patch.object(d, "notify"),
                patch.object(d, "copy_text"),
                patch.object(d, "record_transcript"),
                patch.object(d, "paste_text", return_value=False),
            ):
                d.worker(self.config, self.paths, fd, token)
        finally:
            signal.signal(signal.SIGTERM, previous_term)
            signal.signal(signal.SIGINT, previous_int)
            stopper.join(timeout=1)
        self.assertIn(5, observed)
        self.assertIn(None, observed)

    def test_cancel_does_not_wait_for_full_remote_preview_timeout(self):
        request_received = threading.Event()
        release_request = threading.Event()

        class SlowPreview(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                request_received.set()
                release_request.wait(8)  # Watchdog: the old 120 s behavior fails quickly.

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), SlowPreview)
        serving = threading.Thread(target=server.serve_forever, daemon=True)
        serving.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.addCleanup(release_request.set)
        self.config.values.update(
            backend="http",
            endpoint=f"http://127.0.0.1:{server.server_port}/transcribe",
            live=True,
            live_interval=1,
            timeout=120,
        )
        token = "slow-preview"
        fd = d.lock(self.paths.runtime / "session.lock")
        self.assertIsNotNone(fd)
        cancelled_at = []

        def cancel_during_preview():
            if request_received.wait(5):
                cancelled_at.append(time.monotonic())
                d.atomic(self.paths.control, json.dumps({"token": token, "action": "cancel"}))

        stopper = threading.Thread(target=cancel_during_preview, daemon=True)
        stopper.start()
        previous_term = signal.getsignal(signal.SIGTERM)
        previous_int = signal.getsignal(signal.SIGINT)
        try:
            with patch.object(d, "start_overlay", return_value=None), patch.object(d, "notify"):
                d.worker(self.config, self.paths, fd, token)
        finally:
            signal.signal(signal.SIGTERM, previous_term)
            signal.signal(signal.SIGINT, previous_int)
            release_request.set()
            stopper.join(timeout=1)
        self.assertTrue(cancelled_at, "The preview request never started")
        self.assertLess(time.monotonic() - cancelled_at[0], 7)
        self.assertEqual(d.read_json(self.paths.state)["result"], "cancelled")

    def test_cleaning_preserves_real_words(self):
        self.assertEqual(d.clean_text("music"), "music")
        self.assertEqual(d.clean_text("[BLANK_AUDIO]"), "")
        self.assertEqual(d.clean_text("one new line two"), "one\ntwo")
        self.assertEqual(d.clean_text("new line", False), "new line")

    def test_config_validation_and_explicit_model(self):
        for invalid in ({"threads": -1}, {"live": "yes"}, {"unknown": 1}, {"backend": "other"}):
            d.private_dir(self.paths.config.parent)
            d.atomic(self.paths.config, json.dumps(invalid))
            with self.assertRaises(d.DictationError):
                d.Config(self.paths)
        d.atomic(self.paths.config, "{}")
        self.config.values["model"] = "/missing/model"
        with self.assertRaises(d.DictationError):
            self.config.check()

    def test_zero_threads_means_automatic(self):
        # 0 picks half the cores, bounded: small machines get one, huge ones eight.
        d.private_dir(self.paths.config.parent)
        d.atomic(self.paths.config, json.dumps({"threads": 0}))
        config = d.Config(self.paths)
        self.assertTrue(1 <= config.n("threads") <= 8)
        self.assertLessEqual(config.n("threads"), max(1, (os.cpu_count() or 4) // 2))

    def test_remote_requires_opt_in(self):
        self.config.values.update(backend="http", endpoint="https://example.org/transcribe")
        with self.assertRaises(d.DictationError):
            self.config.check()
        self.config.values["allow_remote"] = True
        self.config.check()
        self.config.values["endpoint"] = "http://example.org/transcribe"
        with self.assertRaises(d.DictationError):
            self.config.check()

    def test_http_multipart_errors_and_redirect(self):
        captured = []
        authorizations = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(handler):
                authorizations.append(handler.headers.get("Authorization"))
                captured.append(handler.rfile.read(int(handler.headers["Content-Length"])))
                if handler.path == "/redirect":
                    handler.send_response(302)
                    handler.send_header("Location", "/success")
                    handler.end_headers()
                else:
                    handler.send_response(200)
                    handler.end_headers()
                    handler.wfile.write(
                        b'{"text":"HTTP transcript"}' if handler.path == "/success" else b"{}"
                    )

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.config.values.update(backend="http", api_model="custom-model")
        for path in ("success", "invalid", "redirect"):
            self.config.values["endpoint"] = f"http://127.0.0.1:{server.server_port}/{path}"
            self.config.check()
            if path == "success":
                # A key saved by desktop setup is used when the variable is unset.
                d.private_dir(self.paths.config.parent)
                d.atomic(d.key_file(self.paths), "saved-key\n")
                with patch.dict(os.environ, {self.config.s("api_key_env"): ""}):
                    self.assertEqual(
                        d.transcribe(self.config, b"\x01\x00" * 5000, self.paths.cache),
                        "HTTP transcript",
                    )
                self.assertEqual(authorizations[-1], "Bearer saved-key")
                d.key_file(self.paths).unlink()
                self.assertIn(b"custom-model", captured[-1])
                self.assertIn(b"RIFF", captured[-1])
            else:
                with self.assertRaises(d.DictationError):
                    d.transcribe(self.config, b"\x01\x00" * 5000, self.paths.cache)
        self.assertEqual(len(captured), 3)

    def test_lock_release_and_permissions(self):
        fd = d.lock(self.paths.runtime / "session.lock")
        self.assertTrue(d.busy(self.paths))
        os.close(fd)
        self.assertFalse(d.busy(self.paths))
        d.atomic(self.paths.text, "private")
        if sys.platform != "win32":
            self.assertEqual(self.paths.text.stat().st_mode & 0o777, 0o600)

    def test_silence_and_config_creation(self):
        self.assertEqual(d.transcribe(self.config, b"\x00" * 5000, self.paths.cache), "")
        self.cli("--init-config")
        self.assertTrue(self.paths.config.exists())
        self.assertNotEqual(self.cli("--init-config", ok=False).returncode, 0)
        self.cli("--doctor")
        self.paths.audio.write_bytes(b"sample")
        self.cli("--discard")
        self.assertFalse(self.paths.audio.exists())

    def test_keep_audio_requires_explicit_discard(self):
        self.env["DICTATION_KEEP_AUDIO"] = "true"
        self.start()
        self.cli()
        self.wait_phase("idle")
        self.assertTrue(self.paths.audio.exists())
        self.assertNotEqual(self.cli(ok=False).returncode, 0)
        self.cli("--discard")
        self.start()
        self.cli("--cancel")
        self.wait_phase("idle")

    def test_maximum_duration_stops_automatically(self):
        self.env["DICTATION_MAX_SECONDS"] = "1"
        self.start()
        self.wait_phase("idle")
        self.assertTrue(self.paths.text.exists())

    def test_noise_preserves_clipboard_and_text(self):
        (self.root / "result").write_text("[BLANK_AUDIO]")
        self.paths.text.write_text("previous")
        (self.root / "clipboard.txt").write_text("previous clipboard")
        self.start()
        self.cli()
        self.wait_phase("idle")
        self.assertEqual(self.paths.text.read_text(), "previous")
        self.assertEqual((self.root / "clipboard.txt").read_text(), "previous clipboard")

    def test_timeout_and_model_arguments(self):
        self.config.values.update(prompt="PostgreSQL", vad_model="/tmp/vad.bin")
        from unittest.mock import Mock

        with patch.object(
            d.subprocess, "run", return_value=Mock(returncode=0, stdout=b"hello")
        ) as run:
            self.assertEqual(
                d.transcribe(self.config, b"\x01\x00" * 5000, self.paths.cache), "hello"
            )
            self.assertIn("--vad", run.call_args.args[0])
            self.assertIn("PostgreSQL", run.call_args.args[0])
        with patch.object(d.subprocess, "run", side_effect=subprocess.TimeoutExpired("whisper", 1)):
            with self.assertRaises(d.DictationError):
                d.transcribe(self.config, b"\x01\x00" * 5000, self.paths.cache)

    def test_dependency_and_url_validation(self):
        for update in ({"arecord": "/missing/recorder"}, {"vad_model": "/missing/vad"}):
            saved = self.config.values.copy()
            self.config.values.update(update)
            with self.assertRaises(d.DictationError):
                self.config.check(recording=True)
            self.config.values = saved
        self.config.values["backend"] = "http"
        for endpoint in (
            "",
            "ftp://localhost/file",
            "http://[invalid",
            "http://localhost:invalid/inference",
            "http://user:pass@localhost/inference",
            "http://localhost/inference?token=secret",
        ):
            self.config.values["endpoint"] = endpoint
            with self.assertRaises(d.DictationError):
                self.config.check()
        self.config.values.update(
            endpoint="http://localhost/inference", api_key_env="not an env name"
        )
        with self.assertRaisesRegex(d.DictationError, "environment variable NAME"):
            self.config.check()

    def test_bad_config_type_and_no_audio(self):
        d.private_dir(self.paths.config.parent)
        d.atomic(self.paths.config, '{"threads": "four"}')
        with self.assertRaises(d.DictationError):
            d.Config(self.paths)
        with self.assertRaises(d.DictationError):
            d.finish(self.config, self.paths)
        with self.assertRaises(d.DictationError):
            d.copy_text(self.config, self.paths)

    def test_private_directory_and_notification_timeout(self):
        if sys.platform != "win32":
            directory = self.root / "linked"
            directory.symlink_to(self.paths.cache, target_is_directory=True)
            with self.assertRaises(d.DictationError):
                d.private_dir(directory)
        with patch.object(d.subprocess, "run", side_effect=subprocess.TimeoutExpired("notify", 2)):
            d.notify(self.config, "recording")

    def test_http_response_boundaries(self):
        import urllib.error
        from unittest.mock import Mock

        self.config.values.update(
            backend="http",
            endpoint="http://localhost/inference",
            api_model="custom",
            prompt="vocabulary",
            language="auto",
        )
        for body in (b"not json", b"x" * (1024 * 1024 + 1), b'{"text": 123}'):
            response = Mock()
            response.read.return_value = body
            opened = Mock()
            opened.__enter__ = Mock(return_value=response)
            opened.__exit__ = Mock(return_value=False)
            opener = Mock()
            opener.open.return_value = opened
            with patch.object(d.urllib.request, "build_opener", return_value=opener):
                with self.assertRaises(d.DictationError):
                    d.transcribe(self.config, b"\x01\x00" * 5000, self.paths.cache)
        with patch.object(d.urllib.request, "build_opener") as build:
            build.return_value.open.side_effect = urllib.error.URLError("private error")
            with self.assertRaisesRegex(d.DictationError, "unavailable"):
                d.transcribe(self.config, b"\x01\x00" * 5000, self.paths.cache)

    def test_watch_and_preview_notification(self):
        self.env.update(
            DICTATION_LIVE="1",
            DICTATION_LIVE_INTERVAL="1",
            DICTATION_PREVIEW_NOTIFICATIONS="1",
            DICTATION_MAX_SECONDS="3",
        )
        self.start()
        watched = self.cli("--watch")
        self.assertIn("Hello", watched.stdout)
        self.assertIn("Last saved transcript", watched.stdout)
        self.wait_phase("idle")

    def test_invalid_json_settings_and_environment(self):
        d.private_dir(self.paths.config.parent)
        for raw in ("[]", "{"):
            d.atomic(self.paths.config, raw)
            with self.assertRaises(d.DictationError):
                d.Config(self.paths)
        d.atomic(self.paths.config, "{}")
        for key, value in (("DICTATION_LIVE", "invalid"), ("DICTATION_WHISPER_THREADS", "oops")):
            with patch.dict(os.environ, {key: value}):
                with self.assertRaises(d.DictationError):
                    d.Config(self.paths)

    def test_busy_discard_and_cancel_idle(self):
        self.cli("--cancel")
        self.start()
        self.assertNotEqual(self.cli("--discard", ok=False).returncode, 0)
        self.assertTrue(d.busy(self.paths))
        self.cli("--cancel")
        self.wait_phase("idle")

    @unittest.skipIf(sys.platform == "win32", "Debian shell installer")
    def test_installer_preserves_other_gnome_bindings(self):
        script = r"""
source "$1/install.sh"
gsettings() {
  if [[ "$1" == get ]]; then
    printf "%s\n" "['/existing/']"
  elif [[ "$1" == set && "$3" == custom-keybindings ]]; then
    printf "%s" "$4"
  fi
}
install_gnome_shortcut
"""
        result = subprocess.run(
            ["bash", "-c", script, "test", str(ROOT)], capture_output=True, text=True, check=True
        )
        self.assertIn("['/existing/', '/org/gnome/", result.stdout)
        self.assertNotIn("/existing/''", result.stdout)

    @unittest.skipIf(sys.platform == "win32", "Debian shell installer")
    def test_source_fallback_is_version_and_commit_pinned(self):
        script = r"""
source "$1/install.sh"
HOME="$2"
git() {
  if [[ "$1" == init ]]; then
    mkdir -p "${!#}/.git"
  elif [[ "$3" == fetch ]]; then
    printf '%s\n' "$*" >"$HOME/fetch-arguments"
  elif [[ "$3" == rev-parse ]]; then
    printf '%s\n' "$WHISPER_COMMIT"
  fi
}
cmake() {
  if [[ "$1" == --build ]]; then
    mkdir -p "$2/bin"
    : >"$2/bin/whisper-cli"
    chmod +x "$2/bin/whisper-cli"
  fi
}
nproc() { printf '1\n'; }
install_whisper_from_source
"""
        home = self.root / "source-install"
        result = subprocess.run(
            ["bash", "-c", script, "test", str(ROOT), str(home)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        fetch = (home / "fetch-arguments").read_text()
        self.assertIn("ggml-org/whisper.cpp.git", fetch)
        self.assertRegex(fetch, r"\b[0-9a-f]{40}\b")  # the pinned commit, not a movable tag
        self.assertTrue((home / ".local/opt/whisper.cpp-v1.8.7/build/bin/whisper-cli").exists())

    @unittest.skipIf(sys.platform == "win32", "Debian shell installer")
    def test_model_download_staging(self):
        script = r"""
source "$1/install.sh"
MODEL_DIR="$2"
MODEL_DEST="$2/ggml-test.bin"
MODEL_LINK="$2/selected.bin"
MODEL_NAME=ggml-test.bin
MODEL_URL=https://example.invalid/model
ALLOW_UNVERIFIED_MODEL=1
download_mode="$3"
curl() {
  local target=""
  while (($#)); do
    if [[ "$1" == --output ]]; then target="$2"; break; fi
    shift
  done
  if [[ "$download_mode" == invalid ]]; then
    printf 'html error' >"$target"
  else
    printf 'lmggfixture' >"$target"
  fi
}
install_model
"""
        # A complete valid fixture is promoted; the selected model points at it.
        model_dir = self.root / "download"
        result = subprocess.run(
            ["bash", "-c", script, "test", str(ROOT), str(model_dir), "valid"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((model_dir / "selected.bin").read_bytes(), b"lmggfixture")
        self.assertFalse((model_dir / "ggml-test.bin.part").exists())
        invalid_dir = self.root / "invalid-download"
        failed = subprocess.run(
            ["bash", "-c", script, "test", str(ROOT), str(invalid_dir), "invalid"],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(failed.returncode, 0)
        self.assertFalse((invalid_dir / "ggml-test.bin").exists())
        self.assertFalse((invalid_dir / "selected.bin").exists())


class WorkerRelaunchFrozenTests(unittest.TestCase):
    def test_dispatch_relaunches_a_sibling_binary_when_frozen(self):
        config = Mock(
            check=Mock(),
            b=Mock(return_value=True),
        )
        with tempfile.TemporaryDirectory() as folder:
            paths = Mock(
                audio=Mock(exists=Mock(return_value=False)),
                state=Path(folder) / "does-not-matter-state.json",
                runtime=Path(folder),
            )
            process = Mock()
            process.poll.return_value = 0  # "exited immediately" — only the argv matters here.
            with (
                patch.object(d, "lock", return_value=1),
                patch.object(d, "read_json", return_value={}),
                patch.object(d.os, "close"),
                patch.object(d.desktop, "frozen_root", return_value=Path("/opt/Clipboard+")),
                patch.object(d.desktop, "platform_name", return_value="linux"),
                patch.object(d.subprocess, "Popen", return_value=process) as popen,
            ):
                with self.assertRaises(d.DictationError):
                    d.dispatch(config, paths, "start")
        args = popen.call_args.args[0]
        self.assertEqual(args[0], str(Path("/opt/Clipboard+") / "dictation"))
        self.assertEqual(args[1], "--worker")


class OpenAppAndOverlayFrozenTests(unittest.TestCase):
    def test_open_app_launches_even_when_the_source_script_is_missing_and_frozen(self):
        config = Mock(b=Mock(return_value=True))
        with (
            patch.object(d.desktop, "frozen_root", return_value=Path("/opt/Clipboard+")),
            patch.object(d.desktop, "relaunch", return_value=["/opt/Clipboard+/app"]),
            patch.object(Path, "is_file", return_value=False),
            patch.object(d.subprocess, "Popen") as popen,
        ):
            d.open_app(config)
        popen.assert_called_once()
        self.assertEqual(popen.call_args.args[0], ["/opt/Clipboard+/app"])

    def test_open_app_does_nothing_from_source_when_the_script_is_missing(self):
        config = Mock(b=Mock(return_value=True))
        with (
            patch.object(d.desktop, "frozen_root", return_value=None),
            patch.object(Path, "is_file", return_value=False),
            patch.object(d.subprocess, "Popen") as popen,
        ):
            d.open_app(config)
        popen.assert_not_called()

    def test_start_overlay_launches_even_when_the_source_script_is_missing_and_frozen(self):
        token = "worker-token"
        config = Mock(b=Mock(return_value=True))
        with (
            patch.object(d.desktop, "frozen_root", return_value=Path("/opt/Clipboard+")),
            patch.object(d.desktop, "relaunch", return_value=["/opt/Clipboard+/overlay", token]),
            patch.object(Path, "is_file", return_value=False),
            patch.object(d.subprocess, "Popen") as popen,
        ):
            result = d.start_overlay(config, token)
        popen.assert_called_once()
        self.assertEqual(popen.call_args.args[0], ["/opt/Clipboard+/overlay", token])
        self.assertIsNotNone(result)


class WhisperBinDefaultTests(unittest.TestCase):
    def test_default_whisper_bin_prefers_a_bundled_binary_when_frozen(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        bundled = root / "whisper-cli"
        bundled.write_text("")
        env = {
            "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_CACHE_HOME": str(root / "cache"),
            "XDG_RUNTIME_DIR": str(root / "runtime"),
        }
        with (
            patch.dict(os.environ, env),
            patch.object(d.shutil, "which", return_value=None),
            patch.object(d.desktop, "frozen_root", return_value=root),
            patch.object(d.desktop, "platform_name", return_value="linux"),
        ):
            config = d.Config(d.Paths())
        self.assertEqual(config.s("whisper_bin"), str(bundled))


if __name__ == "__main__":
    unittest.main()
