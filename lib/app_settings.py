"""Choices and small pure helpers for the Settings page (see App.settings)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

SOUND_CHOICES = (
    ("off", "Off"),
    ("errors", "Errors only"),
    ("all", "Start, stop and errors"),
)

LANGUAGES = ("English", "Multilingual / auto-detect")

# (config key, label) for the switches under Dictation.
VOICE_SWITCHES = (
    ("auto_paste", "Automatically paste into the app I am using"),
    ("overlay", "Show the recording bar (voice levels, then “Copied”)"),
    ("live", "Show a live draft while recording (uses more processing)"),
    ("notifications", "Also show desktop notifications (always on without the bar)"),
)

# (value, title, hint) for where transcription runs.
MODEL_CHOICES = (
    (
        "download",
        "Free on-device AI (recommended)",
        "Private: audio never leaves this computer. One-time 148 MB download.",
    ),
    ("file", "A Whisper model file I already have", ""),
    (
        "service",
        "My own AI service",
        "Fast on any computer. Audio is sent to the service you choose, which may charge.",
    ),
)


def language_label(code: str) -> str:
    """The name shown for a saved language code."""
    return LANGUAGES[0] if code == "en" else LANGUAGES[1]


def language_code(label: str) -> str:
    """The language code saved for a shown name."""
    return "en" if label == LANGUAGES[0] else "auto"


def provider_name(endpoint: str, remote: bool, providers: Mapping[str, tuple[str, str]]) -> str:
    """The provider whose URL is `endpoint`; else the custom entry (remote) or the first."""
    return next(
        (name for name, (url, _) in providers.items() if url and url == endpoint),
        list(providers)[-1] if remote else next(iter(providers)),
    )


def home_text(
    shortcut: str,
    conflict: Any,
    working: bool,
    platform: str,
    detail: str = "",
) -> tuple[str, str]:
    """The Home page's title and subtitle for the state of the dictation shortcut.

    `conflict` is a hotkeys.Conflict (or None); `detail` is why registration failed, as
    recorded by the tray, and is shown instead of a generic hint when there is one.
    """
    paste = "Command\u00a0+\u00a0V" if platform == "macos" else "Ctrl\u00a0+\u00a0V"
    if platform == "linux":
        paste += " (Ctrl\u00a0+\u00a0Shift\u00a0+\u00a0V in a terminal)"
    if conflict is not None:
        return (
            f"{shortcut} is taken by something else",
            f"{conflict.name[0].upper() + conflict.name[1:]} also uses {shortcut} and gets it "
            "first, so dictation doesn’t start. Until you fix it, record from here.",
        )
    if working:
        return (
            f"Press {shortcut} to dictate",
            f"It works in any app: press it, speak, press it again, then paste with {paste}. "
            "You don’t need this window.",
        )
    if platform == "linux":
        return (
            "Set up your shortcut",
            (
                detail
                or "This desktop can’t set shortcuts automatically. In your keyboard settings, "
                f"assign {shortcut} to ~/.local/bin/dictate-toggle."
            )
            + " Until then, record from here.",
        )
    return (
        f"{shortcut} is taken",
        f"Another app already uses {shortcut}. Choose a different shortcut below. "
        "Until then, record from here.",
    )
