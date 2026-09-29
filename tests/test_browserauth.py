from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import browserauth
import clipboardplus

KEY = "cp_live_" + "a" * 43


class BrowserSignInTests(unittest.TestCase):
    def test_callback_rejects_wrong_state_duplicate_codes_and_other_paths(self):
        flow = browserauth.BrowserSignIn()
        code = "a" * 64
        for path in (
            f"/callback?state=wrong&code={code}",
            f"/other?state={flow.state}&code={code}",
            f"/callback?state={flow.state}&code={code}&code={code}",
            f"/callback?state={flow.state}&code=invalid",
        ):
            self.assertFalse(flow.callback(path))
            self.assertFalse(flow.code)
        self.assertTrue(flow.callback(f"/callback?state={flow.state}&code={code}"))
        self.assertEqual(flow.code, code)

    def test_a_code_is_single_use_and_state_is_compared_in_constant_time(self):
        flow = browserauth.BrowserSignIn()
        with patch.object(
            browserauth.hmac, "compare_digest", wraps=browserauth.hmac.compare_digest
        ) as compare:
            self.assertTrue(flow.callback(f"/callback?state={flow.state}&code={'a' * 64}"))
        compare.assert_called_once()
        self.assertFalse(flow.callback(f"/callback?state={flow.state}&code={'b' * 64}"))
        self.assertEqual(flow.code, "a" * 64)
        denied = browserauth.BrowserSignIn()
        self.assertTrue(denied.callback(f"/callback?state={denied.state}&error=access_denied"))
        self.assertFalse(denied.callback(f"/callback?state={denied.state}&code={'c' * 64}"))
        self.assertEqual(denied.code, "")

    def test_requests_from_other_origins_or_fetches_are_refused(self):
        allowed = browserauth.request_allowed
        self.assertTrue(allowed({}))  # A plain navigation without Fetch Metadata.
        self.assertTrue(
            allowed(
                {
                    "Sec-Fetch-Site": "cross-site",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Dest": "document",
                }
            )
        )
        self.assertTrue(allowed({"Sec-Fetch-Site": "none"}))
        self.assertTrue(allowed({"Origin": clipboardplus.SITE}))
        for headers in (
            {"Origin": "https://evil.example"},
            {"Origin": "null"},
            {"Sec-Fetch-Site": "same-origin"},
            {"Sec-Fetch-Site": "same-site"},
            {"Sec-Fetch-Mode": "no-cors"},
            {"Sec-Fetch-Mode": "cors"},
            {"Sec-Fetch-Dest": "image"},
            {"Sec-Fetch-Dest": "script"},
        ):
            with self.subTest(headers=headers):
                self.assertFalse(allowed(headers))

    def test_a_forged_request_does_not_consume_the_flow_and_the_server_stops_after_success(self):
        flow = browserauth.BrowserSignIn()
        seen: list[object] = []
        threads: list[threading.Thread] = []
        port: list[str] = []

        def open_browser(url):
            callback = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["redirect_uri"][0]
            port.append(callback)

            def reply():
                target = callback + "?state=" + flow.state + "&code=" + "d" * 64
                try:
                    forged = urllib.request.Request(
                        target, headers={"Origin": "https://evil.example"}
                    )
                    try:
                        urllib.request.urlopen(forged, timeout=3)
                    except urllib.error.HTTPError as exc:
                        seen.append(exc.code)
                    scripted = urllib.request.Request(target, headers={"Sec-Fetch-Mode": "no-cors"})
                    try:
                        urllib.request.urlopen(scripted, timeout=3)
                    except urllib.error.HTTPError as exc:
                        seen.append(exc.code)
                    with urllib.request.urlopen(target, timeout=3) as good:
                        seen.append(good.status)
                except Exception as exc:  # noqa: BLE001
                    seen.append(exc)

            thread = threading.Thread(target=reply)
            threads.append(thread)
            thread.start()
            return True

        with (
            patch.object(browserauth.webbrowser, "open", side_effect=open_browser),
            patch.object(clipboardplus, "exchange_desktop", return_value=(KEY, "me@example.com")),
        ):
            flow.run()
        for thread in threads:
            thread.join(3)
        self.assertEqual(seen, [400, 400, 200])
        with self.assertRaises(OSError):  # Nothing listens any more.
            urllib.request.urlopen(port[0] + "?state=x", timeout=1)

    def test_it_listens_on_loopback_only(self):
        seen: list[str] = []

        def open_browser(url):
            seen.append(urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["redirect_uri"][0])
            return False

        with patch.object(browserauth.webbrowser, "open", side_effect=open_browser):
            with self.assertRaises(clipboardplus.AuthError):
                browserauth.BrowserSignIn().run()
        self.assertTrue(seen[0].startswith("http://127.0.0.1:"))

    def test_hammering_the_port_ends_the_sign_in(self):
        flow = browserauth.BrowserSignIn()
        flow.bad_requests = browserauth.MAX_BAD_REQUESTS
        with patch.object(browserauth.webbrowser, "open", return_value=True):
            with self.assertRaisesRegex(clipboardplus.AuthError, "interrupted"):
                flow.run()

    def test_real_loopback_callback_and_pkce_exchange(self):
        flow = browserauth.BrowserSignIn()
        results = []
        threads = []

        def open_browser(url):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            self.assertNotIn(flow.verifier, url)
            self.assertEqual(query["state"], [flow.state])
            callback = query["redirect_uri"][0]

            def reply():
                try:
                    req = urllib.request.Request(callback + "?state=wrong&code=" + "a" * 64)
                    try:
                        urllib.request.urlopen(req, timeout=3)
                    except urllib.error.HTTPError as exc:
                        results.append(exc.code)
                    with urllib.request.urlopen(
                        callback + "?state=" + flow.state + "&code=" + "b" * 64, timeout=3
                    ) as response:
                        results.append(response.status)
                except Exception as exc:
                    results.append(exc)

            thread = threading.Thread(target=reply)
            threads.append(thread)
            thread.start()
            return True

        with (
            patch.object(browserauth.webbrowser, "open", side_effect=open_browser),
            patch.object(
                clipboardplus, "exchange_desktop", return_value=(KEY, "me@example.com")
            ) as exchange,
        ):
            self.assertEqual(flow.run(), (KEY, "me@example.com"))
            self.assertEqual(exchange.call_args.args[:2], ("b" * 64, flow.verifier))
        for thread in threads:
            thread.join(3)
        self.assertEqual(results, [400, 200])

    def test_cancel_timeout_browser_failure_and_denial(self):
        flow = browserauth.BrowserSignIn()
        with patch.object(browserauth.webbrowser, "open", return_value=False):
            with self.assertRaisesRegex(clipboardplus.AuthError, "open your browser"):
                flow.run()
        flow.cancelled.set()
        with patch.object(browserauth.webbrowser, "open", return_value=True):
            with self.assertRaisesRegex(clipboardplus.AuthError, "cancelled"):
                flow.run()
        flow = browserauth.BrowserSignIn()
        with (
            patch.object(browserauth.webbrowser, "open", return_value=True),
            patch.object(browserauth.time, "monotonic", side_effect=[0, 181]),
        ):
            with self.assertRaisesRegex(clipboardplus.AuthError, "timed out"):
                flow.run()
        self.assertTrue(flow.callback(f"/callback?state={flow.state}&error=access_denied"))
        with patch.object(browserauth.webbrowser, "open", return_value=True):
            with self.assertRaisesRegex(clipboardplus.AuthError, "cancelled"):
                flow.run()

    def test_exchange_validates_credentials_and_reports_errors_without_echoing(self):
        for status, data, expected in (
            (200, {"token": KEY, "email": "me@example.com"}, None),
            (200, {"token": "bad", "email": "me@example.com"}, "trouble"),
            (200, {"token": KEY, "email": None}, "trouble"),
            (400, {"error": "secret value"}, "finish"),
            (409, {}, "unused key"),
            (404, {}, "not available"),
            (None, {}, "reach"),
        ):
            with (
                self.subTest(status=status, expected=expected),
                patch.object(
                    clipboardplus, "_send", return_value=(status, json.dumps(data).encode())
                ),
            ):
                if expected:
                    with self.assertRaisesRegex(clipboardplus.AuthError, expected):
                        clipboardplus.exchange_desktop("code", "verifier", "redirect")
                else:
                    self.assertEqual(
                        clipboardplus.exchange_desktop("code", "verifier", "redirect"),
                        (KEY, "me@example.com"),
                    )
