"""Audible cues for a dictation session: start, stop and error.

For people who can't (or aren't looking at) the recording pill. Off entirely, errors
only (the default), or all three, chosen by the "sounds" setting. Playing a sound
never blocks and never fails the session: with no player the terminal bell is used,
and with no terminal nothing is played.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path

import desktop

try:
    import logsetup

    log = logsetup.get_logger("cues")
except ImportError:
    log = logging.getLogger(__name__)

MODES = ("off", "errors", "all")
DEFAULT_MODE = "errors"
KINDS = ("start", "stop", "error")

MAC_SOUNDS = {
    "start": "/System/Library/Sounds/Tink.aiff",
    "stop": "/System/Library/Sounds/Pop.aiff",
    "error": "/System/Library/Sounds/Basso.aiff",
}
FREEDESKTOP = Path("/usr/share/sounds/freedesktop/stereo")
LINUX_SOUNDS = {
    "start": ("message", "message.oga"),
    "stop": ("complete", "complete.oga"),
    "error": ("dialog-error", "dialog-error.oga"),
}
# winsound.MessageBeep sound identifiers by name, or (frequency, milliseconds) beeps.
WINDOWS_BEEPS = {"start": (880, 90), "stop": (660, 90), "error": (330, 350)}


def wanted(kind: str, mode: str) -> bool:
    """Whether `mode` asks for a cue of this kind."""
    if kind not in KINDS:
        return False
    return mode == "all" or (mode == "errors" and kind == "error")


def mode_from(value: object) -> str:
    """A valid sounds mode; anything unexpected means the default."""
    return str(value) if str(value) in MODES else DEFAULT_MODE


def _windows(kind: str) -> bool:
    try:
        import winsound  # type: ignore[import-not-found,unused-ignore]
    except ImportError:
        return False
    frequency, length = WINDOWS_BEEPS[kind]
    threading.Thread(
        target=getattr(winsound, "Beep"), args=(frequency, length), daemon=True
    ).start()
    return True


def _spawn(command: list[str]) -> bool:
    try:
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=sys.platform != "win32",
        )
        return True
    except OSError as exc:
        log.debug("could not play a sound with %s: %s", command[0], exc)
        return False


def _bell() -> bool:
    """The terminal bell, when there is a terminal to ring."""
    stream = sys.__stderr__
    try:
        if stream is not None and stream.isatty():
            stream.write("\a")
            stream.flush()
            return True
    except (OSError, ValueError) as exc:
        log.debug("terminal bell failed: %s", exc)
    return False


def play(
    kind: str,
    mode: str = DEFAULT_MODE,
    *,
    platform: str | None = None,
    spawn: Callable[[list[str]], bool] = _spawn,
) -> bool:
    """Play the cue for `kind` if `mode` asks for it; whether anything was played."""
    if not wanted(kind, mode):
        return False
    platform = platform or desktop.platform_name()
    if platform == "macos":
        if shutil.which("afplay") and Path(MAC_SOUNDS[kind]).exists():
            return spawn(["afplay", MAC_SOUNDS[kind]])
    elif platform == "windows":
        if _windows(kind):
            return True
    else:
        event, filename = LINUX_SOUNDS[kind]
        sample = FREEDESKTOP / filename
        if shutil.which("paplay") and sample.exists():
            return spawn(["paplay", str(sample)])
        if shutil.which("canberra-gtk-play"):
            return spawn(["canberra-gtk-play", "-i", event])
        if shutil.which("pw-play") and sample.exists():
            return spawn(["pw-play", str(sample)])
    return _bell()
