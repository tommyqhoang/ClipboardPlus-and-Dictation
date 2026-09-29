"""Helpers shared by several test modules."""

from __future__ import annotations

import faulthandler
import logging
import os
import random
import struct
import sys
import zlib

# Inherited by subprocesses, even when a fixture strips DICTATION_* variables.
os.environ["DO_NOT_TRACK"] = "1"


def release_logs(folder: object) -> None:
    """Close and detach the app's log files that live under `folder`.

    Windows cannot delete a file another handle has open, and a module imported inside
    a test opens its rotating log in that test's temporary folder. Call this before the
    folder is removed (register it after the folder's cleanup so it runs first).
    """
    root = str(folder)
    for logger in list(logging.root.manager.loggerDict.values()):
        if not isinstance(logger, logging.Logger):
            continue
        for handler in list(logger.handlers):
            name = getattr(handler, "baseFilename", "")
            if name.startswith(root):
                logger.removeHandler(handler)
                handler.close()
                logger.addHandler(logging.NullHandler())


def share_one_tk_root() -> None:
    """Make every `tkinter.Tk()` in this test process the same root.

    Under macOS's Aqua Tk, a second Tk interpreter in one process never finishes
    `update()` once the app's window is built in it (its run loop always has more to
    do), which hung the macOS CI job. The app itself only ever makes one root per
    process, so this is a test-harness matter: tests share one root, and its
    `destroy()` empties it for the next test instead of ending the interpreter.
    """
    import tkinter

    real_tk = tkinter.Tk
    shared: list[tkinter.Tk] = []

    def tk(*args: object, **kwargs: object) -> tkinter.Tk:
        if shared:
            return shared[0]
        root = real_tk(*args, **kwargs)  # type: ignore[arg-type]
        base = set(root.tk.splitlist(root.tk.call("bind", "all")))

        def reset() -> None:
            for pending in root.tk.splitlist(root.tk.call("after", "info")):
                root.after_cancel(pending)
            for child in root.winfo_children():
                child.destroy()
            for sequence in root.tk.splitlist(root.tk.call("bind", "all")):
                if sequence not in base:  # What a test's window added; Tk's own stay.
                    root.tk.call("bind", "all", sequence, "")
            for sequence in root.bind():
                root.unbind(sequence)
            root.protocol("WM_DELETE_WINDOW", "")
            root.overrideredirect(False)  # The recording pill's window style.
            root.attributes("-topmost", False)
            root.attributes("-alpha", 1.0)
            root.withdraw()

        root.destroy = reset  # type: ignore[method-assign]
        shared.append(root)
        return root

    tkinter.Tk = tk  # type: ignore[misc, assignment]


# WWD_SHARED_TK=1 runs the same way elsewhere, to check it before macOS CI does.
if sys.platform == "darwin" or os.environ.get("WWD_SHARED_TK"):
    share_one_tk_root()

if os.environ.get("CI"):
    # A hung test (a dialog waiting for a click, a lock never released) would otherwise
    # sit silently until the job timeout. The suite takes about two minutes.
    faulthandler.dump_traceback_later(600, exit=True)


def make_png(
    width: int = 2,
    height: int = 2,
    color: tuple[int, int, int] = (255, 0, 0),
    noise: bool = False,
) -> bytes:
    """A valid PNG without needing Pillow. `noise` makes it large (it barely compresses)."""
    if noise:
        source = random.Random(width * 1000 + height)
        raw = b"".join(b"\x00" + source.randbytes(3 * width) for _ in range(height))
    else:
        raw = b"".join(b"\x00" + bytes(color) * width for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )
