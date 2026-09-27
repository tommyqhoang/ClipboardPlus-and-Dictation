"""Render the application icons: a Clipboard+ clipboard with an integrated microphone.

Run from the repository root: python3 tools/make_icon.py  (needs Pillow; any OS)
Writes lib/whisper-dictation.png (runtime window/launcher icon),
lib/menubar-*.png (macOS menu bar images), lib/tray-recording.png,
assets/icon-1024.png, assets/icon-recording-1024.png, assets/AppIcon.icns (macOS),
assets/icon.ico (Windows), and the matching website icons in site/assets/.

The artwork keeps the Clipboard+ orange clipboard, but gives dictation one clear,
integrated focal point.  A detached circular microphone read like an unread
notification at small sizes, so the microphone now lives inside the clipboard.
It turns red while recording.
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
    # A generous white label makes the microphone readable even at 16 px.  It is
    # deliberately inset on all sides: a single, unified clipboard mark instead
    # of a detached lower-corner badge that resembles an unread notification.
    _box(draw, (306, 384, 658, 752), 58, fill=PAPER)
    _microphone(draw, 482, 550, 102, badge)
    return image.resize((SIZE, SIZE), Image.Resampling.LANCZOS)


def render_glyph(size: int, color: tuple[int, int, int, int]) -> Any:
    """One-color clipboard-and-microphone silhouette for the macOS menu bar."""
    big = 1024 * SCALE
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    _box(draw, (214, 180, 810, 932), 94, outline=color, width=72 * SCALE)
    _box(draw, (332, 116, 692, 282), 46, fill=color)
    _microphone(draw, 512, 566, 126, color)
    return image.resize((size, size), Image.Resampling.LANCZOS)


def main() -> int:
    assets = ROOT / "assets"
    assets.mkdir(exist_ok=True)
    site_assets = ROOT / "site/assets"
    site_assets.mkdir(parents=True, exist_ok=True)
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
    # The desktop landing page, PWA manifest and native application must show the
    # same Clipboard+ clipboard-and-microphone mark, never an older microphone-only icon.
    for name, size in (
        ("icon-192.png", 192),
        ("icon-512.png", 512),
        ("apple-touch-icon.png", 180),
        ("favicon-32.png", 32),
    ):
        master.resize((size, size), Image.Resampling.LANCZOS).save(
            site_assets / name, optimize=True
        )
    print("Icons written to lib/, assets/ and site/assets/.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
