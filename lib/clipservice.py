"""The clipboard service: a headless process that keeps the clipboard history.

It owns capture (through the platform watcher), the local database and its retention.
The tray or menu bar starts it while Clipboard is enabled; the window and the dictation
engine reach it only through the database, `clip-status.json` and small signal files in
the runtime folder, so a stall here can never freeze them.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import time
from collections.abc import Callable
from typing import Any

import clipstore
import clipwatch
import desktop
import dictation as d
import hotkeys

OWN_WRITE_SECONDS = 10.0  # How long the app's own clipboard write is recognised.
PRUNE_SECONDS = 300.0
STATUS_SECONDS = 10.0  # Heartbeat: others see the service is alive.
STALE_SECONDS = 35.0
RETRY_SECONDS = 30.0
ERROR_PAUSE_SECONDS = 1.0  # Keeps a failing watcher from spinning the CPU.


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def mark_own_write(paths: d.Paths, text: str, clock: Callable[[], float] = time.time) -> None:
    """Tell the service that the app itself is about to put `text` on the clipboard.

    The dictation engine records its transcript directly, so the copy it then makes must
    not be captured a second time.
    """
    d.private_dir(paths.runtime)
    d.atomic(
        paths.runtime / "clip-ignore.json",
        json.dumps({"sha": _sha(text), "at": clock()}),
    )


def record_transcript(paths: d.Paths, text: str, clock: Callable[[], float] = time.time) -> None:
    """Keep a dictation transcript in the clipboard history (only while Clipboard is on).

    Never raises: a broken history must not cost the user their dictation.
    """
    try:
        if not hotkeys.Preferences(paths).features().clipboard:
            return
        mark_own_write(paths, text, clock)  # Before the copy, so the watcher skips it.
        store = clipstore.Store(paths.clipboard)
        try:
            store.add_text(text, "dictation", clock())
        finally:
            store.close()
    except (OSError, sqlite3.Error, clipstore.StoreError, d.DictationError):
        return


def write_status(
    paths: d.Paths,
    state: str,
    count: int = 0,
    message: str = "",
    clock: Callable[[], float] = time.time,
) -> None:
    d.private_dir(paths.runtime)
    d.atomic(
        paths.runtime / "clip-status.json",
        json.dumps(
            {
                "state": state,
                "count": count,
                "message": message,
                "updated": clock(),
                "pid": os.getpid(),
            }
        ),
    )


def read_status(paths: d.Paths, clock: Callable[[], float] = time.time) -> dict[str, Any]:
    """The service's last report; "stopped" when there is none or it stopped updating."""
    stopped: dict[str, Any] = {"state": "stopped", "count": 0, "message": "", "updated": 0.0}
    try:
        status = d.read_json(paths.runtime / "clip-status.json")
    except (d.DictationError, OSError):
        return stopped
    updated = status.get("updated")
    if not isinstance(updated, (int, float)) or clock() - updated > STALE_SECONDS:
        return stopped
    return stopped | status


def running(paths: d.Paths) -> bool:
    """Whether a clipboard service holds its lock."""
    d.private_dir(paths.runtime)
    descriptor = desktop.lock(paths.runtime / "clipservice.lock")
    if descriptor is None:
        return True
    os.close(descriptor)
    return False


class CachedPreferences:
    """Preferences, re-read only when the file changes (the service asks every second)."""

    def __init__(self, prefs: hotkeys.Preferences) -> None:
        self._prefs = prefs
        self._stamp: float | None = None
        self._features = hotkeys.Features()
        self._settings = hotkeys.ClipboardSettings()
        self._refresh()

    def _refresh(self) -> None:
        stamp = self._prefs.stamp()
        if stamp != self._stamp:
            self._stamp = stamp
            self._features = self._prefs.features()
            self._settings = self._prefs.clipboard()

    def enabled(self) -> bool:
        self._refresh()
        return self._features.clipboard

    def settings(self) -> hotkeys.ClipboardSettings:
        self._refresh()
        return self._settings


class Service:
    def __init__(
        self,
        store: clipstore.Store,
        watcher: clipwatch.Watcher,
        settings: Callable[[], hotkeys.ClipboardSettings],
        enabled: Callable[[], bool],
        paths: d.Paths,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = store
        self._watcher = watcher
        self._settings = settings
        self._enabled = enabled
        self._paths = paths
        self._clock = clock
        self._error = ""
        self._next_prune = clock() + PRUNE_SECONDS
        self._status: tuple[str, int, str] | None = None
        self._status_at = 0.0

    def paused(self) -> bool:
        return self._settings().paused(self._clock())

    def step(self, timeout: float = 1.0) -> bool:
        """One pass: wait for a copy, store it, keep the history tidy.

        Returns False when something failed, so the caller can pause before retrying.
        Raises clipwatch.Unavailable when the watcher is unusable and must be replaced.
        """
        now = self._clock()
        ok = True
        try:
            clip = self._watcher.next_change(timeout)
        except clipwatch.Unavailable as exc:
            self._fail(str(exc) or "Clipboard capture is not available.", now)
            raise
        except (OSError, ValueError):
            self._fail("Clipboard capture hit a problem and is retrying.", now)
            clip, ok = None, False
        else:
            self._error = ""
        if clip is not None:
            ok = self._capture(clip, now) and ok
        self._maintain(now)
        return ok

    def _capture(self, clip: clipwatch.Clip, now: float) -> bool:
        if not self._enabled() or clip.concealed:
            return True
        settings = self._settings()
        if settings.paused(now):
            return True
        try:
            if clip.text:
                if not self._is_own_write(clip.text, now):
                    self._store.add_text(clip.text, "desktop", now)
            elif clip.image_png and settings.images:
                self._store.add_image(clip.image_png, "desktop", now)
        except sqlite3.Error:
            self._fail("The clipboard history is busy; retrying.", now)
            return False
        return True

    def _is_own_write(self, text: str, now: float) -> bool:
        try:
            marker = d.read_json(self._paths.runtime / "clip-ignore.json")
        except (d.DictationError, OSError):
            return False
        at = marker.get("at")
        return (
            marker.get("sha") == _sha(text)
            and isinstance(at, (int, float))
            and 0 <= now - at <= OWN_WRITE_SECONDS
        )

    def _maintain(self, now: float) -> None:
        if now >= self._next_prune:
            settings = self._settings()
            try:
                self._store.prune(settings.keep_items, settings.keep_days, now=now)
            except (sqlite3.Error, OSError):
                pass  # Retried on the next schedule.
            self._next_prune = now + PRUNE_SECONDS
        self._report(now)

    def _fail(self, message: str, now: float) -> None:
        self._error = message
        self._report(now)

    def _report(self, now: float) -> None:
        if self._error:
            state = "error"
        elif not self._enabled():
            state = "off"
        elif self._settings().paused(now):
            state = "paused"
        else:
            state = "capturing"
        try:
            count = self._store.count()
        except sqlite3.Error:
            count = 0
        report = (state, count, self._error)
        if report != self._status or now - self._status_at >= STATUS_SECONDS:
            try:
                write_status(self._paths, state, count, self._error, lambda: now)
            except OSError:
                return
            self._status, self._status_at = report, now


def _quit_requested(paths: d.Paths) -> bool:
    request = paths.runtime / "clip-quit"
    if request.exists():
        request.unlink(missing_ok=True)
        return True
    return False


def run(
    paths: d.Paths,
    *,
    watcher_factory: Callable[[], clipwatch.Watcher] = clipwatch.create_watcher,
    sleep: Callable[[float], None] | None = None,
    retry_seconds: float = RETRY_SECONDS,
    clock: Callable[[], float] = time.time,
) -> int:
    """Capture until asked to quit or until Clipboard is turned off."""
    d.private_dir(paths.runtime)
    lock = desktop.lock(paths.runtime / "clipservice.lock")
    if lock is None:
        return 0  # Already running.

    def nap(seconds: float) -> None:
        # Sleeps in short slices so a quit request is honoured promptly.
        if sleep is not None:
            sleep(seconds)
            return
        end = time.monotonic() + seconds
        while time.monotonic() < end and not (paths.runtime / "clip-quit").exists():
            time.sleep(min(1.0, max(0.0, end - time.monotonic())))

    prefs = CachedPreferences(hotkeys.Preferences(paths))
    store: clipstore.Store | None = None
    watcher: clipwatch.Watcher | None = None
    service: Service | None = None
    try:
        while not _quit_requested(paths) and prefs.enabled():
            if watcher is None:
                try:
                    watcher = watcher_factory()
                except clipwatch.Unavailable as exc:
                    write_status(paths, "error", 0, str(exc), clock)
                    nap(retry_seconds)
                    continue
                store = store or clipstore.Store(paths.clipboard)
                service = Service(store, watcher, prefs.settings, prefs.enabled, paths, clock)
                continue  # Re-check for a quit request before waiting on the clipboard.
            assert service is not None
            try:
                ok = service.step(1.0)
            except clipwatch.Unavailable:
                watcher.close()
                watcher = None
                nap(retry_seconds)
                continue
            if not ok:
                nap(ERROR_PAUSE_SECONDS)
    finally:
        if watcher is not None:
            watcher.close()
        if store is not None:
            store.close()
        write_status(paths, "stopped", 0, "", clock)
        os.close(lock)
    return 0


def main() -> int:
    os.umask(0o077)
    return run(d.Paths())


if __name__ == "__main__":
    sys.exit(main())
