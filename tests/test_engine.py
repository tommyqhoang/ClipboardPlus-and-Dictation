"""Tests for the ready speech engine (lib/engine.py).

A small local HTTP server stands in for whisper-server: same /health and
/inference shapes, none of the model load.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import dictation as d
import engine

WAV = d.wav_bytes(b"\x10\x00" * 2000)


class FakeConfig:
    """The Config surface engine.py reads."""

    def __init__(self, values: dict[str, Any] | None = None) -> None:
        self.paths = None  # type: ignore[assignment]
        self.values = {
            "whisper_bin": "/usr/bin/whisper-cli",
            "model": "/models/base.bin",
            "language": "en",
            "threads": 4,
            "vad_model": "",
            "prompt": "",
            **(values or {}),
        }

    def s(self, key: str) -> str:
        return str(self.values.get(key, ""))

    def n(self, key: str) -> int:
        return int(self.values.get(key, 0))


class WhisperLikeServer(BaseHTTPRequestHandler):
    text = "hello from the engine"

    def do_GET(self) -> None:  # /health
        self.send_response(503)  # "loading" until the test flips this.
        self.end_headers()

    def do_POST(self) -> None:  # /inference
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        body = json.dumps({"text": WhisperLikeServer.text}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        pass


class EngineTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("DICTATION_")}
        self.env.update(
            {
                "XDG_CONFIG_HOME": str(self.root / "config"),
                "XDG_CACHE_HOME": str(self.root / "cache"),
                "XDG_RUNTIME_DIR": str(self.root / "runtime"),
            }
        )
        self.patch = patch.dict(os.environ, self.env, clear=True)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.paths = d.Paths()
        d.private_dir(self.paths.runtime)
        model = self.root / "model.bin"
        model.write_bytes(b"model")
        self.config = FakeConfig({"model": str(model)})
        self.config.paths = self.paths
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), WhisperLikeServer)
        self.addCleanup(self.server.server_close)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]

    def write_info(self, age: float = 0.0) -> None:
        import time

        d.atomic(
            engine.info_file(self.paths),
            json.dumps({"port": self.port, "pid": 12345, "started": time.time() - age}),
        )

    def test_a_registered_engine_transcribes(self):
        self.write_info()
        text = engine.transcribe(self.config, self.paths, WAV, timeout=5)
        self.assertEqual(text, "hello from the engine")
        # Use was noted, so the supervisor keeps the engine alive.
        self.assertTrue(engine.used_file(self.paths).exists())

    def test_no_registered_engine_means_fallback(self):
        self.assertIsNone(engine.transcribe(self.config, self.paths, WAV, timeout=5))

    def test_a_stale_pointer_is_ignored(self):
        self.write_info(age=13 * 3600)
        self.assertIsNone(engine.read_info(self.paths))

    def test_a_port_without_a_server_is_ignored(self):
        import time

        d.atomic(
            engine.info_file(self.paths),
            json.dumps({"port": 1, "pid": 1, "started": time.time()}),
        )
        with patch.object(engine, "touch") as touch:
            self.assertIsNone(engine.transcribe(self.config, self.paths, WAV, timeout=2))
            touch.assert_not_called()

    def test_the_command_targets_this_machine_only(self):
        with patch.object(engine, "server_binary", return_value="/usr/bin/whisper-server"):
            command = engine.server_command(self.config, 1234, self.root)
        assert command is not None
        self.assertIn("127.0.0.1", command)
        self.assertIn("-nt", command)
        self.assertNotIn("--convert", command)

    def test_no_server_binary_means_no_engine(self):
        with patch.object(engine, "server_binary", return_value=""):
            self.assertIsNone(engine.server_command(self.config, 1234, self.root))
        with patch.object(engine, "server_binary", return_value=""):
            self.assertFalse(engine.start(self.paths, self.config))

    def test_the_supervisor_announces_a_healthy_server_and_retracts_it_after(self):
        # A stand-in "server" that turns healthy once started.
        import threading

        healthy = threading.Event()
        real_healthy = engine._healthy

        def healthy_when_ready(port: int, timeout: float = 1.0) -> bool:
            return healthy.is_set() and real_healthy(port, timeout)

        class Ready(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.send_response(200)
                self.end_headers()

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                pass

        standin = ThreadingHTTPServer(("127.0.0.1", 0), Ready)
        self.addCleanup(standin.server_close)
        port = standin.server_address[1]

        def command() -> list[str]:
            # The real process is a python one-liner that stays alive; the port
            # in the command is the stand-in's, which health checks hit.
            healthy.set()
            threading.Thread(target=standin.serve_forever, daemon=True).start()
            return [
                sys.executable,
                "-c",
                "import time; time.sleep(5)",
                "--port",
                str(port),
            ]

        with patch.object(engine, "server_binary", return_value="unused"):
            with patch.object(engine, "_free_port", return_value=port):
                with patch.object(engine, "_healthy", healthy_when_ready):
                    result = engine.supervise(self.paths, command())
        self.assertEqual(result, 0)
        # The pointer existed while running and is cleaned up afterwards.
        self.assertFalse(engine.info_file(self.paths).exists())

    def test_an_unstartable_server_exits_for_fallback(self):
        with patch.object(engine, "_healthy", lambda *a: False):
            result = engine.supervise(
                self.paths,
                [sys.executable, "-c", "import sys; sys.exit(3)", "--port", "1"],
            )
        self.assertEqual(result, 3)
        self.assertFalse(engine.info_file(self.paths).exists())

    def test_failures_are_classified_with_specific_messages(self):
        import socket
        import urllib.error

        cases = (
            (urllib.error.HTTPError("u", 500, "boom", {}, None), engine.Failure.HTTP_ERROR),  # type: ignore[arg-type]
            (TimeoutError(), engine.Failure.TIMEOUT),
            (socket.timeout(), engine.Failure.TIMEOUT),
            (urllib.error.URLError(TimeoutError()), engine.Failure.TIMEOUT),
            (ValueError("bad json"), engine.Failure.BAD_REPLY),
            (ConnectionRefusedError(), engine.Failure.UNAVAILABLE),
        )
        cases[0][0].close()
        for exc, kind in cases:
            error = engine.classify(exc)
            self.assertIs(error.kind, kind, repr(exc))
            self.assertTrue(str(error))
        self.assertIn("HTTP 500", str(engine.classify(cases[0][0])))
        # Every kind has wording of its own.
        self.assertEqual(len({str(engine.EngineError(kind)) for kind in engine.Failure}), 7)

    def test_a_failed_request_records_why(self):
        self.write_info()
        with patch.object(engine.urllib.request.OpenerDirector, "open", side_effect=TimeoutError):
            self.assertIsNone(engine.transcribe(self.config, self.paths, WAV, timeout=1))
        assert engine.last_failure is not None
        self.assertIs(engine.last_failure.kind, engine.Failure.TIMEOUT)
        self.assertEqual(
            engine.transcribe(self.config, self.paths, WAV, timeout=5), "hello from the engine"
        )
        self.assertIsNone(engine.last_failure)

    def test_an_unreadable_reply_is_a_bad_reply(self):
        self.write_info()
        info = {"port": self.port, "pid": 1}
        with (
            patch.object(engine, "read_info", return_value=info),
            patch.object(engine.json, "loads", return_value={"other": 1}),
        ):
            self.assertIsNone(engine.transcribe(self.config, self.paths, WAV, timeout=5))
        assert engine.last_failure is not None
        self.assertIs(engine.last_failure.kind, engine.Failure.BAD_REPLY)

    def test_empty_audio_is_reported_not_sent(self):
        self.write_info()
        self.assertIsNone(engine.transcribe(self.config, self.paths, d.wav_bytes(b""), timeout=5))
        assert engine.last_failure is not None
        self.assertIs(engine.last_failure.kind, engine.Failure.EMPTY_AUDIO)

    def test_start_reports_a_missing_model_or_server(self):
        config = FakeConfig({"model": str(self.root / "absent.bin")})
        config.paths = self.paths
        self.assertFalse(engine.start(self.paths, config))
        assert engine.last_failure is not None
        self.assertIs(engine.last_failure.kind, engine.Failure.MODEL_MISSING)
        with patch.object(engine, "server_binary", return_value=""):
            self.assertFalse(engine.start(self.paths, self.config))
        assert engine.last_failure is not None
        self.assertIs(engine.last_failure.kind, engine.Failure.SERVER_MISSING)

    def test_start_reports_a_launch_failure(self):
        with (
            patch.object(engine, "server_binary", return_value="/usr/bin/whisper-server"),
            patch.object(engine, "_free_port", return_value=1234),
            patch.object(engine.subprocess, "Popen", side_effect=OSError("no exec")),
        ):
            self.assertFalse(engine.start(self.paths, self.config))
        assert engine.last_failure is not None
        self.assertIs(engine.last_failure.kind, engine.Failure.UNAVAILABLE)

    def test_the_engine_is_started_detached_by_start(self):
        with (
            patch.object(engine, "server_binary", return_value="/usr/bin/whisper-server"),
            patch.object(engine, "_free_port", return_value=1234),
            patch.object(engine.subprocess, "Popen") as popen,
        ):
            self.assertTrue(engine.start(self.paths, self.config))
        args = popen.call_args.args[0]
        self.assertTrue(str(args[1]).endswith("engine.py"))
        self.assertEqual(args[args.index("--supervise") + 1], "/usr/bin/whisper-server")

    def test_the_engine_relaunches_a_sibling_binary_when_frozen(self):
        with (
            patch.object(engine, "server_binary", return_value="/usr/bin/whisper-server"),
            patch.object(engine, "_free_port", return_value=1234),
            patch.object(engine.desktop, "frozen_root", return_value=Path("/opt/Clipboard+")),
            patch.object(engine.desktop, "platform_name", return_value="linux"),
            patch.object(engine.subprocess, "Popen") as popen,
        ):
            self.assertTrue(engine.start(self.paths, self.config))
        args = popen.call_args.args[0]
        # No stray __file__ path when frozen — just the sibling binary and the
        # real args, or argparse (with no positional defined) rejects it outright.
        self.assertEqual(args[0], str(Path("/opt/Clipboard+") / "engine"))
        self.assertEqual(args[1], "--supervise")
        self.assertEqual(args[args.index("--supervise") + 1], "/usr/bin/whisper-server")


if __name__ == "__main__":
    unittest.main()
