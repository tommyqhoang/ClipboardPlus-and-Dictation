"""What the tray and the menu bar do about the clipboard service.

Both apps call this instead of duplicating the logic: start and supervise the service
while Clipboard is enabled, pause and resume capture, and describe its state in one line.
"""

from __future__ import annotations

import dataclasses
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import clipservice
import clipstore
import desktop
import dictation as d
import hotkeys

HERE = Path(__file__).resolve().parent
RESTART_SECONDS = 5.0  # Least time between starts.
BACKOFF_SECONDS = 60.0  # After CRASH_LIMIT starts within CRASH_WINDOW.
CRASH_WINDOW = 60.0
CRASH_LIMIT = 3
PREVIEW_CHARS = 60  # One line of a clip in the menu bar popover or the tray menu.


def preview_text(item: clipstore.Item, width: int = PREVIEW_CHARS) -> str:
    """One line for a clip in a menu: its label, text or image size, kept to `width`."""
    text = (
        item.label
        or item.text
        or (f"Image ({item.width}×{item.height})" if item.kind == "image" else "")
    )
    flat = " ".join(text.split())
    return flat if len(flat) <= width else flat[:width] + "…"


class ClipboardControl:
    def __init__(
        self,
        paths: d.Paths,
        prefs: hotkeys.Preferences,
        *,
        command: list[str] | None = None,
        popen: Callable[..., Any] = subprocess.Popen,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self._paths = paths
        self._prefs = prefs
        self._command = command or desktop.relaunch("clipservice")
        self._popen = popen
        self._clock = clock
        self._wall = wall
        self._process: Any = None
        self._starts: list[float] = []
        self._not_before = 0.0
        self._stamp: tuple[int, int] | None = None
        self._features = hotkeys.Features()
        self._settings = hotkeys.ClipboardSettings()
        self._history: hotkeys.Shortcut | None = hotkeys.DEFAULT_HISTORY
        self._reported: tuple[Any, ...] | None = None

    # -- preferences -------------------------------------------------------
    def _refresh(self) -> None:
        stamp = self._prefs.stamp()
        if stamp != self._stamp:
            self._stamp = stamp
            self._features = self._prefs.features()
            self._settings = self._prefs.clipboard()
            self._history = self._prefs.history_shortcut()

    def features(self) -> hotkeys.Features:
        self._refresh()
        return self._features

    def settings(self) -> hotkeys.ClipboardSettings:
        self._refresh()
        return self._settings

    def history_shortcut(self) -> hotkeys.Shortcut | None:
        """The shortcut that opens the history, while Clipboard is on; otherwise None."""
        self._refresh()
        return self._history if self._features.clipboard else None

    def changed(self) -> bool:
        """Whether features, clipboard settings or the history shortcut changed since last asked."""
        current = (self.features(), self.settings(), self.history_shortcut())
        if current == self._reported:
            return False
        self._reported = current
        return True

    # -- pausing -----------------------------------------------------------
    def paused(self) -> bool:
        return self.settings().paused(self._wall())

    def pause(self, seconds: float | None) -> None:
        """Stop capturing for `seconds`, or until resumed when None."""
        until = -1.0 if seconds is None else self._wall() + seconds
        self._prefs.save(clipboard=dataclasses.replace(self.settings(), paused_until=until))
        self._stamp = None

    def resume(self) -> None:
        self._prefs.save(clipboard=dataclasses.replace(self.settings(), paused_until=0.0))
        self._stamp = None

    # -- the service process -----------------------------------------------
    def supervise(self) -> None:
        """Keep one clipboard service running while Clipboard is enabled."""
        if not self.features().clipboard:
            return
        if self._process is not None and self._process.poll() is None:
            return
        now = self._clock()
        if now < self._not_before or clipservice.running(self._paths):
            return
        self._process = self._popen(
            self._command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **desktop.process_options(detached=True),
        )
        self._starts = [t for t in self._starts if now - t < CRASH_WINDOW] + [now]
        crashing = len(self._starts) >= CRASH_LIMIT
        self._not_before = now + (BACKOFF_SECONDS if crashing else RESTART_SECONDS)

    def stop(self) -> None:
        """Ask a service this app started to quit (it restarts with the next launch)."""
        if self._process is not None and self._process.poll() is None:
            # Only a running service reads it; a stale file would stop the next one.
            d.private_dir(self._paths.runtime)
            d.atomic(self._paths.runtime / "clip-quit", "quit")

    # -- display -----------------------------------------------------------
    def status_line(self, clock: Callable[[], float] = time.time) -> str:
        if not self.features().clipboard:
            return ""
        status = clipservice.read_status(self._paths, clock)
        state, count = status["state"], int(status.get("count", 0))
        if state == "paused":
            return "Clipboard: paused"
        if state == "error":
            return f"Clipboard: {status.get('message') or 'capture is not working'}"
        if state == "capturing":
            if count == 0:
                return "Clipboard: nothing copied yet"
            return f"Clipboard: {count} item{'s' if count != 1 else ''}"
        return "Clipboard: starting…" if state == "stopped" else ""
