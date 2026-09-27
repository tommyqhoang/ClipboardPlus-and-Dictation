"""Privacy and reliability tests for the standard-library telemetry client."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import telemetry


class TelemetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.roots = patch.object(telemetry.desktop, "roots", return_value=(self.root,))
        self.roots.start()
        self.addCleanup(self.roots.stop)
        telemetry._sent.clear()
        telemetry._reports = 0

    def enabled(self) -> patch.dict[str, str]:
        return patch.dict(
            os.environ,
            {"DICTATION_TELEMETRY_TESTS": "1", "DICTATION_TELEMETRY": "1", "DO_NOT_TRACK": "0"},
            clear=False,
        )

    def test_disabled_by_environment_and_preference(self) -> None:
        with patch.dict(os.environ, {"DO_NOT_TRACK": "1"}, clear=False):
            self.assertFalse(telemetry.allowed())
        (self.root / "menubar.json").write_text('{"share_usage": false}', encoding="utf-8")
        with self.enabled():
            self.assertFalse(telemetry.allowed())

    def test_usage_event_accepts_only_fixed_schema(self) -> None:
        sent: list[tuple[str, bytes, dict[str, str]]] = []
        with (
            self.enabled(),
            patch.object(telemetry, "_post", side_effect=lambda *args: sent.append(args)),
        ):
            self.assertTrue(
                telemetry.event(
                    "dictation_complete",
                    wait=True,
                    seconds=2.5,
                    backend="local",
                    transcript="do not send this",
                    path="/home/private",
                )
            )
        payload = json.loads(sent[0][1])
        params = payload["events"][0]["params"]
        self.assertEqual(params["backend"], "local")
        self.assertEqual(params["seconds"], 2.5)
        self.assertNotIn("transcript", params)
        self.assertNotIn("path", params)

    def test_crash_report_removes_exception_text_and_external_paths(self) -> None:
        reports: list[dict[str, object]] = []
        try:
            raise ValueError("clipboard value: very-private@example.test /home/me/secret")
        except ValueError as error:
            with (
                self.enabled(),
                patch.object(telemetry, "_post_sentry", side_effect=reports.append),
            ):
                self.assertTrue(telemetry.capture(error, wait=True, ignored="private"))
        payload = json.dumps(reports[0])
        self.assertNotIn("very-private", payload)
        self.assertNotIn("example.test", payload)
        self.assertNotIn("/home/me", payload)
        self.assertIn("Application error", payload)

    def test_telemetry_id_is_random_and_private_file(self) -> None:
        with self.enabled():
            first = telemetry.client_id()
            second = telemetry.client_id()
        self.assertEqual(first, second)
        path = self.root / "telemetry-id"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
