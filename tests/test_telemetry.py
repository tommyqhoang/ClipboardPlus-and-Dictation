"""Privacy and reliability tests for the standard-library telemetry client."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

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
        # The user agreed during setup (each test that needs otherwise removes it).
        (self.root / "menubar.json").write_text('{"share_usage": true}', encoding="utf-8")

    def enabled(self) -> patch.dict[str, str]:
        return patch.dict(
            os.environ,
            {"DICTATION_TELEMETRY_TESTS": "1", "DICTATION_TELEMETRY": "1", "DO_NOT_TRACK": "0"},
            clear=False,
        )

    def test_opt_in_only_and_environment_can_still_forbid(self) -> None:
        with patch.dict(os.environ, {"DO_NOT_TRACK": "1"}, clear=False):
            self.assertFalse(telemetry.allowed())
        (self.root / "menubar.json").unlink()
        with self.enabled():
            self.assertFalse(telemetry.has_consent())
            self.assertFalse(telemetry.allowed())  # No choice made yet: nothing is sent.
        (self.root / "welcome.json").write_text('{"complete": true}', encoding="utf-8")
        with self.enabled():
            self.assertFalse(telemetry.allowed())  # Finishing setup is not consent.
        telemetry.set_consent(True)
        with self.enabled():
            self.assertTrue(telemetry.has_consent())
            self.assertTrue(telemetry.allowed())
        with patch.dict(os.environ, {"DICTATION_TELEMETRY": "0"}, clear=False):
            self.assertFalse(telemetry.allowed())
        (self.root / "menubar.json").write_text('{"share_usage": "yes"}', encoding="utf-8")
        with self.enabled():
            self.assertFalse(telemetry.allowed())  # Only an explicit true counts.

    def test_withdrawing_consent_discards_the_installation_id(self) -> None:
        with self.enabled():
            first = telemetry.client_id()
            self.assertTrue((self.root / "telemetry-id").exists())
            telemetry.set_consent(False)
            self.assertFalse((self.root / "telemetry-id").exists())
            self.assertFalse(telemetry.allowed())
            telemetry.set_consent(True)
            self.assertNotEqual(telemetry.client_id(), first)
            # A switch that only saved the preference is caught too.
            (self.root / "menubar.json").write_text('{"share_usage": false}', encoding="utf-8")
            self.assertFalse(telemetry.allowed())
            self.assertFalse((self.root / "telemetry-id").exists())

    def test_set_consent_keeps_other_preferences(self) -> None:
        (self.root / "menubar.json").write_text('{"open_at_login": false}', encoding="utf-8")
        telemetry.set_consent(True)
        saved = json.loads((self.root / "menubar.json").read_text(encoding="utf-8"))
        self.assertEqual(saved, {"open_at_login": False, "share_usage": True})

    def test_the_api_host_can_be_overridden_only_with_https(self) -> None:
        import clipboardplus

        with patch.dict(os.environ, {"CLIPBOARDPLUS_API": "https://staging.example.test/"}):
            self.assertEqual(clipboardplus._api(), "https://staging.example.test")
        for bad in ("http://staging.example.test", "https://", "staging"):
            with patch.dict(os.environ, {"CLIPBOARDPLUS_API": bad}):
                self.assertEqual(clipboardplus._api(), clipboardplus.DEFAULT_API)

    def test_usage_goes_to_the_same_api_as_the_account(self) -> None:
        import clipboardplus

        self.assertTrue(telemetry.ANALYTICS_URL.startswith(clipboardplus.API + "/api/"))

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
        if os.name != "nt":  # Windows files have no POSIX mode bits.
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_scrub_removes_personal_data_before_anything_is_sent(self) -> None:
        home = str(Path.home())
        text = (
            f"{home}/notes.txt jane.doe@example.com https://u:pw@x.test/p/q?token=abc#frag "
            "key cp_live_ABCDEF123456 and " + "a" * 40 + " at 192.168.1.20 in /srv/private/data.db"
            " jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2ln"
        )
        cleaned = telemetry.scrub(text)
        self.assertIn("~/notes.txt", cleaned)
        self.assertIn("[email]", cleaned)
        self.assertIn("https://x.test", cleaned)
        for leaked in (
            home,
            "jane.doe",
            "token=abc",
            "u:pw",
            "/p/q",
            "cp_live_ABCDEF123456",
            "a" * 40,
            "192.168.1.20",
            "/srv/private",
            "eyJhbGci",
        ):
            self.assertNotIn(leaked, cleaned)
        self.assertEqual(len(telemetry.scrub("word " * 1000)), 1000)  # Capped.

    def test_crash_envelope_goes_to_the_dsn_project_with_its_key(self) -> None:
        posted = []
        with patch.object(telemetry, "_post", side_effect=lambda *a: posted.append(a)):
            telemetry._post_sentry({"event_id": "abc"})
        url, body, headers = posted[0]
        self.assertTrue(url.endswith("/envelope/"))
        header, kind, payload = (json.loads(line) for line in body.decode().splitlines())
        self.assertEqual(
            (header["event_id"], kind, payload), ("abc", {"type": "event"}, {"event_id": "abc"})
        )
        self.assertIn("sentry_key=", headers["X-Sentry-Auth"])

    def test_nothing_is_posted_without_consent_and_network_errors_stay_quiet(self) -> None:
        with patch.object(telemetry.urllib.request, "urlopen") as urlopen:
            with patch.dict(os.environ, {"DO_NOT_TRACK": "1"}, clear=False):
                telemetry._post("https://x.test", b"{}", {})
            urlopen.assert_not_called()
            with self.enabled():
                urlopen.side_effect = OSError("offline")
                telemetry._post("https://x.test", b"{}", {})  # No exception.
                urlopen.side_effect = None
                telemetry._post("https://x.test", b"{}", {})
            self.assertEqual(urlopen.call_count, 2)

    def test_sending_never_raises_and_never_piles_up(self) -> None:
        ran = []

        def broken() -> None:
            ran.append(True)
            raise RuntimeError("reporting failed")

        with self.enabled():
            telemetry._send(broken, wait=True)  # Its failure is swallowed.
        self.assertEqual(ran, [True])
        # At most two reports in flight: a third is dropped, not queued.
        for _ in range(2):
            telemetry._pending.acquire()
        try:
            telemetry._send(lambda: ran.append(False), wait=True)
        finally:
            for _ in range(2):
                telemetry._pending.release()
        self.assertEqual(ran, [True])
        with patch.object(telemetry.threading.Thread, "start", side_effect=RuntimeError):
            telemetry._send(lambda: None, wait=False)  # Shutting down: gives its slot back.
        self.assertTrue(telemetry._pending.acquire(blocking=False))
        telemetry._pending.release()

    def test_crash_hooks_report_unhandled_errors_then_defer_to_the_previous_ones(self) -> None:
        captured = []
        with (
            patch.object(sys, "excepthook", MagicMock()) as previous,
            patch.object(threading, "excepthook", MagicMock()) as previous_thread,
            patch.object(telemetry, "capture", side_effect=lambda e, **_: captured.append(e)),
        ):
            telemetry.install("tests", ignore=(ValueError,))
            error = RuntimeError("boom")
            sys.excepthook(RuntimeError, error, None)
            sys.excepthook(KeyboardInterrupt, KeyboardInterrupt(), None)  # Quitting: no report.
            sys.excepthook(ValueError, ValueError(), None)  # Ignored by this component.
            thread_error = OSError("thread")
            args = MagicMock(exc_value=thread_error)
            threading.excepthook(args)
            self.assertEqual(captured, [error, thread_error])
            self.assertEqual(previous.call_count, 3)
            previous_thread.assert_called_once_with(args)
        root = MagicMock()
        shown = root.report_callback_exception
        with patch.object(telemetry, "capture", side_effect=lambda e, **_: captured.append(e)):
            telemetry.watch_tk(root)
            tk_error = KeyError("tk")
            root.report_callback_exception(KeyError, tk_error, None)
        self.assertIs(captured[-1], tk_error)
        shown.assert_called_once()
