"""Test your shortcut: the logic every platform shares (no GUI toolkit, no desktop calls
except the optional GNOME lookup).

A shortcut that "should" work often doesn't (another app or the desktop takes the keys,
the desktop refused the registration), and the person cannot tell why. So every place
that lets them choose a shortcut can also test it: ask them to press it, wait for the
acknowledgement the press leaves behind (hotkeys.record_heard, written by the macOS menu
bar, the Windows tray, or on Linux by the command the desktop runs: hotkeys.VIA_SHORTCUT),
and when nothing arrives, say the likely cause and offer the next shortcut that can work.
"""

from __future__ import annotations

import shlex
from collections.abc import Callable, Collection
from dataclasses import dataclass
from pathlib import Path

import desktop
import dictation as d
import hotkeys
from hotkeys import Shortcut

TIMEOUT = 8.0  # Seconds to wait for a press before saying it was not heard.
# A status file counts as written for a new shortcut when it is at least this new.
FRESH_SLACK = 0.05


def _combos(*table: tuple[tuple[str, ...], str]) -> tuple[Shortcut, ...]:
    return tuple(Shortcut(modifiers, key) for modifiers, key in table)


# The shortcuts to try, in order, when the one in use is not heard. Each platform's list
# starts with the shared default, then the least likely to clash; reserved combinations
# are skipped by candidates() (so ⌥⌘D, which macOS uses for the Dock, never appears).
_DICTATION = {
    "macos": (
        (("shift", "cmd"), "D"),
        (("ctrl", "alt"), "D"),
        (("ctrl", "alt", "shift"), "D"),
        (("ctrl", "shift"), "D"),
        (("ctrl", "alt"), "Space"),
    ),
    "windows": (
        (("shift", "cmd"), "D"),
        (("ctrl", "alt"), "D"),
        (("ctrl", "alt", "shift"), "D"),
        (("ctrl", "shift"), "D"),
        (("ctrl", "alt"), "Space"),
    ),
    "linux": (
        (("shift", "cmd"), "D"),
        (("ctrl", "alt"), "D"),
        (("ctrl", "alt", "shift"), "D"),
        (("ctrl", "shift"), "D"),
        (("alt", "cmd"), "D"),
        (("ctrl", "alt"), "Space"),
    ),
}
_HISTORY = (
    (("shift", "cmd"), "F"),
    (("ctrl", "alt"), "H"),
    (("ctrl", "alt", "shift"), "V"),
    (("ctrl", "shift"), "F"),
    (("ctrl", "alt", "shift"), "F"),
)
CANDIDATES: dict[str, dict[str, tuple[Shortcut, ...]]] = {
    "dictation": {platform: _combos(*table) for platform, table in _DICTATION.items()},
    "history": {platform: _combos(*_HISTORY) for platform in ("macos", "windows", "linux")},
}


def candidates(kind: str, platform: str | None = None) -> tuple[Shortcut, ...]:
    """The ordered shortcuts worth trying on `platform`, without any the system reserves."""
    platform = platform or desktop.platform_name()
    table = CANDIDATES[kind].get(platform, CANDIDATES[kind]["linux"])
    return tuple(item for item in table if not item.problem(platform))


def next_candidate(
    kind: str,
    platform: str,
    current: Shortcut | None,
    tried: Collection[Shortcut] = (),
    other: Shortcut | None = None,
    blocked: Callable[[Shortcut], str] | None = None,
) -> Shortcut | None:
    """The first candidate that is not `current`, was not `tried`, would not collide with
    the other feature's shortcut, and that `blocked` (a live check returning the reason
    it cannot work, or "") lets through; None when the list is used up."""
    for option in candidates(kind, platform):
        if option == current or option in tried:
            continue
        if other is not None and option == other:
            continue
        if blocked is not None and blocked(option):
            continue
        return option
    return None


def desktop_blocked(kind: str, platform: str | None = None) -> Callable[[Shortcut], str]:
    """A `blocked` check that asks GNOME whether another shortcut has the keys (elsewhere
    the desktop only says so when it registers, and the test then finds out)."""
    platform = platform or desktop.platform_name()
    path = hotkeys.GNOME_HISTORY_PATH if kind == "history" else hotkeys.GNOME_PATH

    def blocked(shortcut: Shortcut) -> str:
        if platform != "linux" or hotkeys.detect_backend() != "gnome":
            return ""
        found = hotkeys.gnome_conflict(shortcut, path)
        return found.name if found else ""

    return blocked


@dataclass(frozen=True)
class Assessment:
    """Where a test stands: "waiting", "heard", or "silent" (with `reason`)."""

    phase: str
    reason: str = ""


def registered_at(paths: d.Paths, kind: str) -> float:
    """When the tray or menu bar last wrote this kind's registration status (0 if never)."""
    try:
        return (paths.runtime / hotkeys.STATUS_NAMES[kind]).stat().st_mtime
    except OSError:
        return 0.0


def unknown_cause(shortcut: Shortcut, platform: str) -> str:
    """The likely reasons for silence when nothing specific was recorded."""
    label = shortcut.label(platform)
    if platform == "macos":
        return (
            f"Another app may get {label} first (browsers and editors use it), or macOS "
            "holds it. A different shortcut usually fixes it."
        )
    if platform == "windows":
        return f"Windows or another app may already use {label}."
    return (
        f"Your desktop may not have applied {label} yet (KDE needs a log out and back in), "
        "or another shortcut or app uses those keys."
    )


def assess(
    paths: d.Paths,
    kind: str,
    shortcut: Shortcut,
    started: float,
    now: float,
    fresh: bool = False,
    platform: str | None = None,
    timeout: float = TIMEOUT,
) -> Assessment:
    """The state of a test begun at `started`. `fresh` is set right after the shortcut
    was changed: registration status older than that belongs to the previous shortcut."""
    platform = platform or desktop.platform_name()
    name = hotkeys.STATUS_NAMES[kind]
    current = not fresh or registered_at(paths, kind) >= started - FRESH_SLACK
    pressed = hotkeys.heard_at(paths, kind, shortcut)
    if pressed is not None and pressed >= started - FRESH_SLACK:
        return Assessment("heard")
    if current:
        conflict = hotkeys.shortcut_conflict(paths, name)
        if conflict is not None:
            return Assessment(
                "silent",
                f"{conflict.name[:1].upper() + conflict.name[1:]} also uses "
                f"{shortcut.label(platform)} and gets it first.",
            )
        if not hotkeys.shortcut_working(paths, name):
            reason = hotkeys.shortcut_message(paths, name)
            return Assessment(
                "silent",
                reason or f"{hotkeys.APP_NAME} couldn’t register {shortcut.label(platform)}.",
            )
    if now - started >= timeout:
        return Assessment("silent", unknown_cause(shortcut, platform))
    return Assessment("waiting")


def prompt(shortcut: Shortcut, platform: str | None = None) -> str:
    return f"Press {shortcut.label(platform)} now"


def heard_text(shortcut: Shortcut, platform: str | None = None) -> str:
    return f"✓ It works — heard {shortcut.label(platform)}"


def silent_text(reason: str) -> str:
    return f"Didn’t hear it. {reason}".strip()


def command_parts(kind: str, lib: Path | None = None) -> list[str]:
    """What to bind by hand: the command a shortcut runs (with the acknowledgement marker)."""
    if kind == "history":
        return hotkeys.via_shortcut(hotkeys.history_command(lib or Path(__file__).resolve().parent))
    return hotkeys.via_shortcut(["~/.local/bin/dictate-toggle"])


def command_line(parts: list[str]) -> str:
    """The command as one line to paste (a leading ~ stays unquoted so the shell expands it)."""
    return " ".join(part if part.startswith("~/") else shlex.quote(part) for part in parts)


def manual_text(kind: str, shortcut: Shortcut, platform: str, command: list[str]) -> str:
    """The last resort: how to set it by hand where the desktop's own settings live."""
    if platform == "linux":
        return hotkeys.manual_instructions(shortcut, command, hotkeys.detect_backend())
    keys = "Ctrl+Alt (⌃⌥) and a letter" if platform == "macos" else "Ctrl+Alt and a letter"
    return f"Choose your own keys with Change…: hold {keys}, then Save."
