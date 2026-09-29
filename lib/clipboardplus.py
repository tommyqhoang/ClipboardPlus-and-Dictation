"""Optional link to a Clipboard+ account.

The user creates an account or signs in from the app (the password and session token
are used for one request each and discarded; only a key limited to clipboard read and
write is kept) or pastes a key made on the website. With a key saved, the sync engine
mirrors the clipboard history with the account, so it shows in the web dashboard and
the browser extension. Nothing is sent unless a key is saved, and the key is never
used for anything else. Standard library only.
"""

from __future__ import annotations

import hashlib
import http.client
import importlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import desktop

if TYPE_CHECKING:
    from clipstore import Item

# The one place the service's host is named (telemetry.py builds its URL from it too).
# CLIPBOARDPLUS_API points the app at another deployment (a staging server, a local
# test server); only https:// hosts are accepted, because the key travels to it.
DEFAULT_API = "https://backend-production-74d4.up.railway.app"


def _api() -> str:
    override = os.environ.get("CLIPBOARDPLUS_API", "").strip().rstrip("/")
    return override if override.startswith("https://") and len(override) > 8 else DEFAULT_API


API = _api()
SITE = "https://clipboardplus.apercallc.com"
# Opens the account page on its desktop app card, where keys for this app are made.
ACCOUNT_URL = SITE + "/account.html#desktop"
DASHBOARD_URL = SITE + "/dashboard.html"
SOURCE = "Clipboard+ desktop"  # Shown as "Source:" in the extension and website.
KEY_PREFIX = "cp_live_"
# The service rejects larger items, so do not upload them.
MAX_BYTES = 50_000
BATCH = 100  # Items per /sync request.
MAX_REPLY = 16 * 1024 * 1024
# `sync_key` converts service timestamps to a Python datetime. Ignore malformed
# rows rather than letting a non-finite or out-of-range value stop all sync.
MAX_CREATED_MS = 253_402_300_799_999  # 9999-12-31T23:59:59.999Z
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)
SCOPES = ["clipboard:read", "clipboard:write"]
GOOGLE_ONLY = "This account signs in with Google. Choose Continue in browser to sign in."
TROUBLE = "Clipboard+ is having trouble right now. Try again shortly."
OFFLINE = "Couldn’t reach Clipboard+. Check your internet connection and try again."


class AuthError(Exception):
    """The account, password or key was refused. Messages are safe to show."""


class SyncError(Exception):
    """A request failed for another reason; `status` is None when offline."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: it would forward the key to another host."""

    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


def key_path(config_dir: Path) -> Path:
    """The key file. With a system keychain it holds only a marker (not a secret) that
    says the key is in the keychain, so a changed key is still noticed by its file time."""
    return config_dir / "clipboard-plus-key"


KEYRING_SERVICE = "Clipboard+ desktop"
KEYRING_MARKER = "keyring:v1"


def _keyring() -> Any | None:
    """The optional `keyring` package (macOS Keychain, Windows Credential Manager, Secret
    Service), or None when it is missing, broken or switched off (CLIPBOARDPLUS_KEYRING=0).
    Tests never touch a real keychain unless they ask for it."""
    if os.environ.get("CLIPBOARDPLUS_KEYRING", "").lower() in ("0", "false", "off"):
        return None
    if "unittest" in sys.modules and not os.environ.get("CLIPBOARDPLUS_KEYRING_TESTS"):
        return None
    try:
        return importlib.import_module("keyring")
    except Exception:  # noqa: BLE001 - any import failure just means no keychain
        return None


def _keychain_name(config_dir: Path) -> str:
    """The keychain entry name: one per configuration folder."""
    return "api-key-" + hashlib.sha256(str(config_dir).encode("utf-8")).hexdigest()[:16]


def _keychain_store(config_dir: Path, key: str) -> bool:
    """Put the key in the system keychain; True only when it reads back identically."""
    keyring = _keyring()
    if keyring is None:
        return False
    try:
        keyring.set_password(KEYRING_SERVICE, _keychain_name(config_dir), key)
        return bool(keyring.get_password(KEYRING_SERVICE, _keychain_name(config_dir)) == key)
    except Exception:  # noqa: BLE001 - no backend, a locked or refused keychain: use the file
        return False


def _keychain_read(config_dir: Path) -> str:
    keyring = _keyring()
    if keyring is None:
        return ""
    try:
        found = keyring.get_password(KEYRING_SERVICE, _keychain_name(config_dir))
        return clean_key(found) if isinstance(found, str) else ""
    except Exception:  # noqa: BLE001
        return ""


def _keychain_delete(config_dir: Path) -> None:
    keyring = _keyring()
    if keyring is None:
        return
    try:
        keyring.delete_password(KEYRING_SERVICE, _keychain_name(config_dir))
    except Exception:  # noqa: BLE001 - nothing stored, or no backend
        pass


def _shred(path: Path) -> None:
    """Overwrite a file's bytes in place, then delete it (best effort: journaling file
    systems and SSDs may keep old blocks, which is why the keychain is preferred)."""
    try:
        size = path.stat().st_size
        if path.is_file() and not path.is_symlink() and size:
            with path.open("r+b") as stream:
                stream.write(b"\0" * size)
                stream.flush()
                os.fsync(stream.fileno())
    except OSError:
        pass
    path.unlink(missing_ok=True)


def clean_key(value: str) -> str:
    key = value.strip()
    if not key.startswith(KEY_PREFIX) or len(key) < 24 or len(key) > 200:
        raise ValueError("That is not a Clipboard+ API key (it starts with cp_live_).")
    if not key.isascii() or not key.isprintable() or any(char.isspace() for char in key):
        raise ValueError("That is not a Clipboard+ API key (it starts with cp_live_).")
    return key


def read_key(config_dir: Path) -> str:
    """The saved key, or "" when there is none. A key an older version left in the file
    is moved into the keychain (when there is one) and the file's copy wiped."""
    path = key_path(config_dir)
    try:
        text = path.read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return ""
    if text == KEYRING_MARKER:
        return _keychain_read(config_dir)
    try:
        key = clean_key(text)
    except ValueError:
        return ""
    if _keychain_store(config_dir, key):
        try:
            _shred(path)
            _write_private(config_dir, path, KEYRING_MARKER)
        except OSError:
            pass  # The key is safe in the keychain either way.
    else:
        desktop.restrict_to_owner(path)  # A file from before the permission rules.
    return key


def linked(config_dir: Path) -> bool:
    return bool(read_key(config_dir))


def _write_private(directory: Path, path: Path, text: str) -> None:
    """Write an owner-only file atomically: mode 0600 from creation (whatever the umask),
    an owner-only ACL on Windows, a fresh name then a rename."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    desktop.write_private(path, text)


def save_key(config_dir: Path, value: str) -> None:
    """Keep the key in the system keychain when there is one (the file then holds only a
    marker), otherwise in an owner-only file."""
    key = clean_key(value)
    if _keychain_store(config_dir, key):
        _write_private(config_dir, key_path(config_dir), KEYRING_MARKER)
    else:
        _write_private(config_dir, key_path(config_dir), key)


def remove_key(config_dir: Path) -> None:
    _keychain_delete(config_dir)
    _shred(key_path(config_dir))
    email_path(config_dir).unlink(missing_ok=True)


# The address of the account the key belongs to: shown as "Connected as …". It is
# not a secret, but it is kept as privately as the key.
_EMAIL = re.compile(r"[^\s@\x00-\x1f]+@[^\s@\x00-\x1f]+\.[^\s@\x00-\x1f]+")


def email_path(config_dir: Path) -> Path:
    return config_dir / "clipboard-plus-account"


def _clean_email(value: str) -> str:
    email = value.strip()
    if len(email) > 254 or not _EMAIL.fullmatch(email):
        raise ValueError("Not an email address.")
    return email


def read_email(config_dir: Path) -> str:
    try:
        return _clean_email(email_path(config_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""


def save_email(config_dir: Path, value: str) -> None:
    """Remember the account's address; an unusable one is not kept."""
    try:
        email = _clean_email(value)
    except ValueError:
        email_path(config_dir).unlink(missing_ok=True)
        return
    _write_private(config_dir, email_path(config_dir), email)


def _send(
    bearer: str,
    method: str,
    route: str,
    body: dict[str, Any] | None,
    api: str,
    *,
    timeout: float = 6.0,
    limit: int = 64 * 1024,
) -> tuple[int | None, bytes]:
    """(HTTP status, body), or (None, b"") when the service could not be reached."""
    if not api.startswith("https://"):
        return None, b""  # Credentials only ever travel over TLS.
    headers = {"Accept": "application/json"}
    if bearer:
        headers["Authorization"] = "Bearer " + bearer
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    call = urllib.request.Request(api + route, data=data, headers=headers, method=method)
    try:
        with urllib.request.build_opener(NoRedirect()).open(call, timeout=timeout) as reply:
            return int(reply.status), bytes(reply.read(limit + 1))
    except urllib.error.HTTPError as exc:
        try:
            return int(exc.code), bytes(exc.read(64 * 1024))
        except (OSError, http.client.HTTPException, AttributeError, ValueError):
            return int(exc.code), b""
        finally:
            exc.close()
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError):
        return None, b""


def request(key: str, method: str, route: str, body: dict[str, Any] | None, api: str) -> int | None:
    """The HTTP status, or None when the service could not be reached."""
    return _send(key, method, route, body, api)[0]


def _document(data: bytes) -> dict[str, Any] | None:
    try:
        parsed = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def verify(key: str, api: str = API) -> str:
    """ok, invalid, read-only, write-only, no-access, offline or error.

    Write: posts an item of an unknown type; the service checks the key and its write
    permission first, then rejects the item (400) without saving anything.
    Read: fetches one item.
    """
    status = request(key, "POST", "/api/clipboard", {"type": "verify"}, api)
    if status is None:
        return "offline"
    if status == 401:
        return "invalid"
    if status not in (400, 403):
        return "error"
    can_write = status == 400
    status = request(key, "GET", "/api/clipboard?limit=1", None, api)
    if status is None:
        return "offline"
    if status == 401:
        return "invalid"
    if status not in (200, 403):
        return "error"
    can_read = status == 200
    if can_read and can_write:
        return "ok"
    if can_read:
        return "read-only"
    return "write-only" if can_write else "no-access"


# -- account ---------------------------------------------------------------
def _account(
    route: str, bearer: str, payload: dict[str, Any], api: str, refused: dict[int, str]
) -> dict[str, Any]:
    """One account request. Failures never echo the server's text or any credential."""
    status, data = _send(bearer, "POST", route, payload, api, timeout=15.0)
    if status is None:
        raise AuthError(OFFLINE)
    document = _document(data) or {}
    if 200 <= status < 300:
        if not document:
            raise AuthError(TROUBLE)
        return document
    code = document.get("code")
    if code == "GOOGLE_ACCOUNT_ONLY" or code == "GOOGLE_ACCOUNT_EXISTS":
        raise AuthError(GOOGLE_ONLY)
    if status == 400:
        error = str(document.get("error", ""))
        if "at least 8" in error:
            message = "Use a password with at least 8 characters."
        elif "email address" in error:
            message = "That email address doesn’t look right."
        elif "length" in error:
            message = "That email or password is too long."
        else:
            message = "Check your email and password and try again."
        raise AuthError(message)
    if status == 429:
        raise AuthError("Too many attempts. Wait a few minutes and try again.")
    raise AuthError(refused.get(status, TROUBLE))


def exchange_desktop(code: str, verifier: str, redirect: str, api: str = API) -> tuple[str, str]:
    status, raw = _send(
        "",
        "POST",
        "/api/desktop-auth/exchange",
        {
            "code": code,
            "verifier": verifier,
            "redirectUri": redirect,
        },
        api,
        timeout=15.0,
    )
    if status != 200:
        if status is None:
            raise AuthError(OFFLINE)
        if status == 409:
            raise AuthError("Remove an unused key from your Clipboard+ account and try again.")
        if status == 404:
            raise AuthError(
                "Browser sign-in is not available on the service yet. Use an API key for now."
            )
        raise AuthError("Couldn’t finish browser sign-in. Please try again.")
    data = _document(raw) or {}
    try:
        key = clean_key(data.get("token", ""))
        email = data.get("email", "")
        if not isinstance(email, str) or not _EMAIL.fullmatch(email):
            raise ValueError("Invalid account")
    except (ValueError, AttributeError):
        raise AuthError(TROUBLE) from None
    return key, email


def _session(route: str, email: str, password: str, api: str, refused: dict[int, str]) -> str:
    document = _account(route, "", {"email": email, "password": password}, api, refused)
    token = document.get("token")
    if not isinstance(token, str) or not token:
        raise AuthError(TROUBLE)
    return token


def register(email: str, password: str, api: str = API) -> str:
    """Create an account; returns the session token (use it once, then drop it)."""
    return _session(
        "/api/auth/register",
        email,
        password,
        api,
        {409: "That email already has an account. Sign in instead."},
    )


def login(email: str, password: str, api: str = API) -> str:
    """Sign in; returns the session token (use it once, then drop it)."""
    return _session("/api/auth/login", email, password, api, {401: "Wrong email or password."})


def create_key(token: str, name: str, api: str = API) -> str:
    """A key limited to clipboard read and write, made with a session token."""
    document = _account(
        "/api/keys",
        token,
        {"name": name, "scopes": SCOPES},
        api,
        {
            401: "Clipboard+ signed you out. Sign in again.",
            403: "Clipboard+ signed you out. Sign in again.",
            409: "You already have 10 keys. Remove one on the Clipboard+ website, then try again.",
        },
    )
    try:
        return clean_key(str(document.get("token", "")))
    except ValueError:
        raise AuthError(TROUBLE) from None


# -- matching --------------------------------------------------------------
def created_ms(created_at: float) -> int:
    """Epoch seconds as the whole milliseconds the service stores."""
    return int(round(created_at * 1000))


def sync_key(kind: str, created_ms: int, primary: str) -> str:
    """The service's own item key: `type|ISO time|first 200 characters`.

    The service (and the browser extension) count characters as JavaScript does, in
    UTF-16 units, so this does too; a cut through a surrogate pair drops the half.
    """
    moment = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=created_ms)
    stamp = f"{moment:%Y-%m-%dT%H:%M:%S}.{moment.microsecond // 1000:03d}Z"
    head = primary.encode("utf-16-le", "surrogatepass")[:400].decode("utf-16-le", "surrogatepass")
    if head and 0xD800 <= ord(head[-1]) <= 0xDBFF:
        head = head[:-1]
    return f"{kind}|{stamp}|{head}"


# -- cloud -----------------------------------------------------------------
@dataclass(frozen=True)
class CloudItem:
    id: str
    kind: str  # text or url
    text: str  # the text, or the link
    label: str
    favorite: bool
    source: str
    created_ms: int
    updated_at: float  # epoch seconds


@dataclass(frozen=True)
class Removed:
    """A deletion made elsewhere: found by kind and timestamp, since ids are gone."""

    kind: str
    created_ms: int
    prefix: str


@dataclass(frozen=True)
class Pull:
    items: list[CloudItem]
    deleted: list[Removed]


@dataclass(frozen=True)
class Listing:
    """The ids the account holds, and whether that list can be trusted as everything.

    `complete` is True only when every page was read, every row had a valid id, and the
    last page said there is no more. Anything else (an error page, an odd row, a cap)
    leaves it False, and callers must then not conclude that a missing id was deleted.
    """

    ids: frozenset[str]
    complete: bool


LIST_PAGE = 200
LIST_MAX_ITEMS = 20_000  # More than this is treated as "not everything".


def _epoch(stamp: object, fallback: float) -> float:
    if isinstance(stamp, str):
        try:
            return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return fallback


def _created_ms(stamp: object) -> int | None:
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
        return None
    if not 0 < stamp <= MAX_CREATED_MS:
        return None
    return int(stamp)


def _cloud_item(raw: object) -> CloudItem | None:
    if not isinstance(raw, dict):
        return None
    identifier, kind = raw.get("id"), raw.get("type")
    if not isinstance(identifier, str) or not UUID.fullmatch(identifier):
        return None
    if kind not in ("text", "url"):
        return None
    text = raw.get("content" if kind == "text" else "url")
    stamp = raw.get("ts")
    created = _created_ms(stamp)
    if not isinstance(text, str) or not text or created is None:
        return None
    label, source = raw.get("label"), raw.get("source")
    return CloudItem(
        id=identifier,
        kind=str(kind),
        text=text,
        label=label if isinstance(label, str) else "",
        favorite=raw.get("isFavorite") is True,
        source=source if isinstance(source, str) else "",
        created_ms=created,
        updated_at=_epoch(raw.get("updatedAt"), created / 1000),
    )


def _removed(raw: object) -> Removed | None:
    if not isinstance(raw, dict) or raw.get("type") not in ("text", "url"):
        return None
    stamp, prefix = raw.get("ts"), raw.get("contentPrefix")
    created = _created_ms(stamp)
    if created is None:
        return None
    return Removed(str(raw["type"]), created, prefix if isinstance(prefix, str) else "")


class Cloud:
    """The account's clipboard history over HTTPS with a scoped key."""

    def __init__(self, key: str, api: str = API) -> None:
        self._key = clean_key(key)
        self._api = api

    def _call(
        self,
        method: str,
        route: str,
        body: dict[str, Any] | None = None,
        *,
        accept: tuple[int, ...] = (),
        parse: bool = False,
    ) -> dict[str, Any]:
        """The parsed reply ({} when not parsed or accepted-missing)."""
        status, data = _send(
            self._key, method, route, body, self._api, timeout=30.0, limit=MAX_REPLY
        )
        if status is None:
            raise SyncError("Couldn’t reach Clipboard+.")
        if status in (401, 403):
            raise AuthError("Clipboard+ no longer accepts this key.")
        if status in accept:
            return {}
        if not 200 <= status < 300:
            raise SyncError(f"Clipboard+ answered with an error ({status}).", status)
        if not parse:
            return {}
        document = _document(data) if len(data) <= MAX_REPLY else None
        if document is None:
            raise SyncError("Clipboard+ sent a reply that could not be read.", status)
        return document

    @staticmethod
    def _id(cloud_id: str) -> str:
        if not UUID.fullmatch(cloud_id):
            raise SyncError("That is not a Clipboard+ item id.")
        return cloud_id

    def pull(self, since: float | None) -> Pull:
        route = "/api/clipboard/pull"
        if since is not None:
            moment = datetime.fromtimestamp(since, tz=timezone.utc)
            stamp = f"{moment:%Y-%m-%dT%H:%M:%S}.{moment.microsecond // 1000:03d}Z"
            route += "?" + urllib.parse.urlencode({"since": stamp})
        document = self._call("GET", route, parse=True)
        items, deleted = document.get("items", []), document.get("deletedItems", [])
        if not isinstance(items, list) or not isinstance(deleted, list):
            raise SyncError("Clipboard+ sent a reply that could not be read.")
        return Pull(
            [item for item in map(_cloud_item, items) if item],
            [gone for gone in map(_removed, deleted) if gone],
        )

    def list_ids(self) -> Listing:
        """Page through `GET /api/clipboard` (the route `clear` already uses)."""
        found: set[str] = set()
        offset = 0
        while offset < LIST_MAX_ITEMS:
            page = self._call(
                "GET", f"/api/clipboard?limit={LIST_PAGE}&offset={offset}", parse=True
            )
            rows = page.get("items")
            if not isinstance(rows, list):
                return Listing(frozenset(found), False)
            before = len(found)
            for row in rows:
                identifier = row.get("id") if isinstance(row, dict) else None
                if not isinstance(identifier, str) or not UUID.fullmatch(identifier):
                    return Listing(frozenset(found), False)
                found.add(identifier)
            more = page.get("hasMore")
            if more is False:
                return Listing(frozenset(found), True)
            if more is not True or not rows or len(found) == before:
                return Listing(frozenset(found), False)  # No explicit end, or no progress.
            offset += len(rows)
        return Listing(frozenset(found), False)

    def push(self, items: list[Item]) -> None:
        """Upload text and links (images never leave the device), 100 per request."""
        entries: list[dict[str, Any]] = []
        for item in items:
            if item.kind not in ("text", "url"):
                continue
            entry: dict[str, Any] = {
                "type": item.kind,
                "url" if item.kind == "url" else "content": item.text,
                "ts": created_ms(item.created_at),
                "isFavorite": item.favorite,
                "source": SOURCE,
            }
            if item.favorite and item.label:
                entry["label"] = item.label
            entries.append(entry)
        for start in range(0, len(entries), BATCH):
            self._call("POST", "/api/clipboard/sync", {"items": entries[start : start + BATCH]})

    def toggle_favorite(self, cloud_id: str) -> bool | None:
        """The new favorite state, or None when the item no longer exists."""
        document = self._call(
            "PATCH", f"/api/clipboard/{self._id(cloud_id)}/favorite", accept=(404,), parse=True
        )
        if not document:
            return None
        item = document.get("item")
        if isinstance(item, dict) and isinstance(item.get("isFavorite"), bool):
            return bool(item["isFavorite"])
        raise SyncError("Clipboard+ sent a reply that could not be read.")

    def set_label(self, cloud_id: str, label: str) -> None:
        """Name a favorite ("" removes the name). A missing or unstarred item is let go."""
        self._call(
            "PATCH", f"/api/clipboard/{self._id(cloud_id)}", {"label": label}, accept=(400, 404)
        )

    def delete(self, cloud_id: str) -> None:
        """404 is success: it is already gone."""
        self._call("DELETE", f"/api/clipboard/{self._id(cloud_id)}", accept=(404,))

    def clear(self, *, favorites: bool) -> None:
        """Delete the history (starred items stay unless `favorites`)."""
        self._call("DELETE", "/api/clipboard")
        if not favorites:
            return
        starred: list[str] = []
        offset = 0
        while offset < 100_000:
            page = self._call("GET", f"/api/clipboard?limit=200&offset={offset}", parse=True)
            rows = page.get("items", [])
            if not isinstance(rows, list):
                raise SyncError("Clipboard+ sent a reply that could not be read.")
            starred += [
                row["id"]
                for row in rows
                if isinstance(row, dict)
                and row.get("isFavorite") is True
                and isinstance(row.get("id"), str)
                and UUID.fullmatch(row["id"])
            ]
            if page.get("hasMore") is not True or not rows:
                break
            offset += len(rows)
        for start in range(0, len(starred), 500):
            self._call("DELETE", "/api/clipboard/bulk", {"ids": starred[start : start + 500]})
