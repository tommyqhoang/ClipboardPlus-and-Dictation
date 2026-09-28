"""Clipboard+ account link: key storage, request contract and failure isolation."""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
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

    def test_the_account_email_is_kept_privately_beside_the_key_and_leaves_with_it(self):
        self.assertEqual(cp.read_email(self.folder), "")
        cp.save_key(self.folder, KEY)
        cp.save_email(self.folder, "  Me@Example.com \n")
        self.assertEqual(cp.read_email(self.folder), "Me@Example.com")
        if sys.platform != "win32":
            mode = stat.S_IMODE(os.stat(cp.email_path(self.folder)).st_mode)
            self.assertEqual(mode, 0o600)
        cp.remove_key(self.folder)
        self.assertEqual(cp.read_email(self.folder), "")
        self.assertFalse(cp.email_path(self.folder).exists())

    def test_an_unusable_email_is_not_kept(self):
        for bad in ("", "no-at-sign", "a@b\nc.d", "x" * 300 + "@b.co", "a b@c.de"):
            with self.subTest(bad=bad):
                cp.save_email(self.folder, bad)
                self.assertEqual(cp.read_email(self.folder), "")
        cp.email_path(self.folder).write_text("tampered\x00@x.y")
        self.assertEqual(cp.read_email(self.folder), "")

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


class CloudParsingTests(unittest.TestCase):
    def test_malformed_timestamps_are_skipped_without_stopping_a_pull(self):
        valid = {
            "id": "12345678-1234-1234-1234-123456789abc",
            "type": "text",
            "content": "safe",
            "ts": 1_790_424_000_123,
        }
        for bad in (True, 0, -1, float("nan"), float("inf"), 10**1000):
            with self.subTest(timestamp=bad):
                self.assertIsNone(cp._cloud_item({**valid, "ts": bad}))
                self.assertIsNone(cp._removed({"type": "text", "ts": bad}))
        self.assertEqual(cp._cloud_item(valid).created_ms, valid["ts"])
        self.assertEqual(cp._removed({"type": "text", "ts": valid["ts"]}).created_ms, valid["ts"])


class DictationUploadTests(unittest.TestCase):
    """Uploading is the sync engine's job now: dictation only records the transcript."""

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

    def test_the_dictation_engine_no_longer_uploads_on_its_own(self):
        self.assertFalse(hasattr(d, "share_transcript"))
        self.assertFalse(hasattr(cp, "send"))
        cp.save_key(self.paths.config.parent, KEY)
        d.private_dir(self.paths.audio.parent)
        self.paths.audio.write_bytes(b"\x00\x01" * 100)
        with (
            patch.object(cp.urllib.request, "build_opener") as build,
            patch.object(d, "transcribe", return_value="Hello"),
            patch.object(d, "copy_text"),
            patch.object(d, "notify"),
        ):
            d.finish(self.config, self.paths)
        build.assert_not_called()


if __name__ == "__main__":
    unittest.main()
