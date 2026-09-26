"""Optional link to a Clipboard+ account.

When the user pastes a Clipboard+ API key in Settings, each transcript is also
saved to their Clipboard+ history (source "Whisper Dictation"), so it shows in
the web dashboard and the browser extension. Nothing is sent unless a key is
saved, and the key is never used for anything else. Standard library only.
"""

from __future__ import annotations

import http.client
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

API = "https://backend-production-74d4.up.railway.app"
SITE = "https://clipboardplus.apercallc.com"
ACCOUNT_URL = SITE + "/account.html"
DASHBOARD_URL = SITE + "/dashboard.html"
SOURCE = "Whisper Dictation"
KEY_PREFIX = "cp_live_"
# The service rejects larger items, so do not upload them.
MAX_BYTES = 50_000


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: it would forward the key to another host."""

    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


def key_path(config_dir: Path) -> Path:
    return config_dir / "clipboard-plus-key"


def clean_key(value: str) -> str:
    key = value.strip()
    if not key.startswith(KEY_PREFIX) or len(key) < 24 or len(key) > 200:
        raise ValueError("That is not a Clipboard+ API key (it starts with cp_live_).")
    if not key.isascii() or not key.isprintable() or any(char.isspace() for char in key):
        raise ValueError("That is not a Clipboard+ API key (it starts with cp_live_).")
    return key


def read_key(config_dir: Path) -> str:
    try:
        return clean_key(key_path(config_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""


def linked(config_dir: Path) -> bool:
    return bool(read_key(config_dir))


def save_key(config_dir: Path, value: str) -> None:
    key = clean_key(value)
    config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = key_path(config_dir)
    # A fresh, owner-only file (mkstemp uses O_EXCL), then an atomic rename.
    descriptor, temporary = tempfile.mkstemp(dir=config_dir, prefix=".clipboard-plus-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(key)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    if sys.platform != "win32":
        path.chmod(0o600)


def remove_key(config_dir: Path) -> None:
    key_path(config_dir).unlink(missing_ok=True)


def request(key: str, method: str, route: str, body: dict[str, Any] | None, api: str) -> int | None:
    """The HTTP status, or None when the service could not be reached."""
    if not api.startswith("https://"):
        return None  # The key only ever travels over TLS.
    headers = {"Authorization": "Bearer " + key, "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    call = urllib.request.Request(api + route, data=data, headers=headers, method=method)
    try:
        with urllib.request.build_opener(NoRedirect()).open(call, timeout=6) as reply:
            reply.read(64 * 1024)
            return int(reply.status)
    except urllib.error.HTTPError as exc:
        exc.close()
        return int(exc.code)
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError):
        return None


def verify(key: str, api: str = API) -> str:
    """ok, invalid, read-only, offline or error.

    Posts an item of an unknown type: the service checks the key and its write
    permission first, then rejects the item (400) without saving anything.
    """
    status = request(key, "POST", "/api/clipboard", {"type": "verify"}, api)
    if status is None:
        return "offline"
    if status == 400:
        return "ok"
    if status == 401:
        return "invalid"
    return "read-only" if status == 403 else "error"


def send(config_dir: Path, text: str, api: str = API) -> str:
    """sent, off (not linked or nothing to send), rejected (key refused) or failed."""
    key = read_key(config_dir)
    if not key or not text.strip():
        return "off"
    if len(text.encode("utf-8")) > MAX_BYTES:
        return "failed"
    body = {"type": "text", "content": text, "source": SOURCE}
    status = request(key, "POST", "/api/clipboard", body, api)
    if status is not None and 200 <= status < 300:
        return "sent"
    return "rejected" if status in (401, 403) else "failed"
