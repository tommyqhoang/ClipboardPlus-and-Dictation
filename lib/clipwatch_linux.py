"""Linux clipboard watching.

Two ways, chosen when the service starts:

* `wl-paste --watch` on Wayland compositors that allow background clipboard access
  (wlroots, KDE). GNOME's compositor refuses it.
* X11 selection events over X11 or XWayland. On GNOME Wayland this still sees every
  copy, even with no window focused, and can read the content.

Content marked by a password manager (`x-kde-passwordManagerHint`) is never read.
"""

from __future__ import annotations

import os
import select
import shutil
import subprocess
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import clipstore
from clipwatch import Clip, Unavailable, limit_clip

# Most faithful first. text/plain without a charset is taken as UTF-8.
TEXT_TARGETS = ("UTF8_STRING", "text/plain;charset=utf-8", "text/plain", "STRING")
IMAGE_TARGET = "image/png"
CONCEALED_TARGET = "x-kde-passwordManagerHint"
_READ_TIMEOUT = 2.0  # Per step of a selection transfer.
_TRANSFER_TIMEOUT = 10.0  # A whole transfer, however large.


@dataclass(frozen=True)
class Choice:
    concealed: bool
    text_target: str
    image_target: str


def choose(targets: Iterable[str]) -> Choice:
    """Pick what to read from the formats the copying application offers.

    Text wins over an image offered alongside it, so copying spreadsheet cells keeps
    the cells. An image is used when there is no text (screenshots, "copy image").
    """
    offered = set(targets)
    text = next((name for name in TEXT_TARGETS if name in offered), "")
    image = IMAGE_TARGET if IMAGE_TARGET in offered else ""
    return Choice(CONCEALED_TARGET in offered, text, image)


def decode_text(target: str, data: bytes) -> str:
    data = data.rstrip(b"\x00")
    if target == "STRING":  # The legacy X11 text type is Latin-1.
        return data.decode("latin-1")
    return data.decode("utf-8", "replace")


class Source(Protocol):
    """Reports clipboard changes and reads the current content."""

    def wait(self, timeout: float) -> bool: ...

    def read(self) -> Clip: ...

    def close(self) -> None: ...


class LinuxWatcher:
    """Turns a change source into the shared `Watcher` interface."""

    def __init__(self, source: Source) -> None:
        self._source = source

    def next_change(self, timeout: float) -> Clip | None:
        if not self._source.wait(timeout):
            return None
        return limit_clip(self._source.read())

    def close(self) -> None:
        self._source.close()


class X11Source:
    """CLIPBOARD changes from XFIXES selection-owner events, content by selection transfer."""

    _BURST = 0.05  # Applications often announce one copy several times.

    def __init__(
        self,
        display_name: str | None = None,
        max_text: int = clipstore.MAX_TEXT_BYTES,
        max_image: int = clipstore.MAX_IMAGE_BYTES,
    ) -> None:
        try:
            from Xlib import (  # type: ignore[import-not-found, unused-ignore]
                X,
                Xatom,
                display,
                error,
            )
            from Xlib.ext import xfixes  # type: ignore[import-not-found, unused-ignore]
        except ImportError as exc:
            raise Unavailable("Clipboard history needs the python-xlib package.") from exc
        name = display_name or os.environ.get("DISPLAY")
        if not name:
            raise Unavailable("Clipboard history needs an X11 or XWayland display.")
        try:
            self._display = display.Display(name)
            if not self._display.has_extension("XFIXES"):
                raise Unavailable("This X server has no XFIXES extension.")
            self._display.xfixes_query_version()
        except (OSError, error.DisplayError) as exc:
            raise Unavailable(f"Cannot connect to the display {name}.") from exc
        self._x, self._atom_type, self._error = X, Xatom, error
        self._max = {"text": max_text, "image": max_image}
        d = self._display
        root = d.screen().root
        self._clipboard = d.intern_atom("CLIPBOARD")
        self._property = d.intern_atom("CLIPWATCH_DATA")
        self._targets = d.intern_atom("TARGETS")
        self._incr = d.intern_atom("INCR")
        self._notify = d.extension_event.SetSelectionOwnerNotify[0]
        self._window = root.create_window(0, 0, 1, 1, 0, d.screen().root_depth)
        self._window.change_attributes(event_mask=X.PropertyChangeMask)
        d.xfixes_select_selection_input(
            root, self._clipboard, xfixes.XFixesSetSelectionOwnerNotifyMask
        )
        d.flush()
        self._changed = False

    # -- changes -----------------------------------------------------------
    def _absorb(self, event: Any) -> None:
        """Note an ownership change; everything else is handled by whoever waits for it."""
        if event.type == self._notify and getattr(event, "sub_code", 0) == 0:
            owner = getattr(event.owner, "id", event.owner)
            if owner:  # No owner means the clipboard was emptied, not copied to.
                self._changed = True

    def wait(self, timeout: float) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            self._drain()
            if self._changed:
                self._changed = False
                self._settle()
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            select.select([self._display.fileno()], [], [], min(remaining, 0.25))

    def _drain(self) -> None:
        while self._display.pending_events():
            self._absorb(self._display.next_event())

    def _settle(self) -> None:
        """Let a burst of notifications for one copy finish so it is read once."""
        end = time.monotonic() + self._BURST
        while time.monotonic() < end:
            select.select([self._display.fileno()], [], [], self._BURST)
            self._drain()
            if not self._changed:
                break
            self._changed = False

    # -- content -----------------------------------------------------------
    def read(self) -> Clip:
        targets = self._fetch_targets()
        choice = choose(targets)
        if choice.concealed:
            return Clip(concealed=True)
        if choice.text_target:
            data = self._fetch(choice.text_target, self._max["text"])
            if data is not None:
                text = decode_text(choice.text_target, data)
                if text.strip():
                    return Clip(text=text)
        if choice.image_target:
            data = self._fetch(choice.image_target, self._max["image"])
            if data is not None:
                return Clip(image_png=data)
        return Clip()

    def _fetch_targets(self) -> list[str]:
        raw = self._convert(self._targets, 4 * 1000)
        if raw is None:
            return []
        atoms = [int.from_bytes(raw[i : i + 4], "little") for i in range(0, len(raw) - 3, 4)]
        names = []
        for atom in atoms:
            try:
                names.append(self._display.get_atom_name(atom))
            except self._error.XError:
                continue
        return names

    def _fetch(self, target: str, limit: int) -> bytes | None:
        return self._convert(self._display.intern_atom(target), limit)

    def _convert(self, target: int, limit: int) -> bytes | None:
        """Ask the owner for one format. None when refused, too big, or too slow."""
        try:
            return self._transfer(target, limit)
        except (self._error.XError, OSError):
            return None  # The owner went away mid-transfer.

    def _transfer(self, target: int, limit: int) -> bytes | None:
        d, X = self._display, self._x
        self._window.delete_property(self._property)
        self._window.convert_selection(self._clipboard, target, self._property, X.CurrentTime)
        d.flush()
        reply = self._wait_for(lambda e: e.type == X.SelectionNotify, _READ_TIMEOUT)
        if reply is None or reply.property == X.NONE:
            return None
        found = self._window.get_full_property(self._property, X.AnyPropertyType)
        if found is None:
            return None
        if found.property_type != self._incr:
            self._window.delete_property(self._property)
            data = self._as_bytes(found)
            return data if len(data) <= limit else None
        return self._receive_incremental(limit)

    def _receive_incremental(self, limit: int) -> bytes | None:
        """Large content arrives in chunks: delete the property to request each one."""
        d, X = self._display, self._x
        chunks: list[bytes] = []
        total = 0
        end = time.monotonic() + _TRANSFER_TIMEOUT
        self._window.delete_property(self._property)  # Asks for the first chunk.
        d.flush()
        while time.monotonic() < end:
            event = self._wait_for(
                lambda e: (
                    e.type == X.PropertyNotify
                    and e.atom == self._property
                    and e.state == X.PropertyNewValue
                ),
                _READ_TIMEOUT,
            )
            if event is None:
                return None
            found = self._window.get_full_property(self._property, X.AnyPropertyType)
            chunk = self._as_bytes(found) if found is not None else b""
            self._window.delete_property(self._property)
            d.flush()
            if not chunk:
                return b"".join(chunks)
            total += len(chunk)
            if total > limit:
                return None  # Stop asking: the rest is never transferred.
            chunks.append(chunk)
        return None

    @staticmethod
    def _as_bytes(found: Any) -> bytes:
        value = found.value
        if found.format == 8:
            return bytes(value)
        if found.format == 32:
            return b"".join(int(item).to_bytes(4, "little") for item in value)
        return b"".join(int(item).to_bytes(2, "little") for item in value)

    def _wait_for(self, matches: Callable[[Any], bool], timeout: float) -> Any | None:
        """The next matching event; ownership changes seen meanwhile are not lost."""
        end = time.monotonic() + timeout
        while True:
            while self._display.pending_events():
                event = self._display.next_event()
                if matches(event):
                    return event
                self._absorb(event)
            remaining = end - time.monotonic()
            if remaining <= 0:
                return None
            select.select([self._display.fileno()], [], [], remaining)

    def close(self) -> None:
        try:
            self._display.close()
        except (OSError, self._error.XError):
            pass


class WlPasteSource:
    """`wl-paste --watch` for changes and `wl-paste` for content (wlroots, KDE)."""

    def __init__(
        self,
        watcher: Any | None = None,
        reader: Callable[[Sequence[str], int], bytes | None] | None = None,
        max_text: int = clipstore.MAX_TEXT_BYTES,
        max_image: int = clipstore.MAX_IMAGE_BYTES,
    ) -> None:
        # Each change runs the command once with the content on stdin: drain it, then
        # print one line, which is the signal read by wait().
        self._process = watcher or subprocess.Popen(
            ["wl-paste", "--watch", "sh", "-c", "cat >/dev/null; echo changed"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        self._reader = reader or _read_command
        self._max = {"text": max_text, "image": max_image}

    def wait(self, timeout: float) -> bool:
        if self._process.poll() is not None:
            raise Unavailable("The clipboard watcher stopped unexpectedly.")
        stdout = self._process.stdout
        if stdout is None:
            raise Unavailable("The clipboard watcher has no output.")
        ready, _, _ = select.select([stdout], [], [], max(0.0, timeout))
        if not ready:
            return False
        stdout.readline()
        # Collapse a burst into one change.
        while select.select([stdout], [], [], 0.05)[0]:
            if not stdout.readline():
                break
        return True

    def read(self) -> Clip:
        listing = self._reader(["wl-paste", "--list-types"], 64_000)
        choice = choose((listing or b"").decode("utf-8", "replace").split())
        if choice.concealed:
            return Clip(concealed=True)
        if choice.text_target:
            data = self._reader(
                ["wl-paste", "--no-newline", "--type", choice.text_target], self._max["text"]
            )
            if data is not None and (text := decode_text(choice.text_target, data)).strip():
                return Clip(text=text)
        if choice.image_target:
            data = self._reader(
                ["wl-paste", "--no-newline", "--type", choice.image_target], self._max["image"]
            )
            if data is not None:
                return Clip(image_png=data)
        return Clip()

    def close(self) -> None:
        self._process.terminate()
        try:
            self._process.wait(timeout=2)
        except (subprocess.TimeoutExpired, OSError):
            pass
        stdout = self._process.stdout
        if stdout is not None:
            stdout.close()


def _read_command(command: Sequence[str], max_bytes: int) -> bytes | None:
    """Output of a command, or None if it fails, is too slow, or exceeds `max_bytes`."""
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError:
        return None
    try:
        assert process.stdout is not None
        data = process.stdout.read(max_bytes + 1)
        if len(data) > max_bytes:
            return None
        return data if process.wait(timeout=_READ_TIMEOUT) == 0 else None
    except subprocess.TimeoutExpired:
        return None
    finally:
        process.kill()
        process.wait()
        if process.stdout is not None:
            process.stdout.close()


def wl_paste_can_watch(
    start: Callable[[Sequence[str]], Any] | None = None, settle: float = 0.6
) -> bool:
    """Whether the compositor allows `wl-paste --watch` (GNOME's does not: it exits at once)."""
    launch = start or (
        lambda command: subprocess.Popen(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    )
    try:
        process = launch(["wl-paste", "--watch", "true"])
    except OSError:
        return False
    deadline = time.monotonic() + settle
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        time.sleep(0.02)
    supported = process.poll() is None
    process.terminate()
    try:
        process.wait(timeout=2)
    except (subprocess.TimeoutExpired, OSError):
        pass
    return supported


def create(
    environment: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> LinuxWatcher:
    env = os.environ if environment is None else environment
    if env.get("WAYLAND_DISPLAY") and which("wl-paste") and wl_paste_can_watch():
        return LinuxWatcher(WlPasteSource())
    if env.get("DISPLAY"):
        try:
            return LinuxWatcher(X11Source(env["DISPLAY"]))
        except (OSError, Unavailable) as exc:
            raise Unavailable(str(exc) or "Cannot watch the clipboard on this display.") from exc
    raise Unavailable(
        "This desktop does not allow watching the clipboard. Clipboard history needs "
        "X11, XWayland, or a Wayland compositor with clipboard access."
    )
