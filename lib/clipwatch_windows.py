"""Windows clipboard watching.

The clipboard's sequence number (a counter Windows bumps on every change) is polled, so
no hidden window or message loop is needed. Content is read with the clipboard open
briefly, retrying if another program holds it. Clips that ask to stay out of clipboard
history (`ExcludeClipboardContentFromMonitorProcessing`, `CanIncludeInClipboardHistory`
or `CanUploadToCloudClipboard` set to 0), as password managers do, are never read.
"""

from __future__ import annotations

import ctypes
import io
import time
from collections.abc import Callable
from typing import Any, Protocol

import clipstore
from clipwatch import Clip, Unavailable, limit_clip, trim_png

CF_DIB = 8
CF_UNICODETEXT = 13
PNG_FORMAT = "PNG"
EXCLUDE_FORMAT = "ExcludeClipboardContentFromMonitorProcessing"
HISTORY_FORMAT = "CanIncludeInClipboardHistory"
CLOUD_FORMAT = "CanUploadToCloudClipboard"
OPEN_ATTEMPTS = 5  # OpenClipboard fails while another program has it open.
OPEN_RETRY_SECONDS = 0.02
DIB_FACTOR = 4  # A bitmap is far larger than the PNG made from it; cap the raw size.


class Win32(Protocol):
    """The slice of the Win32 clipboard API the watcher uses (faked in tests)."""

    def sequence(self) -> int: ...

    def open(self) -> bool: ...

    def close(self) -> None: ...

    def has(self, kind: int | str) -> bool: ...

    def text(self) -> str | None: ...

    def data(self, kind: int | str) -> bytes | None: ...

    def dword(self, kind: str) -> int | None: ...


class _Busy:
    """Marks a read that could not open the clipboard, to be retried on the next poll."""


_BUSY = _Busy()


class WindowsWatcher:
    def __init__(
        self,
        win32: Win32,
        dib_to_png: Callable[[bytes], bytes],
        *,
        poll: float = 0.25,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        max_text: int = clipstore.MAX_TEXT_BYTES,
        max_image: int = clipstore.MAX_IMAGE_BYTES,
    ) -> None:
        self._win32 = win32
        self._dib_to_png = dib_to_png
        self._poll = poll
        self._clock = clock
        self._sleep = sleep
        self._max_text = max_text
        self._max_image = max_image
        # What is on the clipboard now was copied before capture began: not ours to keep.
        self._last = win32.sequence()

    def next_change(self, timeout: float) -> Clip | None:
        deadline = self._clock() + max(0.0, timeout)
        while True:
            current = self._win32.sequence()
            if current != self._last:
                outcome = self._read()
                if not isinstance(outcome, _Busy):
                    # Remember the state before reading: a change during the read is
                    # then noticed and read again on the next poll.
                    self._last = current
                    return limit_clip(outcome, self._max_text, self._max_image)
            remaining = deadline - self._clock()
            if remaining <= 0:
                return None
            self._sleep(min(self._poll, remaining))

    def _open(self) -> bool:
        for attempt in range(OPEN_ATTEMPTS):
            if self._win32.open():
                return True
            if attempt + 1 < OPEN_ATTEMPTS:
                self._sleep(OPEN_RETRY_SECONDS)
        return False

    def _concealed(self) -> bool:
        win32 = self._win32
        if win32.has(EXCLUDE_FORMAT):
            return True
        return win32.dword(HISTORY_FORMAT) == 0 or win32.dword(CLOUD_FORMAT) == 0

    def _read(self) -> Clip | _Busy:
        if not self._open():
            return _BUSY
        win32 = self._win32
        try:
            if self._concealed():
                return Clip(concealed=True)
            text = win32.text()
            if text and text.strip() and len(text.encode("utf-8", "replace")) <= self._max_text:
                return Clip(text=text)
            png = win32.data(PNG_FORMAT)
            if png is not None:
                png = trim_png(png)
                if len(png) <= self._max_image:
                    return Clip(image_png=png)
            dib = win32.data(CF_DIB)
            if dib is not None and len(dib) <= DIB_FACTOR * self._max_image:
                converted = self._dib_to_png(dib)
                if converted:
                    return Clip(image_png=converted)
            return Clip()
        finally:
            win32.close()

    def close(self) -> None:
        """Nothing to release: the clipboard is only polled."""


def dib_to_png(dib: bytes) -> bytes:
    """PNG bytes for a device-independent bitmap (needs Pillow), or nothing."""
    try:
        from PIL import Image  # type: ignore[import-not-found, unused-ignore]
    except ImportError:
        return b""
    try:
        with Image.open(io.BytesIO(dib)) as image:
            output = io.BytesIO()
            image.save(output, "PNG")
            return output.getvalue()
    except Exception:  # noqa: BLE001 - any decoder failure just means no image
        return b""


class Win32Clipboard:
    """The real Win32 calls. Argument and result types are declared so 64-bit handles
    are not truncated to 32 bits."""

    def __init__(self) -> None:
        windll = getattr(ctypes, "windll", None)
        if windll is None:
            raise Unavailable("Clipboard history for Windows only works on Windows.")
        from ctypes import wintypes

        user32: Any = windll.user32
        kernel32: Any = windll.kernel32
        user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
        user32.OpenClipboard.argtypes = [wintypes.HWND]
        user32.OpenClipboard.restype = wintypes.BOOL
        user32.CloseClipboard.restype = wintypes.BOOL
        user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
        user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
        user32.GetClipboardData.argtypes = [wintypes.UINT]
        user32.GetClipboardData.restype = wintypes.HANDLE
        user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
        user32.RegisterClipboardFormatW.restype = wintypes.UINT
        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalLock.restype = wintypes.LPVOID
        kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalUnlock.restype = wintypes.BOOL
        kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalSize.restype = ctypes.c_size_t
        self._user32, self._kernel32 = user32, kernel32
        self._registered: dict[str, int] = {}

    def _id(self, kind: int | str) -> int:
        if isinstance(kind, int):
            return kind
        if kind not in self._registered:
            self._registered[kind] = int(self._user32.RegisterClipboardFormatW(kind))
        return self._registered[kind]

    def sequence(self) -> int:
        return int(self._user32.GetClipboardSequenceNumber())

    def open(self) -> bool:
        return bool(self._user32.OpenClipboard(None))

    def close(self) -> None:
        self._user32.CloseClipboard()

    def has(self, kind: int | str) -> bool:
        return bool(self._user32.IsClipboardFormatAvailable(self._id(kind)))

    def _bytes(self, kind: int | str) -> bytes | None:
        handle = self._user32.GetClipboardData(self._id(kind))
        if not handle:
            return None
        size = int(self._kernel32.GlobalSize(handle))
        pointer = self._kernel32.GlobalLock(handle)
        if not pointer:
            return None
        try:
            return ctypes.string_at(pointer, size)
        finally:
            self._kernel32.GlobalUnlock(handle)

    def text(self) -> str | None:
        if not self.has(CF_UNICODETEXT):
            return None
        raw = self._bytes(CF_UNICODETEXT)
        if raw is None:
            return None
        # A block may be larger than the string in it: stop at the terminator.
        return raw.decode("utf-16-le", "replace").split("\x00", 1)[0]

    def data(self, kind: int | str) -> bytes | None:
        return self._bytes(kind) if self.has(kind) else None

    def dword(self, kind: str) -> int | None:
        if not self.has(kind):
            return None
        raw = self._bytes(kind)
        return int.from_bytes(raw[:4], "little") if raw and len(raw) >= 4 else None


def create() -> WindowsWatcher:
    return WindowsWatcher(Win32Clipboard(), dib_to_png)
