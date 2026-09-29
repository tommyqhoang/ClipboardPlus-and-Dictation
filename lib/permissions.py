"""Operating-system permissions dictation needs, and what to tell the user about them.

Auto-paste needs permission to press a key in another app (macOS Accessibility, a
helper such as wtype or xdotool on Linux); recording needs microphone access. Each
probe answers "unknown" (None or "unknown") when it cannot tell, and never raises: a
probe that fails must not stop dictation. The messages name the exact place to fix it.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
import shutil
import subprocess
import sys
from typing import Any

import desktop

try:
    import logsetup

    log = logsetup.get_logger("permissions")
except ImportError:
    log = logging.getLogger(__name__)

ACCESSIBILITY_URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
MAC_MICROPHONE_URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone"
WINDOWS_MICROPHONE_URL = "ms-settings:privacy-microphone"
WINDOWS_KEYBOARD_URL = "ms-settings:easeofaccess-keyboard"

# AVAuthorizationStatus values.
_AV_STATUS = {0: "not_determined", 1: "restricted", 2: "denied", 3: "authorized"}


def accessibility_trusted() -> bool | None:
    """Whether this process may control other apps (macOS). None when it can't be told."""
    if desktop.platform_name() != "macos":
        return None
    try:
        import HIServices  # type: ignore[import-not-found,unused-ignore]

        return bool(HIServices.AXIsProcessTrusted())
    except ImportError:
        pass
    except Exception as exc:  # PyObjC bridge trouble: fall through to ctypes.
        log.debug("HIServices trust check failed: %s", exc)
    try:
        path = ctypes.util.find_library("ApplicationServices")
        if not path:
            return None
        framework: Any = ctypes.CDLL(path)
        framework.AXIsProcessTrusted.restype = ctypes.c_bool
        return bool(framework.AXIsProcessTrusted())
    except (OSError, AttributeError) as exc:
        log.debug("AXIsProcessTrusted unavailable: %s", exc)
        return None


def microphone_status() -> str:
    """ "authorized", "denied", "restricted", "not_determined" or "unknown"."""
    if desktop.platform_name() != "macos":
        return "unknown"
    try:
        import AVFoundation  # type: ignore[import-not-found,unused-ignore]

        status = AVFoundation.AVCaptureDevice.authorizationStatusForMediaType_(
            AVFoundation.AVMediaTypeAudio
        )
        return _AV_STATUS.get(int(status), "unknown")
    except ImportError:
        return "unknown"
    except Exception as exc:
        log.debug("microphone authorization probe failed: %s", exc)
        return "unknown"


def linux_audio_server() -> str:
    """ "pipewire", "pulseaudio", "alsa" (a bare sound card) or "none"."""
    if shutil.which("pw-cli") or shutil.which("wpctl"):
        return "pipewire"
    if shutil.which("pactl"):
        return "pulseaudio"
    if shutil.which("arecord"):
        return "alsa"
    return "none"


def open_settings(url: str) -> bool:
    """Open an operating-system settings page; False when nothing could open it."""
    platform = desktop.platform_name()
    try:
        if platform == "windows":
            os.startfile(url)  # type: ignore[attr-defined,unused-ignore]  # noqa: S606
            return True
        opener = "open" if platform == "macos" else "xdg-open"
        if not shutil.which(opener):
            return False
        subprocess.Popen(
            [opener, url],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except (OSError, AttributeError) as exc:
        log.warning("could not open %s: %s", url, exc)
        return False


def session_kind() -> str:
    """ "macos", "windows", "wayland" or "x11" (for the paste helper to pick)."""
    platform = desktop.platform_name()
    if platform != "linux":
        return platform
    return "wayland" if os.environ.get("WAYLAND_DISPLAY") else "x11"


def paste_helper(session: str) -> str:
    return "wtype" if session == "wayland" else "xdotool"


def accessibility_explanation() -> str:
    """Shown before the first paste attempt on macOS, so the system prompt isn't a surprise."""
    return (
        "To paste for you, Clipboard+ presses Command+V in the app you are using. "
        "macOS only allows that after you turn on Clipboard+ under System Settings, "
        "Privacy & Security, Accessibility. Your text is always on the clipboard "
        "either way, so you can also press Command+V yourself."
    )


def paste_failure_message(session: str | None = None, reason: str = "") -> str:
    """What to do when auto-paste failed; the transcript is on the clipboard regardless."""
    session = session or session_kind()
    tail = f" ({reason})" if reason else ""
    if session == "macos":
        return (
            "Copied, but Clipboard+ isn’t allowed to paste for you. Turn it on in System "
            f"Settings, Privacy & Security, Accessibility ({ACCESSIBILITY_URL}), "
            "then press Command+V now." + tail
        )
    if session == "windows":
        return (
            "Copied, but pasting for you didn’t work. Press Ctrl+V now. If it keeps "
            "happening, check that PowerShell is allowed to run and that the app you "
            f"paste into isn’t running as administrator ({WINDOWS_KEYBOARD_URL})." + tail
        )
    helper = paste_helper(session)
    if shutil.which(helper):
        return f"Copied, but {helper} couldn’t paste{tail}. Press Ctrl+V now. " + (
            "Some Wayland desktops (GNOME) don’t allow it; Ctrl+V always works."
            if session == "wayland"
            else "Check that the focused app accepts synthetic keys."
        )
    return (
        f"Copied. To paste for you, install {helper} (for example: sudo apt install "
        f"{helper}, or your distribution’s equivalent) for this {session.upper()} session. "
        "Press Ctrl+V now." + tail
    )


def microphone_message(session: str | None = None, reason: str = "") -> str:
    """The microphone could not start: where to allow it on this operating system."""
    session = session or session_kind()
    tail = f" ({reason})" if reason else ""
    base = "Microphone could not start. "
    if session == "macos":
        return (
            base
            + "Choose a microphone in Settings and allow microphone access in "
            + f"System Settings, Privacy & Security, Microphone ({MAC_MICROPHONE_URL})."
            + tail
        )
    if session == "windows":
        return (
            base
            + "Choose a microphone in Settings and allow microphone access "
            + f"({WINDOWS_MICROPHONE_URL})."
            + tail
        )
    server = linux_audio_server()
    if server == "none":
        hint = "No sound system was found: install PipeWire or PulseAudio (and arecord)."
    elif server == "alsa":
        hint = (
            "Only ALSA was found; install pipewire-pulse if your desktop uses PipeWire, "
            "and check that no other app holds the microphone."
        )
    else:
        hint = (
            "Choose a microphone in Settings, unmute it in your sound settings, and, "
            "in a sandbox (Flatpak/Snap), grant microphone access."
        )
    return base + hint + tail


def microphone_blocked() -> str | None:
    """A reason the microphone is known to be blocked, before trying to record."""
    status = microphone_status()
    if status == "denied":
        return microphone_message("macos", "access was denied")
    if status == "restricted":
        return "Microphone access is restricted on this Mac by a profile or Screen Time."
    return None


def microphone_settings_url(session: str | None = None) -> str | None:
    session = session or session_kind()
    if session == "macos":
        return MAC_MICROPHONE_URL
    if session == "windows":
        return WINDOWS_MICROPHONE_URL
    return None


def tail_of(data: bytes | str | None, limit: int = 300) -> str:
    """The last few lines of a tool's output, for a message or a log line."""
    if not data:
        return ""
    text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    return " ".join(lines[-3:])[-limit:]


if __name__ == "__main__":  # pragma: no cover - a quick manual check.
    print("session:", session_kind(), file=sys.stderr)
    print("accessibility:", accessibility_trusted())
    print("microphone:", microphone_status())
