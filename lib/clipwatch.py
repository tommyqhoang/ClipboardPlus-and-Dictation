"""What every platform's clipboard watcher provides.

A watcher blocks until the clipboard changes and returns what was copied. Each
operating system has its own module (`clipwatch_linux`, `clipwatch_macos`,
`clipwatch_windows`); the clipboard service only sees this interface.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import clipstore
import desktop


class Unavailable(Exception):
    """Clipboard watching is not possible here (the message says why)."""


@dataclass(frozen=True)
class Clip:
    """One clipboard change. A concealed clip (a password manager's) carries no content."""

    text: str = ""
    image_png: bytes = b""
    concealed: bool = False


@runtime_checkable
class Watcher(Protocol):
    def next_change(self, timeout: float) -> Clip | None:
        """Wait up to `timeout` seconds; None when nothing usable was copied."""
        ...

    def close(self) -> None: ...


# A copied API key, access token or private key is treated like a password manager's
# secret: never kept, never synced. Only well-known formats, so ordinary text is kept.
_SECRET = re.compile(
    r"(?:sk-|sk_live_|rk_live_|gsk_|ghp_|gho_|ghu_|ghs_|ghr_|github_pat_|glpat-|cp_live_"
    r"|xox[abprs]-|AIza)[A-Za-z0-9_\-]{16,}"
    r"|AKIA[0-9A-Z]{16}"
)
_PRIVATE_KEY = re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----")


def looks_secret(text: str) -> bool:
    """A single copied token in a known secret format, or a private key block."""
    token = text.strip()
    if _PRIVATE_KEY.search(token):
        return True
    return len(token) <= 400 and bool(_SECRET.fullmatch(token))


def limit_clip(
    clip: Clip,
    max_text: int = clipstore.MAX_TEXT_BYTES,
    max_image: int = clipstore.MAX_IMAGE_BYTES,
) -> Clip | None:
    """Drop oversize or blank parts; None when nothing is left to store."""
    if clip.concealed or looks_secret(clip.text):
        return Clip(concealed=True)
    text = clip.text if 0 < len(clip.text.encode("utf-8", "replace")) <= max_text else ""
    if not text.strip():
        text = ""
    image = clip.image_png if 0 < len(clip.image_png) <= max_image else b""
    if not text and not image:
        return None
    return Clip(text=text, image_png=image)


_PNG_END = b"IEND\xaeB`\x82"


def trim_png(data: bytes) -> bytes:
    """A PNG without trailing bytes: some clipboards return a block larger than the image."""
    end = data.rfind(_PNG_END)
    return data if end == -1 else data[: end + len(_PNG_END)]


def _platform_watcher(platform: str) -> Watcher:
    # Imported lazily: each module needs libraries that exist only on its platform.
    if platform == "linux":
        import clipwatch_linux

        return clipwatch_linux.create()
    if platform == "macos":
        import clipwatch_macos

        return clipwatch_macos.create()
    if platform == "windows":
        import clipwatch_windows

        return clipwatch_windows.create()
    raise Unavailable(f"Clipboard history is not supported on {platform}.")


def create_watcher(platform: str | None = None) -> Watcher:
    """The watcher for this system, or Unavailable."""
    return _platform_watcher(platform or desktop.platform_name())
