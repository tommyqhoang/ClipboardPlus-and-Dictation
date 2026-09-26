from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import dictation as d
import rewriting
import workflow


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        env = patch.dict(
            os.environ,
            {
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_CACHE_HOME": str(root / "cache"),
                "XDG_RUNTIME_DIR": str(root / "runtime"),
                "DICTATION_NOTIFY": str(root / "missing-notifier"),
            },
        )
        env.start()
        self.addCleanup(env.stop)
        self.paths = d.Paths()
        self.config = d.Config(self.paths)
        self.config.values.update(
            rewrite_model="test-local-model",
            rewrite_endpoint="http://127.0.0.1:1/v1/chat/completions",
        )

    def server(self):
        self.response = {
            "choices": [{"message": {"content": "Shorter text."}, "finish_reason": "stop"}]
        }
        self.requests = []
        self.code = 200
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                owner.requests.append(
                    (json.loads(self.rfile.read(int(self.headers["Content-Length"]))), self.headers)
                )
                self.send_response(owner.code)
                self.send_header("Location", "/should-not-follow")
                self.end_headers()
                self.wfile.write(
                    owner.response
                    if isinstance(owner.response, bytes)
                    else json.dumps(owner.response).encode()
                )

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.config.values["rewrite_endpoint"] = (
            f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
        )

    def run_action(self, action):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            rewriting.run(self.config, self.paths, action)
        return output.getvalue()

    def test_review_copy_and_original_preservation(self):
        self.server()
        d.atomic(self.paths.text, "Original words, please keep them.")
        with (
            patch.dict(os.environ, {"DICTATION_REWRITE_API_KEY": "test-secret"}),
            patch.object(d.subprocess, "run") as clipboard,
        ):
            output = self.run_action("concise")
            clipboard.assert_not_called()
        self.assertIn("Shorter text", output)
        self.assertIn("Original words", output)
        self.assertEqual(self.paths.text.read_text(), "Original words, please keep them.")
        body, headers = self.requests[0]
        self.assertEqual(body["messages"][1]["content"], self.paths.text.read_text())
        self.assertFalse(body["stream"])
        self.assertEqual(headers["Authorization"], "Bearer test-secret")
        self.assertEqual(len(self.requests), 1)
        self.assertIn("Shorter text", self.run_action("review"))
        with patch.object(d.subprocess, "run") as copied:
            self.run_action("copy")
            self.assertEqual(copied.call_args.kwargs["input"], b"Shorter text.")
        d.atomic(self.paths.text, "A different recording")
        with self.assertRaisesRegex(d.DictationError, "No concise draft"):
            self.run_action("copy")

    def test_no_network_without_explicit_safe_configuration(self):
        for url in (
            "",
            "http://[bad",
            "http://localhost:no/",
            "ftp://localhost/path",
            "https://user:pass@example.com",
            "https://example.com?secret=x",
            "https://example.com#secret",
            "http://example.com",
            "https://example.com",
        ):
            self.config.values["rewrite_endpoint"] = url
            with patch.object(rewriting.urllib.request, "build_opener") as network:
                with self.assertRaises(d.DictationError):
                    rewriting.request(self.config, "Original")
                network.assert_not_called()
        self.config.values["rewrite_endpoint"] = "http://localhost:11434/v1/chat/completions"
        for model, text in (("", "words"), ("model", ""), ("model", "x" * (rewriting.LIMIT + 1))):
            self.config.values["rewrite_model"] = model
            with self.assertRaises(d.DictationError):
                rewriting.request(self.config, text)

    def test_invalid_responses_redirects_and_failure_preserve_original(self):
        self.server()
        d.atomic(self.paths.text, "Unchanged original")
        for response in (
            b"invalid",
            b"x" * (1024 * 1024 + 1),
            {},
            [],
            {"choices": []},
            {"choices": [{"message": {"content": ""}}]},
            {"choices": [{"message": {"content": 123}}]},
            {"choices": [{"message": {"content": "x" * (rewriting.LIMIT + 1)}}]},
            {"choices": [{"message": {"content": "partial"}, "finish_reason": "length"}]},
        ):
            self.response = response
            with self.assertRaises(d.DictationError):
                self.run_action("concise")
            self.assertEqual(self.paths.text.read_text(), "Unchanged original")
            self.assertFalse((self.paths.cache / "concise.json").exists())
            self.assertFalse(d.busy(self.paths))
        for code in (302, 401, 429):
            self.code = code
            before = len(self.requests)
            with self.assertRaisesRegex(d.DictationError, f"HTTP {code}"):
                self.run_action("concise")
            self.assertEqual(len(self.requests), before + 1)
        with patch.object(rewriting.urllib.request, "build_opener") as build:
            build.return_value.open.side_effect = TimeoutError("private data")
            with self.assertRaisesRegex(d.DictationError, "original kept") as error:
                rewriting.request(self.config, "words")
            self.assertNotIn("private data", str(error.exception))

    def test_missing_large_and_busy_transcript(self):
        with self.assertRaisesRegex(d.DictationError, "No saved transcript"):
            self.run_action("review")
        d.atomic(self.paths.text, "x" * (rewriting.LIMIT + 1))
        with self.assertRaisesRegex(d.DictationError, "too large"):
            self.run_action("review")
        for filename in ("command.lock", "session.lock"):
            fd = d.lock(self.paths.runtime / filename)
            try:
                with self.assertRaises(d.DictationError):
                    self.run_action("concise")
            finally:
                os.close(fd)

    def test_status_monitor_live_and_recovery(self):
        d.atomic(self.paths.state, json.dumps({"phase": "recording", "started_at": 100}))
        d.atomic(self.paths.preview, "Draft\x1b\x00text")
        d.atomic(self.paths.text, "Original text")
        self.paths.audio.write_bytes(b"audio")
        with (
            patch.object(d, "busy", side_effect=[True, False]),
            patch.object(workflow.time, "time", return_value=165),
            patch.object(workflow.time, "sleep"),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            workflow.watch(self.paths)
        value = output.getvalue()
        self.assertIn("[RECORDING] 01:05", value)
        self.assertIn("[INTERRUPTED]", value)
        self.assertIn("Drafttext", value)
        self.assertIn("--transcribe", value)
        self.assertNotIn("\x1b", value)
        self.assertIn("Last saved transcript", value)

    def test_monitor_idle_and_repeated_states(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            workflow.watch(self.paths)
        self.assertIn("[IDLE]", output.getvalue())
        d.atomic(self.paths.preview, "draft")
        with (
            patch.object(d, "busy", side_effect=[True, True, False]),
            patch.object(workflow.time, "sleep"),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            workflow.watch(self.paths)
        self.assertEqual(output.getvalue().count("[IDLE]"), 1)
        self.assertEqual(output.getvalue().count("Live draft"), 1)

    def test_cli_review_reports_missing_draft(self):
        d.atomic(self.paths.text, "original")
        for flag in ("--review", "--copy-concise", "--concise"):
            result = subprocess.run(
                [sys.executable, str(Path(d.__file__)), flag], capture_output=True, text=True
            )
            self.assertEqual(result.returncode, 1)
            self.assertNotIn("Traceback", result.stderr)

    def test_setup_local_preserves_transcription_settings_and_sends_nothing(self):
        d.private_dir(self.paths.config.parent)
        d.atomic(self.paths.config, json.dumps({"prompt": "special vocabulary", "live": True}))
        self.config.values["rewrite_endpoint"] = ""
        with (
            patch("builtins.input", side_effect=["", "my-local-model", ""]),
            patch.object(rewriting.urllib.request, "build_opener") as network,
        ):
            self.run_action("setup")
        saved = d.read_json(self.paths.config)
        self.assertEqual(saved["rewrite_endpoint"], "http://localhost:11434/v1/chat/completions")
        self.assertEqual(saved["rewrite_model"], "my-local-model")
        self.assertFalse(saved["rewrite_allow_remote"])
        self.assertTrue(saved["live"])
        self.assertEqual(saved["prompt"], "special vocabulary")
        network.assert_not_called()

    def test_setup_remote_requires_consent_and_never_stores_key_values(self):
        with patch(
            "builtins.input",
            side_effect=["https://example.com/v1/chat/completions", "text-model", ""],
        ):
            self.assertIn("unchanged", self.run_action("setup"))
        self.assertFalse(self.paths.config.exists())
        with (
            patch(
                "builtins.input",
                side_effect=[
                    "https://example.com/v1/chat/completions",
                    "text-model",
                    "y",
                    "MY_TEXT_KEY",
                ],
            ),
            patch.dict(os.environ, {"MY_TEXT_KEY": "never-save-this-value"}),
        ):
            self.run_action("setup")
        self.assertTrue(d.read_json(self.paths.config)["rewrite_allow_remote"])
        self.assertNotIn("never-save-this-value", self.paths.config.read_text())
        before = self.paths.config.read_bytes()
        for answers in (
            ["http://[broken", "model"],
            ["http://localhost/path", "model", "sk-not-an-environment-name"],
        ):
            with patch("builtins.input", side_effect=answers):
                with self.assertRaises(d.DictationError):
                    self.run_action("setup")
            self.assertEqual(self.paths.config.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
