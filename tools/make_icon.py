"""Render the application icons: the Clipboard+ clipboard with a microphone badge.

Run from the repository root: python3 tools/make_icon.py  (needs Pillow; any OS)
Writes lib/whisper-dictation.png (runtime window/launcher icon),
lib/menubar-*.png (macOS menu bar images), lib/tray-recording.png,
assets/icon-1024.png, assets/icon-recording-1024.png, assets/AppIcon.icns (macOS)
and assets/icon.ico (Windows).

The artwork matches the Clipboard+ logo (an orange clipboard with a cloud) so the
desktop app, the extension and the website look like one product; the badge marks
the dictation that only the desktop app has. It turns red while recording.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw  # type: ignore[import-not-found, unused-ignore]

ROOT = Path(__file__).resolve().parents[1]
SIZE = 1024
SCALE = 4  # Drawn larger, then reduced, for smooth edges.
ORANGE = (248, 177, 66, 255)  # The Clipboard+ logo's orange.
INK = (17, 17, 17, 255)
PAPER = (255, 255, 255, 255)
RECORDING = (229, 72, 77, 255)


def _box(draw: Any, box: tuple[float, float, float, float], radius: float, **style: Any) -> None:
    draw.rounded_rectangle([v * SCALE for v in box], radius=radius * SCALE, **style)


def _circle(draw: Any, cx: float, cy: float, r: float, **style: Any) -> None:
    draw.ellipse([(cx - r) * SCALE, (cy - r) * SCALE, (cx + r) * SCALE, (cy + r) * SCALE], **style)


def _line(
    draw: Any, a: tuple[float, float], b: tuple[float, float], width: float, fill: Any
) -> None:
    draw.line(
        [a[0] * SCALE, a[1] * SCALE, b[0] * SCALE, b[1] * SCALE],
        fill=fill,
        width=int(width * SCALE),
    )
    for x, y in (a, b):  # Round caps.
        _circle(draw, x, y, width / 2, fill=fill)


def _microphone(draw: Any, cx: float, cy: float, unit: float, fill: Any) -> None:
    """A microphone about 3 units wide, centered on (cx, cy)."""
    _box(
        draw,
        (cx - 0.55 * unit, cy - 1.45 * unit, cx + 0.55 * unit, cy + 0.25 * unit),
        0.55 * unit,
        fill=fill,
    )
    stroke = 0.26 * unit
    draw.arc(
        [
            (cx - 1.05 * unit) * SCALE,
            (cy - 0.85 * unit) * SCALE,
            (cx + 1.05 * unit) * SCALE,
            (cy + 0.95 * unit) * SCALE,
        ],
        start=0,
        end=180,
        fill=fill,
        width=int(stroke * SCALE),
    )
    _line(draw, (cx, cy + 0.95 * unit), (cx, cy + 1.45 * unit), stroke, fill)
    _line(
        draw,
        (cx - 0.55 * unit, cy + 1.45 * unit),
        (cx + 0.55 * unit, cy + 1.45 * unit),
        stroke,
        fill,
    )


def render(badge: tuple[int, int, int, int] = INK) -> Any:
    """The full-color icon on transparency, SIZE x SIZE."""
    big = SIZE * SCALE
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    stroke = 42
    # The board: black outline, orange face.
    _box(draw, (214, 196, 750, 900), 86, fill=INK)
    _box(draw, (214 + stroke, 196 + stroke, 750 - stroke, 900 - stroke), 86 - stroke, fill=ORANGE)
    # The clip: a ring on top of a bar, white inside.
    _circle(draw, 482, 150, 74, fill=INK)
    _circle(draw, 482, 150, 34, fill=PAPER)
    _circle(draw, 482, 150, 15, fill=INK)
    _box(draw, (334, 158, 630, 290), 34, fill=INK)
    _box(draw, (334 + 34, 158 + 34, 630 - 34, 290 - 34), 10, fill=PAPER)
    # The cloud, drawn as an outline on the orange.
    for r, fill in ((0, INK), (-30, ORANGE)):
        _circle(draw, 408, 486, 72 + r, fill=fill)
        _circle(draw, 494, 438, 94 + r, fill=fill)
        _circle(draw, 574, 492, 66 + r, fill=fill)
        _box(draw, (340 - r, 486 - r, 640 + r, 574 + r), 44 + r, fill=fill)
    # Two lines of text.
    _line(draw, (330, 660), (630, 660), 40, INK)
    _line(draw, (330, 748), (530, 748), 40, INK)
    # The dictation badge, cut out of the board by a white ring.
    _circle(draw, 766, 792, 214, fill=PAPER)
    _circle(draw, 766, 792, 184, fill=badge)
    _microphone(draw, 766, 800, 72, PAPER)
    return image.resize((SIZE, SIZE), Image.Resampling.LANCZOS)


def render_glyph(size: int, color: tuple[int, int, int, int]) -> Any:
    """One-color silhouette for the macOS menu bar (a template image at 2x)."""
    big = 1024 * SCALE
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    clear = (0, 0, 0, 0)
    _box(draw, (190, 160, 770, 930), 96, outline=color, width=76 * SCALE)
    _box(draw, (330, 110, 630, 280), 44, fill=color)
    _circle(draw, 770, 800, 240, fill=clear)
    _circle(draw, 770, 800, 188, fill=color)
    _microphone(draw, 770, 808, 74, clear)
    return image.resize((size, size), Image.Resampling.LANCZOS)


def main() -> int:
    assets = ROOT / "assets"
    assets.mkdir(exist_ok=True)
    master = render()
    master.save(assets / "icon-1024.png", optimize=True)
    master.resize((256, 256), Image.Resampling.LANCZOS).save(
        ROOT / "lib/whisper-dictation.png", optimize=True
    )
    # Windows/Linux tray while recording: the same logo, badge in red.
    recording = render(RECORDING)
    recording.save(assets / "icon-recording-1024.png", optimize=True)
    recording.resize((256, 256), Image.Resampling.LANCZOS).save(
        ROOT / "lib/tray-recording.png", optimize=True
    )
    render_glyph(36, INK).save(ROOT / "lib/menubar-icon.png", optimize=True)
    render_glyph(36, RECORDING).save(ROOT / "lib/menubar-recording.png", optimize=True)
    master.save(
        assets / "AppIcon.icns",
        sizes=[(16, 16), (32, 32), (64, 64), (128, 128), (256, 256), (512, 512), (1024, 1024)],
    )
    master.save(
        assets / "icon.ico",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    print("Icons written to lib/ and assets/.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
