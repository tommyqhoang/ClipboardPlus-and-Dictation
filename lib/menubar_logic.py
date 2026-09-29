"""The macOS menu bar's decisions, with no PyObjC in sight.

menubar.py draws with AppKit and can only run on a Mac. What it decides to draw (the
menu bar title, the popover's header and rows, keyboard selection, the hotkey's
codes, whether to explain Accessibility) lives here so it is tested on every system.
"""

from __future__ import annotations

import re
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
