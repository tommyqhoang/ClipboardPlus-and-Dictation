"""Render the application icon without third-party imaging libraries.

Run from the repository root: python3 tools/make_icon.py
Writes lib/whisper-dictation.png (runtime window/launcher icon),
lib/menubar-*.png (macOS menu bar images), lib/tray-recording.png,
assets/icon-1024.png, assets/AppIcon.icns (macOS, needs sips/iconutil),
and assets/icon.ico (Windows).
"""

from __future__ import annotations

import math
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SIZE = 1024
TOP = (31, 150, 138)
BOTTOM = (9, 72, 76)
WHITE = (255, 255, 255)


def clamp(value: float) -> float:
    return 0.0 if value < 0 else 1.0 if value > 1 else value


def rounded_box(x: float, y: float, cx: float, cy: float, hw: float, hh: float, r: float) -> float:
    qx, qy = abs(x - cx) - hw + r, abs(y - cy) - hh + r
    outside = math.hypot(max(qx, 0.0), max(qy, 0.0))
    return outside + min(max(qx, qy), 0.0) - r


def segment(x: float, y: float, ax: float, ay: float, bx: float, by: float, r: float) -> float:
    px, py, dx, dy = x - ax, y - ay, bx - ax, by - ay
    t = clamp((px * dx + py * dy) / (dx * dx + dy * dy))
    return math.hypot(px - dx * t, py - dy * t) - r


def arc(
    x: float, y: float, cx: float, cy: float, radius: float, start: float, end: float, r: float
) -> float:
    """Round-capped arc; angles in degrees, clockwise from +x (screen space)."""
    angle = math.degrees(math.atan2(y - cy, x - cx))
    if start <= angle <= end or start <= angle + 360 <= end:
        return abs(math.hypot(x - cx, y - cy) - radius) - r
    ends = (math.radians(start), math.radians(end))
    return (
        min(math.hypot(x - cx - radius * math.cos(a), y - cy - radius * math.sin(a)) for a in ends)
        - r
    )


def glyph(x: float, y: float) -> tuple[float, float]:
    """Return (microphone distance, sound-wave distance) at a pixel."""
    mic = min(
        segment(x, y, 512, 330, 512, 520, 92),  # capsule
        arc(x, y, 512, 500, 168, 0, 180, 22),  # holder
        segment(x, y, 512, 668, 512, 752, 22),  # stem
        segment(x, y, 420, 752, 604, 752, 22),  # base
    )
    waves = min(
        arc(x, y, 512, 440, 250, -30, 30, 18),
        arc(x, y, 512, 440, 250, 150, 210, 18),
        arc(x, y, 512, 440, 320, -24, 24, 18),
        arc(x, y, 512, 440, 320, 156, 204, 18),
    )
    return mic, waves


def render(top: tuple[int, int, int] = TOP, bottom: tuple[int, int, int] = BOTTOM) -> bytes:
    rows = []
    margin, half, radius = 100, 412, 186
    for py in range(SIZE):
        y = py + 0.5
        t = clamp((y - margin) / (2 * half))
        base = tuple(top[i] + (bottom[i] - top[i]) * t for i in range(3))
        row = bytearray([0])
        for px in range(SIZE):
            x = px + 0.5
            tile = rounded_box(x, y, 512, 512, half, half, radius)
            # Soft drop shadow below the tile, per the macOS icon grid.
            shadow = rounded_box(x, y - 12, 512, 512, half, half, radius)
            shadow_alpha = 0.22 * clamp(1 - (shadow + 4) / 28) if tile > -1 else 0.0
            alpha = clamp(0.5 - tile)
            if alpha <= 0:
                row += bytes((0, 0, 0, round(255 * shadow_alpha)))
                continue
            color = list(base)
            # Subtle top highlight for depth.
            color = [c + (255 - c) * 0.10 * clamp(1 - (y - margin) / 360) for c in color]
            if 180 < y < 840 and 120 < x < 904:
                mic, waves = glyph(x, y)
                for distance, strength in ((waves, 0.55), (mic, 1.0)):
                    cover = clamp(0.5 - distance) * strength
                    if cover:
                        color = [c + (w - c) * cover for c, w in zip(color, WHITE)]
            out_alpha = alpha + shadow_alpha * (1 - alpha)
            row += bytes((*(round(c) for c in color), round(255 * out_alpha)))
        rows.append(bytes(row))
    return png(SIZE, SIZE, b"".join(rows))


def render_glyph(size: int, color: tuple[int, int, int]) -> bytes:
    """Microphone only, on transparency: the menu bar image (18 pt at 2x)."""
    scale = 600 / size  # source units per output pixel
    rows = []
    for py in range(size):
        row = bytearray([0])
        for px in range(size):
            mic, _ = glyph(512 + (px + 0.5 - size / 2) * scale, 506 + (py + 0.5 - size / 2) * scale)
            row += bytes((*color, round(255 * clamp(0.5 - mic / scale))))
        rows.append(bytes(row))
    return png(size, size, b"".join(rows))


def png(width: int, height: int, raw: bytes) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def resize(source: Path, size: int, target: Path) -> None:
    subprocess.run(
        ["sips", "-z", str(size), str(size), str(source), "--out", str(target)],
        check=True,
        capture_output=True,
    )


def main() -> int:
    if not shutil.which("sips") or not shutil.which("iconutil"):
        print("Run on macOS: sips and iconutil are required for resizing.", file=sys.stderr)
        return 1
    assets = ROOT / "assets"
    assets.mkdir(exist_ok=True)
    master = assets / "icon-1024.png"
    master.write_bytes(render())
    resize(master, 256, ROOT / "lib/whisper-dictation.png")
    # Windows/Linux tray while recording: the same tile in red.
    recording = assets / "icon-recording-1024.png"
    recording.write_bytes(render((236, 94, 84), (176, 40, 36)))
    resize(recording, 256, ROOT / "lib/tray-recording.png")
    (ROOT / "lib/menubar-icon.png").write_bytes(render_glyph(36, (0, 0, 0)))
    (ROOT / "lib/menubar-recording.png").write_bytes(render_glyph(36, (229, 72, 77)))
    with tempfile.TemporaryDirectory() as folder:
        iconset = Path(folder) / "AppIcon.iconset"
        iconset.mkdir()
        for size in (16, 32, 128, 256, 512):
            resize(master, size, iconset / f"icon_{size}x{size}.png")
            resize(master, size * 2, iconset / f"icon_{size}x{size}@2x.png")
        subprocess.run(
            ["iconutil", "-c", "icns", str(iconset), "-o", str(assets / "AppIcon.icns")],
            check=True,
        )
        images = []
        for size in (16, 24, 32, 48, 64, 128, 256):
            target = Path(folder) / f"ico-{size}.png"
            resize(master, size, target)
            images.append((size, target.read_bytes()))
    directory = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, blobs = b"", b""
    for size, data in images:
        entries += struct.pack(
            "<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(data), offset + len(blobs)
        )
        blobs += data
    (assets / "icon.ico").write_bytes(directory + entries + blobs)
    print("Icons written to lib/ and assets/.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
