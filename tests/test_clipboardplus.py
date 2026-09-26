"""Clipboard+ account link: key storage, request contract and failure isolation."""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import clipboardplus as cp
import dictation as d

KEY = "cp_live_" + "a1b2c3d4" * 6


def response(status: int) -> Mock:
    reply = Mock(status=status)
    reply.__enter__ = lambda self: self
    reply.__exit__ = lambda *args: None
    reply.read.return_value = b"{}"
    return reply


class KeyStorageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)

    def test_saved_key_is_private_and_removable(self):
        self.assertFalse(cp.linked(self.folder))
        cp.save_key(self.folder, f"  {KEY}\n")
        self.assertTrue(cp.linked(self.folder))
        self.assertEqual(cp.read_key(self.folder), KEY)
        if sys.platform != "win32":
            mode = stat.S_IMODE(os.stat(cp.key_path(self.folder)).st_mode)
            self.assertEqual(mode, 0o600)
        cp.remove_key(self.folder)
        cp.remove_key(self.folder)  # Already gone is fine.
        self.assertFalse(cp.linked(self.folder))

    def test_rejects_anything_that_is_not_a_clipboard_plus_key(self):
        for bad in ("", "abc", "sk-live-" + "x" * 40, KEY + " extra", "cp_live_ünï" + "x" * 40):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    cp.save_key(self.folder, bad)
        self.assertFalse(cp.linked(self.folder))

    def test_a_tampered_key_file_reads_as_not_linked(self):
        cp.key_path(self.folder).write_text("not a key")
        self.assertEqual(cp.read_key(self.folder), "")
        self.assertFalse(cp.linked(self.folder))


class RequestTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        cp.save_key(self.folder, KEY)

    def opener(self, *outcomes):
        opener = Mock()
        opener.open.side_effect = list(outcomes)
        patcher = patch.object(cp.urllib.request, "build_opener", return_value=opener)
        self.build = patcher.start()
        self.addCleanup(patcher.stop)
        return opener

    def test_send_posts_the_transcript_to_the_users_history(self):
        opener = self.opener(response(201))
        self.assertEqual(cp.send(self.folder, "Hello world."), "sent")
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, cp.API + "/api/clipboard")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + KEY)
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(
            json.loads(request.data),
            {"type": "text", "content": "Hello world.", "source": "Whisper Dictation & Clipboard+"},
        )

    def test_the_key_is_never_forwarded_by_a_redirect(self):
        # A real HTTP exchange: the server answers 302 to another host.
        import http.server
        import threading

        followed = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                if self.path.startswith("/elsewhere"):
                    followed.append(self.headers.get("Authorization"))
                self.send_response(302)
                self.send_header("Location", "/elsewhere")
                self.end_headers()

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        api = f"http://127.0.0.1:{server.server_port}"
        # request() refuses plain HTTP, so exercise the opener it uses directly.
        opener = cp.urllib.request.build_opener(cp.NoRedirect())
        outgoing = cp.urllib.request.Request(api + "/api/clipboard", data=b"{}", method="POST")
        with self.assertRaises(cp.urllib.error.HTTPError) as caught:
            opener.open(outgoing, timeout=5)
        caught.exception.close()
        self.assertEqual(caught.exception.code, 302)
        self.assertEqual(followed, [])

    def test_nothing_is_sent_unless_linked_or_when_empty(self):
        opener = self.opener()
        cp.remove_key(self.folder)
        self.assertEqual(cp.send(self.folder, "Hello"), "off")
        cp.save_key(self.folder, KEY)
        self.assertEqual(cp.send(self.folder, "   \n"), "off")
        opener.open.assert_not_called()

    def test_oversized_text_is_skipped_rather_than_rejected(self):
        opener = self.opener()
        self.assertEqual(cp.send(self.folder, "x" * 60_000), "failed")
        opener.open.assert_not_called()

    def test_failures_are_reported_never_raised(self):
        def http_error(code):
            return urllib.error.HTTPError(cp.API, code, "err", {}, None)

        cases = (
            (http_error(401), "rejected"),
            (http_error(403), "rejected"),
            (http_error(500), "failed"),
            (http_error(429), "failed"),
            (urllib.error.URLError("offline"), "failed"),
            (TimeoutError(), "failed"),
            (OSError("reset"), "failed"),
            (cp.http.client.IncompleteRead(b""), "failed"),
        )
        for outcome, expected in cases:
            with self.subTest(outcome=repr(outcome)):
                self.opener(outcome)
                self.assertEqual(cp.send(self.folder, "Hello"), expected)

    def test_only_https_endpoints_receive_the_key(self):
        opener = self.opener(response(201))
        self.assertEqual(cp.send(self.folder, "Hello", api="http://evil.example"), "failed")
        opener.open.assert_not_called()

    def test_verify_distinguishes_valid_invalid_and_offline(self):
        def error(code):
            return urllib.error.HTTPError(cp.API, code, "err", {}, None)

        # The probe item is invalid on purpose: 400 means the key may write.
        for outcome, expected in (
            (error(400), "ok"),
            (error(403), "read-only"),
            (error(401), "invalid"),
            (error(500), "error"),
            (response(201), "error"),
            (urllib.error.URLError("offline"), "offline"),
        ):
            with self.subTest(outcome=repr(outcome)):
                opener = self.opener(outcome)
                self.assertEqual(cp.verify(KEY), expected)
                request = opener.open.call_args.args[0]
                self.assertEqual(request.get_method(), "POST")
                self.assertEqual(json.loads(request.data), {"type": "verify"})


class EngineTests(unittest.TestCase):
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
        d.private_dir(self.paths.config.parent)
        self.config = d.Config(self.paths)

    def test_an_unlinked_account_uploads_nothing(self):
        with patch.object(cp, "send") as send:
            self.assertIsNone(d.share_transcript(self.config, "Hello"))
        send.assert_not_called()

    def test_a_linked_account_receives_the_transcript_in_the_background(self):
        cp.save_key(self.paths.config.parent, KEY)
        with patch.object(cp, "send", return_value="sent") as send:
            thread = d.share_transcript(self.config, "Hello")
            self.assertIsNotNone(thread)
            thread.join(5)
        send.assert_called_once_with(self.paths.config.parent, "Hello")

    def test_a_refused_key_is_reported_and_other_failures_are_silent(self):
        cp.save_key(self.paths.config.parent, KEY)
        for outcome, notified in (("rejected", True), ("failed", False), ("sent", False)):
            with self.subTest(outcome=outcome):
                with (
                    patch.object(cp, "send", return_value=outcome),
                    patch.object(d, "notify") as notify,
                ):
                    thread = d.share_transcript(self.config, "Hello")
                    thread.join(5)
                self.assertEqual(notify.called, notified)
                if notified:
                    self.assertIn("Settings", notify.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
