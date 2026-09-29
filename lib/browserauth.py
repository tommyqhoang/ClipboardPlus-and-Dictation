"""Browser sign-in with a single-use PKCE code and a loopback-only callback."""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.server
import secrets
import threading
import time
import urllib.parse
import webbrowser
from typing import Any

import clipboardplus

TIMEOUT_SECONDS = 180  # A hard limit on the whole sign-in.
MAX_BAD_REQUESTS = 25  # Something is hammering the port: give up rather than keep listening.
SITE_ORIGIN = clipboardplus.SITE


def request_allowed(headers: Any) -> bool:
    """Whether a callback request looks like the browser navigating back from the site.

    The only legitimate caller is the browser following the sign-in page's redirect, a
    top-level navigation. A request that names another Origin, or that a page made itself
    (a script fetch, an image, a form from a page on this machine) is refused.
    """
    origin = headers.get("Origin")
    if origin is not None and origin != SITE_ORIGIN:
        return False
    if headers.get("Sec-Fetch-Site", "cross-site") not in ("cross-site", "none"):
        return False
    if headers.get("Sec-Fetch-Mode", "navigate") != "navigate":
        return False
    return bool(headers.get("Sec-Fetch-Dest", "document") == "document")


class BrowserSignIn:
    def __init__(self) -> None:
        self.cancelled = threading.Event()
        self.verifier = secrets.token_urlsafe(32)
        self.state = secrets.token_urlsafe(32)
        self.code = ""
        self.denied = False
        self.bad_requests = 0
        self._lock = threading.Lock()

    def callback(self, path: str) -> bool:
        """Accept the first valid callback only; every later one is refused (single use)."""
        with self._lock:
            if self.code or self.denied:
                return False
            return self._accept(path)

    def _accept(self, path: str) -> bool:
        url = urllib.parse.urlsplit(path)
        query = urllib.parse.parse_qs(url.query)
        states = query.get("state", [])
        # Constant time, so the state cannot be guessed a character at a time.
        if url.path != "/callback" or len(states) != 1:
            return False
        if not hmac.compare_digest(states[0].encode("utf-8"), self.state.encode("utf-8")):
            return False
        codes = query.get("code", [])
        if (
            len(codes) == 1
            and len(codes[0]) == 64
            and all(c in "0123456789abcdef" for c in codes[0])
        ):
            self.code = codes[0]
            return True
        if query.get("error") == ["access_denied"]:
            self.denied = True
            return True
        return False

    def run(self, api: str = clipboardplus.API) -> tuple[str, str]:
        flow = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def setup(self) -> None:
                self.request.settimeout(1)
                super().setup()

            def do_GET(self) -> None:
                if self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}":  # type: ignore[attr-defined]
                    self.send_error(400)
                    return
                accepted = request_allowed(self.headers) and flow.callback(self.path)
                if not accepted:
                    flow.bad_requests += 1
                body = (
                    b"Sign-in received. You can return to Clipboard+."
                    if accepted
                    else b"Invalid sign-in request. Return to Clipboard+ and try again."
                )
                self.send_response(200 if accepted else 400)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:
                pass  # Never log a callback URL or authorization code.

        try:
            with http.server.HTTPServer(("127.0.0.1", 0), Handler) as server:
                server.timeout = 0.25
                redirect = f"http://127.0.0.1:{server.server_port}/callback"
                challenge = (
                    base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode("ascii")).digest())
                    .rstrip(b"=")
                    .decode("ascii")
                )
                query = urllib.parse.urlencode(
                    {
                        "desktop_connect": "1",
                        "challenge": challenge,
                        "redirect_uri": redirect,
                        "state": self.state,
                    }
                )
                if not webbrowser.open(clipboardplus.SITE + "/account.html?" + query + "#desktop"):
                    raise clipboardplus.AuthError("Couldn’t open your browser. Try again.")
                deadline = time.monotonic() + TIMEOUT_SECONDS
                while not self.code and not self.denied and not self.cancelled.is_set():
                    if self.bad_requests >= MAX_BAD_REQUESTS:
                        raise clipboardplus.AuthError("Sign-in was interrupted. Try again.")
                    if time.monotonic() >= deadline:
                        raise clipboardplus.AuthError(
                            "Sign-in timed out. Try again when you’re ready."
                        )
                    server.handle_request()
            if self.denied or self.cancelled.is_set():
                raise clipboardplus.AuthError("Sign-in cancelled.")
            return clipboardplus.exchange_desktop(self.code, self.verifier, redirect, api)
        except OSError as exc:
            raise clipboardplus.AuthError("Couldn’t receive browser sign-in. Try again.") from exc
