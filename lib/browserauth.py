"""Browser sign-in with a single-use PKCE code and a loopback-only callback."""

from __future__ import annotations

import base64
import hashlib
import http.server
import secrets
import threading
import time
import urllib.parse
import webbrowser
from typing import Any

import clipboardplus


class BrowserSignIn:
    def __init__(self) -> None:
        self.cancelled = threading.Event()
        self.verifier = secrets.token_urlsafe(32)
        self.state = secrets.token_urlsafe(32)
        self.code = ""
        self.denied = False

    def callback(self, path: str) -> bool:
        url = urllib.parse.urlsplit(path)
        query = urllib.parse.parse_qs(url.query)
        if url.path != "/callback" or query.get("state") != [self.state]:
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
                accepted = flow.callback(self.path)
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
                deadline = time.monotonic() + 180
                while not self.code and not self.denied and not self.cancelled.is_set():
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
