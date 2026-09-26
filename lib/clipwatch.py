"""What every platform's clipboard watcher provides.

A watcher blocks until the clipboard changes and returns what was copied. Each
operating system has its own module (`clipwatch_linux`, `clipwatch_macos`,
`clipwatch_windows`); the clipboard service only sees this interface.
"""

from __future__ import annotations

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


def limit_clip(
    clip: Clip,
    max_text: int = clipstore.MAX_TEXT_BYTES,
    max_image: int = clipstore.MAX_IMAGE_BYTES,
) -> Clip | None:
    """Drop oversize or blank parts; None when nothing is left to store."""
    if clip.concealed:
        return Clip(concealed=True)
    text = clip.text if 0 < len(clip.text.encode("utf-8", "replace")) <= max_text else ""
    if not text.strip():
        text = ""
    image = clip.image_png if 0 < len(clip.image_png) <= max_image else b""
    if not text and not image:
        return None
    return Clip(text=text, image_png=image)


def _platform_watcher(platform: str) -> Watcher:
    # Imported lazily: each module needs libraries that exist only on its platform.
    if platform == "linux":
        import clipwatch_linux

        return clipwatch_linux.create()
    raise Unavailable(f"Clipboard history is not supported on {platform}.")


def create_watcher(platform: str | None = None) -> Watcher:
    """The watcher for this system, or Unavailable."""
    return _platform_watcher(platform or desktop.platform_name())
