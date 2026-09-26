from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

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

    def cli(self, *args, ok=True):
        result = subprocess.run(
            [sys.executable, str(ROOT / "lib/dictation.py"), *args],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=10,
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

    def test_messages_speak_in_app_terms(self):
        with (
            patch.object(d.desktop, "available", return_value=True),
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
        time.sleep(0.15)
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
        self.cli()
        self.wait_phase("error")
        d.atomic(self.paths.state, '{"phase":"recording","token":"old"}')
        self.assertEqual(json.loads(self.cli("--status").stdout)["phase"], "interrupted")

    def test_copy_failure_recoverable(self):
        (self.root / "copy-fail").touch()
        self.paths.audio.write_bytes(b"\x10\x00" * 5000)
        with self.assertRaises(d.DictationError):
            d.finish(self.config, self.paths)
        self.assertTrue(self.paths.audio.exists())
        self.assertTrue(self.paths.text.exists())

    def test_cleaning_preserves_real_words(self):
        self.assertEqual(d.clean_text("music"), "music")
        self.assertEqual(d.clean_text("[BLANK_AUDIO]"), "")
        self.assertEqual(d.clean_text("one new line two"), "one\ntwo")
        self.assertEqual(d.clean_text("new line", False), "new line")

    def test_config_validation_and_explicit_model(self):
        for invalid in ({"threads": 0}, {"live": "yes"}, {"unknown": 1}, {"backend": "other"}):
            d.private_dir(self.paths.config.parent)
            d.atomic(self.paths.config, json.dumps(invalid))
            with self.assertRaises(d.DictationError):
                d.Config(self.paths)
        d.atomic(self.paths.config, "{}")
        self.config.values["model"] = "/missing/model"
        with self.assertRaises(d.DictationError):
            self.config.check()

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
  if [[ "$1" == clone ]]; then
    printf '%s\n' "$*" >"$HOME/clone-arguments"
    destination="${!#}"
    mkdir -p "$destination/.git"
  else
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
        clone = (home / "clone-arguments").read_text()
        self.assertIn("--branch v1.8.7", clone)
        self.assertIn("ggml-org/whisper.cpp.git", clone)
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


if __name__ == "__main__":
    unittest.main()
