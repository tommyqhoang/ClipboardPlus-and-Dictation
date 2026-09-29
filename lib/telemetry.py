"""Anonymous crash reports (Sentry) and usage statistics (Google Analytics 4).

Opt-in: nothing is sent until the user turns on "Share anonymous crash reports and
usage statistics" (setup and Settings; see `set_consent`), and never with DO_NOT_TRACK=1
or DICTATION_TELEMETRY=0. Withdrawing consent also discards the installation id. Usage reports
contain an installation-random identifier, a fixed event name, approved feature
choices and bounded counters. Crash reports contain the exception type and frames
from this app only. Clipboard contents, transcripts, audio, file names or paths,
email addresses, keys, and arbitrary error text are never sent.

Standard library only, like the dictation engine that also uses it: events go out
over HTTPS on a background thread and are dropped quietly when offline.
"""

from __future__ import annotations

import json
import math
import os
import platform
import re
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Iterable
from pathlib import Path
from types import TracebackType
from typing import Any

import clipboardplus
import desktop

# Sentry project apercallc/clipboardplus-desktop. A DSN is a public client key.
SENTRY_DSN = (
    "https://c697e9ce83dad48da2dc775d930eac30@o4508955926396928.ingest.us.sentry.io/"
    "4512154615808000"
)
# Usage counts go to the Clipboard+ API, which checks them against a fixed schema before
# relaying them to Google Analytics. The host is named once, in clipboardplus.API
# (default DEFAULT_API there); set CLIPBOARDPLUS_API=https://host to use another
# deployment for the account, sync and these statistics alike.
ANALYTICS_URL = clipboardplus.API + "/api/telemetry/desktop"
TIMEOUT = 4.0
WAIT_SECONDS = 3.0  # How long a short-lived process waits for its report to go out.
MAX_REPORTS = 20  # Per process: a failure loop never floods the project.
PREFERENCE = "share_usage"  # In menubar.json, next to the other preferences.

_sent: set[str] = set()
_reports = 0
_lock = threading.Lock()
_session = str(int(time.time()))
_component = "app"
_pending = threading.BoundedSemaphore(2)
EVENTS = frozenset(
    {
        "app_open",
        "app_installed",
        "app_uninstalled",
        "setup_complete",
        "dictation_setup",
        "dictation_complete",
        "dictation_cancelled",
        "tray_start",
        "shortcut_conflict",
        "shortcut_take_over",
        "setting_changed",
        "mic_test",
        "clipboard_open",
        "clipboard_favorite",
        "clipboard_label",
        "clipboard_copy",
        "clipboard_delete",
        "clipboard_clear",
        "clipboard_daily",
        "account_connect",
        "account_disconnect",
        "account_change",
        "account_sync",
    }
)
COUNTERS = frozenset(
    {
        "seconds",
        "transcribe_seconds",
        "count",
        "texts",
        "images",
        "history_size",
        "on",
        "ok",
        "picker",
        "favorite",
        "everywhere",
        "keep_favorites",
        "first_run",
        "dictation",
        "clipboard",
        "share_usage",
        "setup_complete",
        "open_at_login",
        "default_shortcut",
        "shortcut_ok",
        "live",
        "overlay",
    }
)
CHOICES = {
    "backend": {"local", "openai", "groq", "remote"},
    "source": {"download", "file", "service"},
    "mode": {"both", "clipboard", "dictation"},
    "kind": {"text", "image", "custom", "desktop"},
    "action": {"add", "remove", "set", "clear"},
    "result": {
        *("ok", "empty", "success", "silent", "quiet", "no_audio"),
        *("copied", "pasted", "good", "faint", "none"),  # Dictation and mic test outcomes.
    },
    "sync": {"off", "idle", "syncing", "error", "offline", "connected"},
    "page": {"home", "clipboard", "dictation", "settings", "account", "setup"},
    "stage": {"sync", "capture", "watcher", "recording"},
    "setting": {
        "share_usage",
        "overlay",
        "live",
        "auto_paste",
        "dictation",
        "clipboard",
        "voice",
        "notifications",
    },
}


def safe_params(params: dict[str, Any]) -> dict[str, str | int | float]:
    """Only fixed enums and finite counters cross the privacy boundary."""
    result: dict[str, str | int | float] = {}
    for key, value in params.items():
        if key in COUNTERS and isinstance(value, (int, float)) and math.isfinite(value):
            result[key] = int(value) if isinstance(value, bool) else max(0, min(value, 10**9))
        elif key in CHOICES and isinstance(value, str) and value in CHOICES[key]:
            result[key] = value
    return result


# -- consent -----------------------------------------------------------------
def _config_dir() -> Path:
    return desktop.roots()[0]


def _read_preferences() -> dict[str, Any]:
    try:
        with (_config_dir() / "menubar.json").open(encoding="utf-8") as stream:
            found = json.load(stream)
    except (OSError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


def has_consent() -> bool:
    """Whether the user explicitly agreed to share reports and statistics (default: no)."""
    return _read_preferences().get(PREFERENCE) is True


def set_consent(agreed: bool) -> None:
    """Record the user's choice in the preferences (the same value the Settings switch
    stores). Withdrawing it also discards the installation id, so a later opt-in starts
    from a new, unrelated one."""
    values = _read_preferences()
    values[PREFERENCE] = bool(agreed)
    try:
        desktop.write_private(_config_dir() / "menubar.json", json.dumps(values, indent=2))
    except OSError:
        pass
    if not agreed:
        _forget_client_id()


def _forget_client_id() -> None:
    try:
        (_config_dir() / "telemetry-id").unlink(missing_ok=True)
    except OSError:
        pass


def allowed() -> bool:
    """Whether anything may be sent: the user consented and the environment does not forbid it."""
    if os.environ.get("DICTATION_TELEMETRY", "").lower() in ("0", "false", "off"):
        return False
    if os.environ.get("DO_NOT_TRACK", "") not in ("", "0"):
        return False
    if "unittest" in sys.modules and not os.environ.get("DICTATION_TELEMETRY_TESTS"):
        return False  # A test run never reports itself.
    if has_consent():
        return True
    # No consent (never given, or withdrawn by a switch that only saved the preference):
    # make sure no identifier outlives it.
    _forget_client_id()
    return False


def client_id() -> str:
    """A random id for this installation (not tied to the person or the machine)."""
    path = _config_dir() / "telemetry-id"
    try:
        known = path.read_text(encoding="utf-8").strip()
    except OSError:
        known = ""
    if re.fullmatch(r"[0-9a-f]{32}", known):
        return known
    fresh = uuid.uuid4().hex
    try:
        desktop.write_private(path, fresh)
    except OSError:
        pass
    return fresh


# -- what describes the system -----------------------------------------------
def _memory_gb() -> int:
    try:
        sysconf = getattr(os, "sysconf")  # Not on Windows: AttributeError, like a failure.
        pages, size = int(sysconf("SC_PHYS_PAGES")), int(sysconf("SC_PAGE_SIZE"))
        return round(pages * size / 1e9)
    except (AttributeError, OSError, ValueError):
        return 0


def _os_version() -> str:
    system = desktop.platform_name()
    if system == "macos":
        return platform.mac_ver()[0]
    if system == "windows":
        return platform.version()
    try:
        release = platform.freedesktop_os_release()
        return f"{release.get('ID', 'linux')} {release.get('VERSION_ID', '')}".strip()
    except OSError:
        return platform.release()


def system() -> dict[str, str]:
    """The machine as statistics see it: no names, no paths, no serial numbers."""
    session = os.environ.get("XDG_SESSION_TYPE", "")
    return {
        "app_version": desktop.APP_VERSION,
        "os": desktop.platform_name(),
        "os_version": _os_version()[:36],
        "desktop": (os.environ.get("XDG_CURRENT_DESKTOP", "") or "")[:36],
        "session": session or ("wayland" if os.environ.get("WAYLAND_DISPLAY") else ""),
        "arch": platform.machine(),
        "python": platform.python_version(),
        "cpus": str(os.cpu_count() or 0),
        "memory_gb": str(_memory_gb()),
        "language": (os.environ.get("LANG", "") or "").split(".")[0][:12],
    }


# -- scrubbing ---------------------------------------------------------------
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_SECRET = re.compile(
    r"\b(?:cp_live_|sk-|sk_live_|gsk_|ghp_|gho_|ghs_|github_pat_|xox[abprs]-|AIza|AKIA)[A-Za-z0-9_-]{6,}"
    r"|\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*"
    r"|\b[A-Za-z0-9_+/=-]{32,}\b"
)
_URL = re.compile(
    r"(?P<scheme>[a-z][a-z0-9+.-]*)://(?:[^\s/@]+@)?(?P<host>[^\s/?#:]+)(?::\d+)?[^\s]*", re.I
)
_PATH = re.compile(r"(?:[A-Za-z]:\\|/)(?:[\w.@ -]+[\\/])+[\w.@ -]*")
_IP = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")


def _identity_words() -> list[str]:
    """This machine's private names: account, computer and home folder."""
    words = {str(Path.home())}
    for name in ("USER", "USERNAME", "LOGNAME"):
        words.add(os.environ.get(name, ""))
    try:
        words.add(platform.node())
    except OSError:
        pass
    words.discard("/")
    return sorted((w for w in words if len(w) >= 3), key=len, reverse=True)


def scrub(text: str) -> str:
    """Remove what could identify a person or leak data from a message: the home folder,
    account and computer names, email addresses, URLs (kept as scheme and host only, so
    tokens, queries, credentials and paths in them go), file paths outside the app,
    IP addresses and anything that looks like a key or token."""
    cleaned = text
    home = str(Path.home())
    if home not in ("", "/"):
        cleaned = cleaned.replace(home, "~")
    cleaned = _URL.sub(lambda m: f"{m['scheme']}://{m['host']}", cleaned)
    cleaned = _EMAIL.sub("[email]", cleaned)
    cleaned = _SECRET.sub("[redacted]", cleaned)
    cleaned = _IP.sub("[ip]", cleaned)
    for word in _identity_words():
        if word != home:
            cleaned = re.sub(re.escape(word), "[name]", cleaned, flags=re.IGNORECASE)
    # Paths: keep the file name only (the app's own frames carry no path at all).
    cleaned = _PATH.sub(lambda m: "…/" + re.split(r"[\\/]", m.group(0).rstrip("\\/"))[-1], cleaned)
    return cleaned[:1000]


# -- sending -----------------------------------------------------------------
def _post(url: str, body: bytes, headers: dict[str, str]) -> None:
    if not allowed():
        return
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310
            response.read(256)
    except Exception:  # noqa: BLE001 - offline or refused: statistics never get in the way
        pass


def _send(work: Callable[[], None], wait: bool) -> None:
    if not _pending.acquire(blocking=False):
        return

    def run() -> None:
        try:
            if allowed():
                work()
        except Exception:
            pass  # Reporting failures must never recursively report themselves.
        finally:
            _pending.release()

    thread = threading.Thread(target=run, daemon=True)
    try:
        thread.start()
    except RuntimeError:
        _pending.release()
        return
    if wait:
        thread.join(WAIT_SECONDS)


def set_component(name: str) -> None:
    """Which part of the app is reporting (engine, window, tray, clipboard, pill)."""
    global _component
    _component = name


def event(name: str, wait: bool = False, **params: str | int | float | bool) -> bool:
    """Count something that happened (GA4). True when it was handed over for sending."""
    if name not in EVENTS or not allowed():
        return False
    clean = safe_params(params)
    clean.update(session_id=_session, engagement_time_msec=1, component=_component)
    body = {
        "client_id": client_id(),
        "events": [{"name": name[:40], "params": clean}],
    }
    data = json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    _send(lambda: _post(ANALYTICS_URL, data, headers), wait)
    return True


def _frames(trace: TracebackType | None) -> list[dict[str, Any]]:
    frames = []
    for summary in traceback.extract_tb(trace):
        path = Path(summary.filename)
        ours = path.parent == Path(__file__).parent
        if not ours:
            continue
        frames.append(
            {
                "filename": scrub(path.name),
                "module": scrub(path.stem),
                "function": scrub(summary.name),
                "lineno": summary.lineno,
                "in_app": ours,
            }
        )
    return frames


def capture(
    error: BaseException | None = None,
    message: str = "",
    level: str = "error",
    wait: bool = False,
    **tags: str,
) -> bool:
    """Report an error (Sentry). True when it was handed over for sending."""
    global _reports
    if not SENTRY_DSN or not allowed():
        return False
    # Exceptions may contain a transcript, clipboard value, URL or credential.
    # Regex scrubbing cannot make arbitrary user content safe to transmit.
    text = "Application error" if error is not None else "Application warning"
    kind = type(error).__name__ if error is not None else "Message"
    frames = _frames(error.__traceback__) if error is not None else []
    fingerprint = f"{kind}:{frames[-1:]!r}:{safe_params(tags)!r}"
    with _lock:
        if fingerprint in _sent or _reports >= MAX_REPORTS:
            return False
        _sent.add(fingerprint)
        _reports += 1
    info = system()
    payload: dict[str, Any] = {
        "event_id": uuid.uuid4().hex,
        "timestamp": time.time(),
        "platform": "python",
        "level": level,
        "logger": _component,
        "release": f"clipboardplus-desktop@{desktop.APP_VERSION}",
        "environment": os.environ.get("DICTATION_ENVIRONMENT", "production"),
        "user": {"id": client_id()},
        "tags": {
            "component": _component,
            "os": info["os"],
            **safe_params(tags),
        },
        "contexts": {
            "os": {"name": info["os"], "version": info["os_version"]},
            "runtime": {"name": "CPython", "version": info["python"]},
            "device": {"arch": info["arch"], "memory_size": _memory_gb() * 10**9},
        },
    }
    if error is not None:
        payload["exception"] = {
            "values": [
                {
                    "type": kind,
                    "value": text,
                    "module": type(error).__module__,
                    "stacktrace": {"frames": frames},
                }
            ]
        }
    else:
        payload["message"] = {"formatted": text}
    _send(lambda: _post_sentry(payload), wait)
    return True


def _post_sentry(payload: dict[str, Any]) -> None:
    parts = urllib.parse.urlsplit(SENTRY_DSN)
    project = parts.path.strip("/")
    url = f"{parts.scheme}://{parts.hostname}/api/{project}/envelope/"
    header = {"event_id": payload["event_id"], "dsn": SENTRY_DSN}
    body = "\n".join(json.dumps(part) for part in (header, {"type": "event"}, payload)).encode(
        "utf-8"
    )
    auth = (
        f"Sentry sentry_version=7, sentry_client=clipboardplus-desktop/{desktop.APP_VERSION}, "
        f"sentry_key={parts.username}"
    )
    _post(url, body, {"Content-Type": "application/x-sentry-envelope", "X-Sentry-Auth": auth})


# -- crashes -----------------------------------------------------------------
def install(component: str, ignore: Iterable[type[BaseException]] = ()) -> None:
    """Report crashes this process does not handle (the main thread and others)."""
    set_component(component)
    skipped: tuple[type[BaseException], ...] = (KeyboardInterrupt, SystemExit, *ignore)
    previous = sys.excepthook

    def hook(kind: type[BaseException], error: BaseException, trace: TracebackType | None) -> None:
        if not issubclass(kind, skipped):
            capture(error.with_traceback(trace), wait=True)
        previous(kind, error, trace)

    sys.excepthook = hook
    previous_thread = threading.excepthook

    def thread_hook(args: threading.ExceptHookArgs) -> None:
        if args.exc_value is not None and not isinstance(args.exc_value, skipped):
            capture(args.exc_value)
        previous_thread(args)

    threading.excepthook = thread_hook


def watch_tk(root: Any) -> None:
    """Report errors in Tk callbacks too (Tk prints them and carries on)."""
    shown = root.report_callback_exception

    def report(kind: type[BaseException], error: BaseException, trace: Any) -> None:
        capture(error.with_traceback(trace))
        shown(kind, error, trace)

    root.report_callback_exception = report
