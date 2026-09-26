"""Helpers shared by several test modules."""

from __future__ import annotations

import faulthandler
import os
import random
import struct
import zlib

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
