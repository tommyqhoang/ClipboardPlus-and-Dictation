"""The macOS menu bar's decisions, with no PyObjC in sight.

menubar.py draws with AppKit and can only run on a Mac. What it decides to draw (the
menu bar title, the popover's header and rows, keyboard selection, the hotkey's
codes, whether to explain Accessibility) lives here so it is tested on every system.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

SOURCE_NAMES = {"desktop": "Desktop", "dictation": "Dictation", "cloud": "Cloud"}
QUERY_LIMIT = 200


def clock(elapsed: int) -> str:
    """Seconds as m:ss."""
    return f"{elapsed // 60}:{elapsed % 60:02d}"


def status_button(phase: str, active: bool, elapsed: int) -> tuple[str, str]:
    """The menu bar button: ("recording" or "idle" image, its title)."""
    if phase == "recording":
        return "recording", f" {clock(elapsed)}"
    if active:
        return "idle", " …"
    return "idle", ""


@dataclass(frozen=True)
class Header:
    """The popover's top line and its Start/Stop button."""

    status: str
    button: str
    enabled: bool
    show_cancel: bool


def popover_header(
    phase: str, active: bool, elapsed: int, ready: bool, hotkey_ok: bool, label: str
) -> Header:
    if phase == "recording":
        return Header(f"Recording… {clock(elapsed)}", "Stop", True, True)
    if active:
        return Header("Transcribing…", "…", False, False)
    if not ready:
        status = "Finish setup to start"
    elif hotkey_ok:
        status = f"Press {label} anywhere to dictate"
    else:
        status = f"{label} is taken — choose another shortcut"
    return Header(status, "Start", True, False)


def normalize_query(text: str) -> str:
    """The search box's text as the history search wants it: trimmed, one-spaced, bounded."""
    return re.sub(r"\s+", " ", text).strip()[:QUERY_LIMIT]


def empty_message(store_available: bool, query: str) -> str:
    """What the empty list says."""
    if not store_available:
        return "Clipboard history isn’t available."
    return "No matches." if query else "Nothing copied yet."


def next_selection(selected: int, delta: int, count: int) -> int:
    """The row after moving `delta` rows, kept inside the list (0 for an empty one)."""
    if count <= 0:
        return 0
    return max(0, min(selected + delta, count - 1))


def row_detail(item: Any, stamp: str) -> str:
    """Hover text: kind, where it came from and when. `stamp` is the formatted date."""
    kind = f"Image {item.width}×{item.height}" if item.kind == "image" else "Text"
    source = SOURCE_NAMES.get(item.source, item.source)
    return f"{kind} · {source} · {stamp}"


# Search-field selectors (from AppKit) mapped to what the popover does.
KEY_ACTIONS = {
    "moveDown:": "down",
    "moveUp:": "up",
    "insertNewline:": "activate",
    "cancelOperation:": "close",
    # macOS's default Emacs-style binding for Ctrl+A just moves the caret; the popover
    # selects everything instead, matching every other platform.
    "moveToBeginningOfParagraph:": "select_all",
}


def key_action(selector: str) -> str:
    """What a search-field selector does: down, up, activate, close, select_all, or ""."""
    return KEY_ACTIONS.get(selector, "")


def carbon_hotkey(shortcut: Any) -> tuple[int, int]:
    """(virtual key code, Carbon modifier mask) for RegisterEventHotKey."""
    return int(shortcut.mac_key_code()), int(shortcut.carbon_modifiers())


def fourcc(code: str) -> int:
    """A four-character code as the integer Carbon wants."""
    return int.from_bytes(code.encode("ascii"), "big")


def needs_accessibility_explanation(trusted: bool | None, already_told: bool) -> bool:
    """Whether to explain Accessibility before dictating: only when macOS says it is off
    (None, meaning unknown, is not a reason to interrupt) and the user hasn't been told."""
    return trusted is False and not already_told


EVENT_HOTKEY_EXISTS = -9878  # eventHotKeyExistsErr: another app (or macOS) owns the keys.
EVENT_HOTKEY_INVALID = -9879  # eventHotKeyInvalidErr: not a shortcut macOS accepts.


@dataclass(frozen=True)
class HotKeyFailure:
    """Why Carbon would not give us a shortcut: which kind of failure, and its OSStatus."""

    kind: str  # "conflict", "invalid", "handler" or "error".
    status: int

    def message(self, label: str, log_path: str) -> str:
        """Plain words for the window, naming the log for anything not the user's doing."""
        if self.kind == "conflict":
            return f"{label} is already used by another app or by macOS. Choose a different one."
        if self.kind == "invalid":
            return (
                f"macOS doesn’t accept {label} as a shortcut (error {self.status}). Choose another."
            )
        if self.kind == "handler":
            return (
                f"macOS wouldn’t let Clipboard+ listen for shortcuts (error {self.status}), so "
                f"{label} can’t work. Quit and reopen Clipboard+. Details: {log_path}"
            )
        return f"macOS refused {label} (error {self.status}). Details: {log_path}"


def registration_failure(status: int, handler_status: int = 0) -> HotKeyFailure | None:
    """Classify the OSStatus of InstallEventHandler and RegisterEventHotKey; None is success."""
    if handler_status != 0:
        return HotKeyFailure("handler", handler_status)
    if status == 0:
        return None
    if status == EVENT_HOTKEY_EXISTS:
        return HotKeyFailure("conflict", status)
    if status == EVENT_HOTKEY_INVALID:
        return HotKeyFailure("invalid", status)
    return HotKeyFailure("error", status)


def guarded(action: Callable[[], Any], log: Any, name: str) -> Callable[[], int]:
    """Wrap a Carbon callback: ctypes swallows a Python exception raised inside one, so
    log it with its traceback and still return noErr (0) to Carbon."""

    def call() -> int:
        try:
            action()
        except Exception:  # noqa: BLE001 - nothing may escape into Carbon.
            log.exception("the %s shortcut handler failed", name)
        return 0

    return call
