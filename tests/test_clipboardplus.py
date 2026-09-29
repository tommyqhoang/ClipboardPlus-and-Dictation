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


class FakeKeyring:
    """The slice of the `keyring` package the app uses, kept in a dict."""

    def __init__(self, broken: bool = False) -> None:
        self.items: dict[tuple[str, str], str] = {}
        self.broken = broken

    def set_password(self, service, name, value):
        if self.broken:
            raise RuntimeError("No recommended backend was available.")
        self.items[(service, name)] = value

    def get_password(self, service, name):
        if self.broken:
            raise RuntimeError("No recommended backend was available.")
        return self.items.get((service, name))

    def delete_password(self, service, name):
        if self.broken:
            raise RuntimeError("No recommended backend was available.")
        del self.items[(service, name)]


class KeychainTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name) / "config"
        self.keyring = FakeKeyring()
        patcher = patch.object(cp, "_keyring", side_effect=lambda: self.keyring)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_key_lives_in_the_keychain_and_the_file_is_only_a_marker(self):
        cp.save_key(self.folder, KEY)
        self.assertEqual(list(self.keyring.items.values()), [KEY])
        self.assertEqual(cp.key_path(self.folder).read_text(), cp.KEYRING_MARKER)
        self.assertNotIn(KEY, cp.key_path(self.folder).read_text())
        self.assertEqual(cp.read_key(self.folder), KEY)
        self.assertTrue(cp.linked(self.folder))
        cp.remove_key(self.folder)
        self.assertEqual(self.keyring.items, {})
        self.assertFalse(cp.linked(self.folder))
        self.assertFalse(cp.key_path(self.folder).exists())

    def test_a_plaintext_key_from_an_older_version_is_migrated_once_and_wiped(self):
        self.folder.mkdir(parents=True)
        cp.key_path(self.folder).write_text(KEY + "\n")
        self.assertEqual(cp.read_key(self.folder), KEY)
        self.assertEqual(list(self.keyring.items.values()), [KEY])
        self.assertEqual(cp.key_path(self.folder).read_text(), cp.KEYRING_MARKER)
        self.assertEqual(cp.read_key(self.folder), KEY)  # Now read from the keychain.
        leftovers = [p for p in self.folder.iterdir() if p.name != cp.key_path(self.folder).name]
        self.assertEqual(leftovers, [])

    def test_a_missing_keychain_entry_means_not_linked(self):
        cp.save_key(self.folder, KEY)
        self.keyring.items.clear()
        self.assertEqual(cp.read_key(self.folder), "")

    def test_a_keychain_that_fails_falls_back_to_a_private_file(self):
        self.keyring.broken = True
        cp.save_key(self.folder, KEY)
        self.assertEqual(cp.key_path(self.folder).read_text(), KEY)
        self.assertEqual(cp.read_key(self.folder), KEY)
        if sys.platform != "win32":
            self.assertEqual(stat.S_IMODE(cp.key_path(self.folder).stat().st_mode), 0o600)
        cp.remove_key(self.folder)  # A broken keychain must not stop removal.
        self.assertFalse(cp.key_path(self.folder).exists())

    def test_a_failed_migration_keeps_the_file_and_tightens_it(self):
        self.keyring.broken = True
        self.folder.mkdir(parents=True)
        path = cp.key_path(self.folder)
        path.write_text(KEY)
        if sys.platform != "win32":
            path.chmod(0o644)
        self.assertEqual(cp.read_key(self.folder), KEY)
        self.assertEqual(path.read_text(), KEY)
        if sys.platform != "win32":
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_keychain_is_off_under_the_test_runner_and_by_switch(self):
        patch.stopall()
        self.assertIsNone(cp._keyring())  # unittest is loaded and no test opt-in is set.
        with patch.dict(
            os.environ, {"CLIPBOARDPLUS_KEYRING_TESTS": "1", "CLIPBOARDPLUS_KEYRING": "0"}
        ):
            self.assertIsNone(cp._keyring())

    @unittest.skipIf(sys.platform == "win32", "POSIX permission bits")
    def test_the_fallback_file_is_never_group_or_world_readable_even_with_umask_zero(self):
        self.keyring.broken = True
        previous = os.umask(0)
        try:
            cp.save_key(self.folder, KEY)
        finally:
            os.umask(previous)
        self.assertEqual(stat.S_IMODE(cp.key_path(self.folder).stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.folder.stat().st_mode), 0o700)


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
