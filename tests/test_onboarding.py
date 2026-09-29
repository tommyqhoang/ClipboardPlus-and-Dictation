from __future__ import annotations

import contextlib
import errno
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import dictation
import onboarding


class Response(io.BytesIO):
    """Minimal HTTP response: a readable body plus headers."""

    def __init__(self, data: bytes, length: bool = True):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))} if length else {}


class OnboardingTests(unittest.TestCase):
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
            },
        )
        env.start()
        self.addCleanup(env.stop)
        self.paths = dictation.Paths()
        self.model = root / "custom.bin"
        self.model.write_bytes(b"lmgg-test")

    def wizard(self, answers, platform="linux"):
        with (
            patch(
                "builtins.input", side_effect=[*answers, ""]
            ),  # The last answer is the consent step.
            patch.object(onboarding.desktop, "platform_name", return_value=platform),
            patch.object(onboarding.subprocess, "run") as runner,
            patch.object(dictation.Config, "check"),
            patch.object(onboarding, "is_interactive", return_value=True),
            patch.object(onboarding, "preflight_download"),
            patch.object(onboarding, "check_microphone", return_value=(True, "ok")),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            onboarding.run(self.paths)
        return runner, output.getvalue()

    def test_custom_model_and_platform_walkthroughs(self):
        for platform in ("linux", "macos", "windows"):
            self.paths.config.unlink(missing_ok=True)
            runner, output = self.wizard(["2", str(self.model), "USB Mic", "Codex", "y"], platform)
            saved = json.loads(self.paths.config.read_text())
            self.assertEqual(saved["language"], "auto")
            self.assertEqual(saved["device"], "USB Mic")
            self.assertEqual(saved["model"], str(self.model.resolve()))
            self.assertTrue(saved["live"])
            self.assertFalse(saved["allow_remote"])
            # Device enumeration only (Windows also runs `icacls` on the settings folder).
            listing = [c for c in runner.call_args_list if c.args[0][0] != "icacls"]
            self.assertEqual(len(listing), 1)
            args = listing[0].args[0]
            self.assertTrue("-L" in args or "-list_devices" in args)
            self.assertIn("Nothing has been recorded", output)

    def test_download_default_and_existing_settings_preserved(self):
        # Desktop installation creates blank defaults; first-run setup must not
        # mistake them for a completed configuration and default to cancelling.
        dictation.private_dir(self.paths.config.parent)
        dictation.atomic(self.paths.config, json.dumps(dictation.DEFAULTS))
        with patch.object(onboarding, "download_model", return_value=self.model) as download:
            self.wizard(["", "", "", "", ""])
            self.assertEqual(download.call_args.args[1], "en")
        before = self.paths.config.read_bytes()
        self.wizard([""])
        self.assertEqual(self.paths.config.read_bytes(), before)
        self.wizard(["y", "1", str(self.model), "", "", ""])

    def test_invalid_inputs_do_not_write_settings(self):
        for answers, platform in (
            (["3"], "linux"),
            (["1", "/missing/model"], "linux"),
            (["1", str(self.model), ""], "windows"),
        ):
            with self.assertRaises(dictation.DictationError):
                self.wizard(answers, platform)
            self.assertFalse(self.paths.config.exists())
        invalid_model = Path(self.temp.name) / "not-a-model.bin"
        invalid_model.write_bytes(b"not a GGML file")
        with self.assertRaisesRegex(dictation.DictationError, "not a whisper.cpp GGML"):
            self.wizard(["1", str(invalid_model)])
        self.assertFalse(self.paths.config.exists())
        with patch.object(dictation, "busy", return_value=True):
            with self.assertRaises(dictation.DictationError):
                self.wizard([])

    def test_verified_download_reuse_and_mismatch(self):
        data = b"lmgg-model-test"
        folder = Path(self.temp.name) / "models"
        with (
            patch.dict(onboarding.MODELS, {"en": ("base.en", hashlib.sha256(data).hexdigest())}),
            patch.object(onboarding, "open_url", return_value=Response(data)) as request,
        ):
            progress = []
            path = onboarding.download_model(folder, "en", lambda *step: progress.append(step))
            self.assertEqual(path.read_bytes(), data)
            self.assertEqual(progress, [(len(data), len(data))])
            self.assertEqual(onboarding.download_model(folder, "en"), path)
            self.assertEqual(request.call_count, 1)
            path.write_bytes(b"corrupt")
            request.return_value = Response(data)
            # A damaged model is kept aside and downloaded again, not a dead end.
            self.assertEqual(onboarding.download_model(folder, "en").read_bytes(), data)
            damaged = path.with_name(path.name + ".damaged")
            self.assertEqual(damaged.read_bytes(), b"corrupt")
            path.unlink()
            damaged.unlink()
        with patch.object(onboarding, "open_url", return_value=Response(b"bad", length=False)):
            with self.assertRaises(dictation.DictationError):
                onboarding.download_model(folder, "en")
        self.assertEqual(list(folder.glob("*.part")), [])

    def test_download_failures_say_what_to_do(self):
        folder = Path(self.temp.name) / "models"
        offline = urllib.error.URLError("Name or service not known")
        with patch.object(onboarding, "open_url", side_effect=offline):
            with self.assertRaisesRegex(dictation.DictationError, "internet connection"):
                onboarding.download_model(folder, "auto")
        full = OSError(errno.ENOSPC, "No space left on device")
        with patch.object(onboarding, "open_url", side_effect=full):
            with self.assertRaisesRegex(dictation.DictationError, "disk space"):
                onboarding.download_model(folder, "auto")
        self.assertEqual(list(folder.glob("*.part")), [])

    def test_a_dropped_connection_keeps_the_partial_to_resume(self):
        folder = Path(self.temp.name) / "models"
        with patch.object(onboarding, "open_url", side_effect=ConnectionResetError("reset")):
            with self.assertRaisesRegex(dictation.DictationError, "resume"):
                onboarding.download_model(folder, "auto")
        self.assertEqual(len(list(folder.glob("*.part"))), 1)

    def test_download_removes_only_stale_partials(self):
        folder = Path(self.temp.name) / "models"
        folder.mkdir()
        stale, active = folder / "ggml-old.bin.part", folder / "ggml-new.bin.part"
        stale.write_bytes(b"")
        active.write_bytes(b"")
        old = stale.stat().st_mtime - onboarding.STALE_PARTIAL_SECONDS - 1
        os.utime(stale, (old, old))
        with patch.object(onboarding, "open_url", side_effect=OSError("offline")):
            with self.assertRaises(dictation.DictationError):
                onboarding.download_model(folder, "en")
        self.assertFalse(stale.exists())
        self.assertTrue(active.exists())

    def test_pause_preserves_bytes_and_resume_requests_validated_range(self):
        data = b"lmgg" + b"a" * (1024 * 1024 + 20)
        folder = Path(self.temp.name) / "models"
        pause = threading.Event()
        with (
            patch.dict(onboarding.MODELS, {"en": ("base.en", hashlib.sha256(data).hexdigest())}),
            patch.object(onboarding, "open_url", return_value=Response(data)),
        ):
            with self.assertRaises(onboarding.DownloadPaused):
                onboarding.download_model(folder, "en", lambda *_: pause.set(), pause)
        partial = folder / "ggml-base.en.bin.part"
        prefix = partial.read_bytes()
        self.assertEqual(prefix, data[: 1024 * 1024])
        response = Response(data[len(prefix) :])
        response.status = 206
        response.headers["Content-Range"] = f"bytes {len(prefix)}-{len(data) - 1}/{len(data)}"
        with (
            patch.dict(onboarding.MODELS, {"en": ("base.en", hashlib.sha256(data).hexdigest())}),
            patch.object(onboarding, "open_url", return_value=response) as request,
        ):
            self.assertEqual(onboarding.download_model(folder, "en").read_bytes(), data)
        self.assertEqual(request.call_args.args[0].get_header("Range"), f"bytes={len(prefix)}-")
        self.assertFalse(partial.exists())

    def test_resume_restarts_when_range_is_ignored_and_rejects_wrong_range(self):
        data = b"lmgg-complete"
        folder = Path(self.temp.name) / "models"
        folder.mkdir()
        partial = folder / "ggml-base.en.bin.part"
        partial.write_bytes(data[:4])
        with (
            patch.dict(onboarding.MODELS, {"en": ("base.en", hashlib.sha256(data).hexdigest())}),
            patch.object(onboarding, "open_url", return_value=Response(data)),
        ):
            destination = onboarding.download_model(folder, "en")
            self.assertEqual(destination.read_bytes(), data)
        destination.unlink()
        partial.write_bytes(data[:4])
        response = Response(data[4:])
        response.status = 206
        response.headers["Content-Range"] = f"bytes 3-{len(data) - 1}/{len(data)}"
        with patch.object(onboarding, "open_url", return_value=response):
            with self.assertRaisesRegex(dictation.DictationError, "invalid resume"):
                onboarding.download_model(folder, "en")
        self.assertFalse(partial.exists())

    def test_connect_skips_unreachable_address_family_quickly(self):
        v6 = (onboarding.socket.AF_INET6, onboarding.socket.SOCK_STREAM, 6, "", ("::1", 443, 0, 0))
        v4 = (onboarding.socket.AF_INET, onboarding.socket.SOCK_STREAM, 6, "", ("1.2.3.4", 443))
        attempts = []

        class Socket:
            def __init__(self, family, kind, proto):
                self.family = family

            def settimeout(self, value):
                attempts.append((self.family, value))

            def bind(self, address):
                attempts.append(("bind", address))

            def connect(self, target):
                if self.family == onboarding.socket.AF_INET6:
                    raise TimeoutError("unreachable")

            def close(self):
                attempts.append((self.family, "closed"))

        with (
            patch.object(onboarding.socket, "getaddrinfo", return_value=[v6, v6, v4]),
            patch.object(onboarding.socket, "socket", Socket),
        ):
            sock = onboarding.connect(("example.org", 443), 60, ("0.0.0.0", 0))
        self.assertEqual(sock.family, onboarding.socket.AF_INET)
        # The IPv4 address is tried second, not after every IPv6 address.
        self.assertEqual(
            [entry for entry in attempts if entry[1] == onboarding.CONNECT_ATTEMPT_SECONDS],
            [(v6[0], 4.0), (v4[0], 4.0)],
        )
        self.assertIn((v4[0], 60.0), attempts)
        self.assertIn((v6[0], "closed"), attempts)
        with (
            patch.object(onboarding.socket, "getaddrinfo", return_value=[v6]),
            patch.object(onboarding.socket, "socket", Socket),
        ):
            with self.assertRaises(TimeoutError):
                onboarding.connect(("example.org", 443))
        with patch.object(onboarding.socket, "getaddrinfo", return_value=[]):
            with self.assertRaisesRegex(OSError, "No network address"):
                onboarding.connect(("example.org", 443))

    def test_open_url_uses_fast_connection(self):
        with patch.object(onboarding.urllib.request.OpenerDirector, "open") as request:
            onboarding.open_url("https://example.org/model.bin")
        request.assert_called_once_with("https://example.org/model.bin", timeout=60)
        handler = onboarding._Handler()
        with patch.object(handler, "do_open") as do_open:
            handler.https_open(onboarding.urllib.request.Request("https://example.org"))
        self.assertIs(do_open.call_args.args[0], onboarding._Connection)
        connection = onboarding._Connection("example.org")
        self.assertIs(connection._create_connection, onboarding.connect)

    def test_cli_setup_errors_are_friendly(self):
        result = subprocess.run(
            [sys.executable, str(Path(dictation.__file__)), "--setup", "--interactive"],
            input="3\n",
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("Choose 1 or 2", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
