"""Concise drafts (lib/rewriting.py): endpoint checks, the request, and the review flow."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import dictation as d
import rewriting


class ChatHandler(BaseHTTPRequestHandler):
    reply: bytes = b""
    status = 200
    seen: list[dict] = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        ChatHandler.seen.append(
            {"body": json.loads(self.rfile.read(length)), "auth": self.headers.get("Authorization")}
        )
        self.send_response(ChatHandler.status)
        self.send_header("Content-Type", "application/json")
        if 300 <= ChatHandler.status < 400:
            self.send_header("Location", "http://127.0.0.1:1/elsewhere")
        self.send_header("Content-Length", str(len(ChatHandler.reply)))
        self.end_headers()
        self.wfile.write(ChatHandler.reply)

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        pass


def chat(text, finish="stop"):
    return json.dumps(
        {"choices": [{"message": {"content": text}, "finish_reason": finish}]}
    ).encode()


class RewritingCase(unittest.TestCase):
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
                "HOME": str(root / "home"),
                "DICTATION_MODEL": str(root / "model.bin"),
            },
        )
        env.start()
        self.addCleanup(env.stop)
        (root / "model.bin").write_bytes(b"lmgg")
        self.paths = d.Paths()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), ChatHandler)
        self.addCleanup(self.server.server_close)
        threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        ).start()
        self.addCleanup(self.server.shutdown)
        ChatHandler.seen = []
        ChatHandler.status = 200
        ChatHandler.reply = chat("Short.")
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1/chat/completions"
        self.write_config(rewrite_endpoint=self.url, rewrite_model="tiny")

    def write_config(self, **values):
        d.private_dir(self.paths.config.parent)
        d.atomic(self.paths.config, json.dumps(values))

    def config(self):
        return d.Config(self.paths)


class CheckTests(RewritingCase):
    def test_a_local_endpoint_with_a_model_is_fine(self):
        rewriting.check(self.config())

    def test_bad_endpoints_are_explained(self):
        cases = {
            "http://user:pw@127.0.0.1/x": "plain HTTP",
            "ftp://127.0.0.1/x": "plain HTTP",
            "http://127.0.0.1/x?key=1": "plain HTTP",
            "": "plain HTTP",
            "http://127.0.0.1:99999/x": "malformed",
        }
        for endpoint, message in cases.items():
            self.write_config(rewrite_endpoint=endpoint, rewrite_model="m")
            with self.assertRaisesRegex(d.DictationError, message, msg=endpoint):
                rewriting.check(self.config())

    def test_remote_needs_https_and_explicit_permission(self):
        self.write_config(rewrite_endpoint="https://api.example.com/v1/chat", rewrite_model="m")
        with self.assertRaisesRegex(d.DictationError, "leaves this machine"):
            rewriting.check(self.config())
        self.write_config(
            rewrite_endpoint="http://api.example.com/v1/chat",
            rewrite_model="m",
            rewrite_allow_remote=True,
        )
        with self.assertRaisesRegex(d.DictationError, "HTTPS"):
            rewriting.check(self.config())
        self.write_config(
            rewrite_endpoint="https://api.example.com/v1/chat",
            rewrite_model="m",
            rewrite_allow_remote=True,
        )
        rewriting.check(self.config())

    def test_a_model_and_a_key_variable_name_are_required(self):
        self.write_config(rewrite_endpoint=self.url, rewrite_model="  ")
        with self.assertRaisesRegex(d.DictationError, "rewrite_model"):
            rewriting.check(self.config())
        self.write_config(
            rewrite_endpoint=self.url, rewrite_model="m", rewrite_api_key_env="sk-not-a-name!"
        )
        with self.assertRaisesRegex(d.DictationError, "environment variable NAME"):
            rewriting.check(self.config())


class RequestTests(RewritingCase):
    def test_the_transcript_is_sent_as_text_to_edit(self):
        self.assertEqual(rewriting.request(self.config(), "um so like hello"), "Short.")
        sent = ChatHandler.seen[0]["body"]
        self.assertEqual(sent["model"], "tiny")
        self.assertFalse(sent["stream"])
        self.assertEqual(sent["messages"][0]["content"], rewriting.INSTRUCTION)
        self.assertEqual(sent["messages"][1], {"role": "user", "content": "um so like hello"})

    def test_the_key_comes_from_the_named_variable(self):
        with patch.dict(os.environ, {"DICTATION_REWRITE_API_KEY": "secret"}):
            rewriting.request(self.config(), "hi")
        self.assertEqual(ChatHandler.seen[0]["auth"], "Bearer secret")
        rewriting.request(self.config(), "hi")
        self.assertIsNone(ChatHandler.seen[1]["auth"])

    def test_empty_and_oversized_transcripts_are_refused_before_any_request(self):
        for text in ("   ", "x" * (rewriting.LIMIT + 1)):
            with self.assertRaisesRegex(d.DictationError, "nonempty"):
                rewriting.request(self.config(), text)
        self.assertEqual(ChatHandler.seen, [])

    def test_a_refusal_or_cutoff_keeps_the_original(self):
        ChatHandler.reply = chat("cut off", finish="length")
        with self.assertRaisesRegex(d.DictationError, "incomplete or refused"):
            rewriting.request(self.config(), "hello")

    def test_bad_replies_are_reported_not_raised_raw(self):
        for reply in (b"not json", b"{}", chat("   "), json.dumps({"choices": []}).encode()):
            ChatHandler.reply = reply
            with self.assertRaises(d.DictationError):
                rewriting.request(self.config(), "hello")

    def test_an_http_error_says_so_and_does_not_retry(self):
        ChatHandler.status = 503
        ChatHandler.reply = b"{}"
        with self.assertRaisesRegex(d.DictationError, "HTTP 503"):
            rewriting.request(self.config(), "hello")
        self.assertEqual(len(ChatHandler.seen), 1)

    def test_a_redirect_is_not_followed(self):
        ChatHandler.status = 302
        ChatHandler.reply = b"{}"
        with self.assertRaisesRegex(d.DictationError, "HTTP 302"):
            rewriting.request(self.config(), "hello")
        self.assertEqual(len(ChatHandler.seen), 1)  # The new location was never asked.

    def test_an_unreachable_endpoint_is_unavailable(self):
        self.server.shutdown()
        self.server.server_close()
        with self.assertRaisesRegex(d.DictationError, "unavailable"):
            rewriting.request(self.config(), "hello")


class RunTests(RewritingCase):
    def run_action(self, action):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rewriting.run(self.config(), self.paths, action)
        return out.getvalue()

    def test_concise_saves_a_draft_bound_to_the_transcript_and_shows_both(self):
        d.atomic(self.paths.text, "um hello there")
        shown = self.run_action("concise")
        self.assertIn("Original:", shown)
        self.assertIn("Short.", shown)
        draft = d.read_json(self.paths.cache / "concise.json")
        self.assertEqual(draft["text"], "Short.")
        self.assertEqual(self.paths.text.read_text(), "um hello there")  # Never replaced.

    def test_copy_puts_only_the_accepted_draft_on_the_clipboard(self):
        d.atomic(self.paths.text, "um hello there")
        self.run_action("concise")
        with patch.object(d, "copy_text") as copy:
            self.run_action("copy")
        self.assertEqual(copy.call_args.kwargs["text"], "Short.")

    def test_a_draft_for_an_older_transcript_is_refused(self):
        d.atomic(self.paths.text, "first")
        self.run_action("concise")
        d.atomic(self.paths.text, "second, newer")
        with self.assertRaisesRegex(d.DictationError, "No concise draft"):
            self.run_action("review")
        with self.assertRaisesRegex(d.DictationError, "No concise draft"):
            self.run_action("copy")

    def test_no_transcript_no_rewrite(self):
        with self.assertRaisesRegex(d.DictationError, "No saved transcript"):
            self.run_action("concise")

    def test_a_huge_transcript_is_refused(self):
        self.paths.text.write_text("x" * (rewriting.LIMIT + 1), encoding="utf-8")
        with self.assertRaisesRegex(d.DictationError, "too large"):
            self.run_action("concise")

    def test_it_will_not_run_beside_a_recording_or_another_command(self):
        session = d.lock(self.paths.runtime / "session.lock")
        self.assertIsNotNone(session)
        try:
            with self.assertRaisesRegex(d.DictationError, "Finish the current"):
                self.run_action("concise")
        finally:
            os.close(session)
        command = d.lock(self.paths.runtime / "command.lock")
        try:
            with self.assertRaisesRegex(d.DictationError, "Another command"):
                self.run_action("concise")
        finally:
            os.close(command)
        # Both locks are released again afterwards.
        d.atomic(self.paths.text, "hello")
        self.run_action("concise")


class SetupTests(RewritingCase):
    def setup(self, answers):
        out = io.StringIO()
        with (
            patch("builtins.input", side_effect=answers),
            contextlib.redirect_stdout(out),
        ):
            rewriting.run(self.config(), self.paths, "setup")
        return out.getvalue()

    def test_a_local_server_is_saved_without_touching_other_settings(self):
        self.write_config(language="auto", rewrite_endpoint="", rewrite_model="")
        self.setup(["http://localhost:11434/v1/chat/completions", "llama3", ""])
        saved = d.read_json(self.paths.config)
        self.assertEqual(saved["rewrite_model"], "llama3")
        self.assertEqual(saved["language"], "auto")
        self.assertFalse(saved["rewrite_allow_remote"])

    def test_a_remote_service_needs_a_yes_and_a_no_changes_nothing(self):
        self.write_config(language="en")
        out = self.setup(["https://api.example.com/v1/chat", "m", "n"])
        self.assertIn("was not enabled", out)
        self.assertNotIn("rewrite_endpoint", d.read_json(self.paths.config))
        self.setup(["https://api.example.com/v1/chat", "m", "y", "MY_KEY"])
        saved = d.read_json(self.paths.config)
        self.assertTrue(saved["rewrite_allow_remote"])
        self.assertEqual(saved["rewrite_api_key_env"], "MY_KEY")

    def test_a_key_pasted_instead_of_a_name_is_refused(self):
        self.write_config(language="en")
        with self.assertRaisesRegex(d.DictationError, "NAME"):
            self.setup(["http://localhost:1/x", "m", "sk-secret-value!"])
        self.assertNotIn("rewrite_endpoint", d.read_json(self.paths.config))

    def test_a_malformed_url_is_refused(self):
        with self.assertRaisesRegex(d.DictationError, "malformed"):
            self.setup(["http://[bad", "m"])


if __name__ == "__main__":
    unittest.main()
