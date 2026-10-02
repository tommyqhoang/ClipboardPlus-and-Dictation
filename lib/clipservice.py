"""The clipboard service: a headless process that keeps the clipboard history.

It owns capture (through the platform watcher), the local database and its retention.
The tray or menu bar starts it while Clipboard is enabled; the window and the dictation
engine reach it only through the database, `clip-status.json` and small signal files in
the runtime folder, so a stall here can never freeze them.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import stat
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

import clipboardplus as cp
import clipstore
import clipsync
import clipwatch
import desktop
import dictation as d
import hotkeys
import telemetry

OWN_WRITE_SECONDS = 10.0  # How long the app's own clipboard write is recognised.
PRUNE_SECONDS = 300.0
STATUS_SECONDS = 10.0  # Heartbeat: others see the service is alive.
STALE_SECONDS = 35.0
RETRY_SECONDS = 30.0
ERROR_PAUSE_SECONDS = 1.0  # Keeps a failing watcher from spinning the CPU.
SYNC_SOON_SECONDS = 5.0  # A local change is sent this soon.
log = logging.getLogger("clipservice")  # Goes to stderr; the tray keeps it in logs/clipservice.log.
SYNC_NOW = "clip-sync-now"  # Runtime file: sync at once (Settings, dictation).
QUIT = "clip-quit"  # Runtime file: the tray asks the service to stop.
PID_FILE = "clipservice.pid"
MAX_SIGNAL_BYTES = 16  # Signal files carry a word or a digit; anything longer is not ours.


def _open_claimed(path: Path, flags: int) -> int:
    """Open a claimed file. On Windows a reader that lost the rename race can still hold
    the file for a moment, which makes the winner's open fail with a sharing violation:
    retry briefly instead of dropping a request that was correctly claimed."""
    for attempt in range(20):
        try:
            return os.open(path, flags)
        except PermissionError:
            if sys.platform != "win32" or attempt == 19:
                raise
            time.sleep(0.01)
    raise AssertionError("unreachable")


def consume_signal(path: Path, accepted: frozenset[str]) -> bool:
    """Claim a control file and say whether it was a valid request.

    The file is first renamed to a name only this call knows (an atomic step, so two
    readers, or a reader and the writer's next write, can never both take the same
    request), and only then read. It must be a small regular file of this user (a link,
    a folder, a foreign file or a long one is ignored) whose text is in `accepted`.
    Whatever it was, it is gone afterwards, so an invalid file is not looked at again.
    """
    claimed = path.with_name(f".{path.name}.{os.getpid()}.{os.urandom(4).hex()}.claimed")
    try:
        os.rename(path, claimed)
    except OSError:
        return False  # Absent, or another reader won the race.
    try:
        info = os.lstat(claimed)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_size > MAX_SIGNAL_BYTES
            or (sys.platform != "win32" and info.st_uid != os.getuid())
        ):
            return False
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(_open_claimed(claimed, flags), "rb") as stream:
            text = stream.read(MAX_SIGNAL_BYTES + 1).decode("utf-8", "replace").strip()
        return text in accepted
    except OSError:
        return False
    finally:
        try:
            if stat.S_ISDIR(os.lstat(claimed).st_mode):
                os.rmdir(claimed)
            else:
                os.unlink(claimed)
        except OSError:
            pass


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
        if cp.linked(paths.config.parent):
            d.atomic(paths.runtime / SYNC_NOW, "1")  # Send it to the account now.
    except (OSError, sqlite3.Error, clipstore.StoreError, d.DictationError):
        return


def write_status(
    paths: d.Paths,
    state: str,
    count: int = 0,
    message: str = "",
    clock: Callable[[], float] = time.time,
    sync: str = "off",
    synced: float = 0.0,
) -> None:
    d.private_dir(paths.runtime)
    d.atomic(
        paths.runtime / "clip-status.json",
        json.dumps(
            {
                "state": state,
                "count": count,
                "message": message,
                "sync": sync,  # off, syncing, ok, offline, error or auth
                "synced": synced,  # When the account last synced fine.
                "updated": clock(),
                "pid": os.getpid(),
            }
        ),
    )


def read_status(paths: d.Paths, clock: Callable[[], float] = time.time) -> dict[str, Any]:
    """The service's last report; "stopped" when there is none or it stopped updating."""
    stopped: dict[str, Any] = {
        "state": "stopped",
        "count": 0,
        "message": "",
        "sync": "off",
        "synced": 0.0,
        "updated": 0.0,
    }
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
        self._stamp: tuple[int, int] | None = None
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


class SyncEngine(Protocol):
    state: str

    def run_once(self) -> clipsync.Report: ...
    def retry_delay(self) -> float | None: ...


def _default_engine(store: clipstore.Store, key: str) -> SyncEngine:
    return clipsync.Engine(store, cp.Cloud(key))


class Syncer:
    """Runs the sync engine for the service: on a schedule, soon after a local change,
    or on request, always on its own thread so a slow network never delays capture."""

    def __init__(
        self,
        store: clipstore.Store,
        paths: d.Paths,
        clock: Callable[[], float] = time.time,
        engine_factory: Callable[[clipstore.Store, str], SyncEngine] = _default_engine,
        threaded: bool = True,
    ) -> None:
        self._store = store
        self._paths = paths
        self._clock = clock
        self._factory = engine_factory
        self._threaded = threaded
        self._engine: SyncEngine | None = None
        self._stamp: tuple[int, int, int] | None = None
        self._due = 0.0
        self._thread: threading.Thread | None = None
        self._crashed = False
        self.synced = 0.0

    @property
    def state(self) -> str:
        """off, syncing, ok, offline, error or auth."""
        if self._engine is None:
            return "off"
        if self._running():
            return "syncing"
        if self._crashed:
            return "error"
        return "syncing" if self._engine.state == "idle" else self._engine.state

    def poke(self, now: float) -> None:
        """Something changed here: send it soon."""
        self._due = min(self._due, now + SYNC_SOON_SECONDS)

    def step(self, now: float) -> None:
        if self._running():
            return
        self._reload(now)
        if consume_signal(self._paths.runtime / SYNC_NOW, frozenset({"", "1"})):
            if self._engine is not None:
                self._due = now
        engine = self._engine
        if engine is None or engine.state == "auth" or now < self._due:
            return
        if self._threaded:
            self._thread = threading.Thread(target=self._run, args=(engine,), daemon=True)
            self._thread.start()
        else:
            self._run(engine)

    def wait(self, timeout: float) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def close(self) -> None:
        self.wait(30.0)

    def _running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _reload(self, now: float) -> None:
        """Follow the saved key: a new, removed or re-saved key starts a fresh engine."""
        path = cp.key_path(self._paths.config.parent)
        try:
            info = path.stat()
            stamp: tuple[int, int, int] | None = (info.st_mtime_ns, info.st_size, info.st_ino)
        except OSError:
            stamp = None
        if stamp == self._stamp:
            return
        key = cp.read_key(self._paths.config.parent) if stamp else ""
        try:
            info = path.stat()  # Reading may have moved a plaintext key into the keychain.
            stamp = (info.st_mtime_ns, info.st_size, info.st_ino) if stamp else None
        except OSError:
            stamp = None
        self._stamp = stamp
        if self._engine is not None:
            # A round for the previous key may have written account ids after the
            # window reset them. Do this after that round finishes, even when a new
            # key was saved immediately (without an intervening disconnected step).
            self._store.reset_sync()
        self._engine = self._factory(self._store, key) if key else None
        self._crashed = False
        self._due = now

    def _run(self, engine: SyncEngine) -> None:
        try:
            engine.run_once()
        except Exception as exc:  # noqa: BLE001 - a bug or odd server data must not loop or kill the service.
            log.error("sync crashed", exc_info=exc)
            telemetry.capture(exc, stage="sync")
            self._crashed = True
            self._due = self._clock() + clipsync.BACKOFF_MAX_SECONDS
            return
        self._crashed = False
        finished = self._clock()
        delay = engine.retry_delay()
        self._due = finished + (clipsync.INTERVAL_SECONDS if delay is None else delay)
        if engine.state == "ok":
            self.synced = finished


class Service:
    def __init__(
        self,
        store: clipstore.Store,
        watcher: clipwatch.Watcher,
        settings: Callable[[], hotkeys.ClipboardSettings],
        enabled: Callable[[], bool],
        paths: d.Paths,
        clock: Callable[[], float] = time.time,
        syncer: Syncer | None = None,
    ) -> None:
        self._syncer = syncer
        self._store = store
        self._watcher = watcher
        self._settings = settings
        self._enabled = enabled
        self._paths = paths
        self._clock = clock
        self._error = ""
        self._next_prune = clock() + PRUNE_SECONDS
        self._status: tuple[str, int, str, str] | None = None
        self._status_at = 0.0
        # Counted for the daily statistics (numbers only, never what was copied).
        self._day = time.strftime("%Y-%m-%d", time.localtime(clock()))
        self.copied = {"text": 0, "image": 0}

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
        if self._syncer is not None:
            try:
                self._syncer.step(now)
            except (OSError, sqlite3.Error):
                pass  # Syncing is optional: it must never stop the capturing.
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
                    self.copied["text"] += 1
                    self._changed(now)
            elif clip.image_png and settings.images:
                self._store.add_image(clip.image_png, "desktop", now)
                self.copied["image"] += 1
        except sqlite3.Error:
            self._fail("The clipboard history is busy; retrying.", now)
            return False
        except OSError:
            self._fail("The clipboard history can’t be written (is the disk full?); retrying.", now)
            return False
        return True

    def _changed(self, now: float) -> None:
        if self._syncer is not None:
            self._syncer.poke(now)

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
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        if today != self._day:
            self.send_statistics()
            self._day = today
        self._report(now)

    def send_statistics(self, wait: bool = False) -> None:
        """One day's clipboard use, as counts, then start counting again."""
        if not any(self.copied.values()):
            return
        try:
            total = self._store.count()
        except sqlite3.Error:
            total = 0
        telemetry.event(
            "clipboard_daily",
            wait=wait,
            texts=self.copied["text"],
            images=self.copied["image"],
            history_size=total,
            sync=self._syncer.state if self._syncer is not None else "off",
        )
        self.copied = {"text": 0, "image": 0}

    def _fail(self, message: str, now: float) -> None:
        if message != self._error:
            telemetry.capture(message=message, level="warning", stage="capture")
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
        sync = self._syncer.state if self._syncer is not None else "off"
        report = (state, count, self._error, sync)
        if report != self._status or now - self._status_at >= STATUS_SECONDS:
            try:
                synced = self._syncer.synced if self._syncer is not None else 0.0
                write_status(self._paths, state, count, self._error, lambda: now, sync, synced)
            except OSError:
                return
            self._status, self._status_at = report, now


def _quit_requested(paths: d.Paths) -> bool:
    return consume_signal(paths.runtime / QUIT, frozenset({"quit"}))


def _store_problem(paths: d.Paths, exc: Exception) -> str:
    """Say what's wrong; an unreadable database is set aside so capture can start over."""
    if isinstance(exc, clipstore.StoreError):
        return str(exc)
    if isinstance(exc, sqlite3.OperationalError):
        return "The clipboard history is busy; retrying."
    if isinstance(exc, OSError):
        return "The clipboard history can’t be opened. Check storage space and folder permissions."
    database = paths.clipboard / "clips.db"
    kept = database.with_name(f"clips.db.damaged-{int(time.time())}")
    try:
        for suffix in ("", "-wal", "-shm"):
            part = database.with_name(database.name + suffix)
            if part.exists():
                part.rename(kept.with_name(kept.name + suffix))
    except OSError:
        return "The clipboard history file is damaged and could not be set aside."
    return (
        f"The clipboard history file was damaged, so a new one was started ({kept.name} was kept)."
    )


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
        return 0  # Already running: the lock is held by the one live service.
    try:
        d.atomic(paths.runtime / PID_FILE, str(os.getpid()))  # For diagnosis only.
    except OSError:
        pass
    log.info("clipboard service started (pid %d)", os.getpid())

    def nap(seconds: float) -> None:
        # Sleeps in short slices so a quit request is honoured promptly.
        if sleep is not None:
            sleep(seconds)
            return
        end = time.monotonic() + seconds
        while time.monotonic() < end and not (paths.runtime / QUIT).exists():
            time.sleep(min(1.0, max(0.0, end - time.monotonic())))

    prefs = CachedPreferences(hotkeys.Preferences(paths))
    store: clipstore.Store | None = None
    syncer: Syncer | None = None
    watcher: clipwatch.Watcher | None = None
    service: Service | None = None
    try:
        while not _quit_requested(paths) and prefs.enabled():
            if watcher is None:
                try:
                    watcher = watcher_factory()
                except clipwatch.Unavailable as exc:
                    log.warning("clipboard watcher unavailable: %s", exc)
                    telemetry.capture(exc, level="warning", stage="watcher")
                    write_status(paths, "error", 0, str(exc), clock)
                    nap(retry_seconds)
                    continue
                if store is None:
                    try:
                        store = clipstore.Store(paths.clipboard)
                    except (clipstore.StoreError, sqlite3.DatabaseError, OSError) as exc:
                        log.error("clipboard history cannot be opened: %s", exc)
                        telemetry.capture(exc, level="warning", stage="store")
                        write_status(paths, "error", 0, _store_problem(paths, exc), clock)
                        watcher.close()
                        watcher = None  # Retry the entire initialization after storage recovers.
                        nap(retry_seconds)
                        continue
                syncer = syncer or Syncer(store, paths, clock)
                service = Service(
                    store, watcher, prefs.settings, prefs.enabled, paths, clock, syncer
                )
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
        if service is not None:
            service.send_statistics(wait=True)  # The day so far, before quitting.
        if watcher is not None:
            watcher.close()
        if syncer is not None:
            syncer.close()
        if store is not None:
            store.close()
        write_status(paths, "stopped", 0, "", clock)
        (paths.runtime / PID_FILE).unlink(missing_ok=True)
        os.close(lock)
        log.info("clipboard service stopped")
    return 0


def main() -> int:
    os.umask(0o077)
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    telemetry.install("clipboard")
    return run(d.Paths())


if __name__ == "__main__":
    sys.exit(main())
