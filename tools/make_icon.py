"""Render the application icons: the Clipboard+ "+" whose crossbar is a sound wave.

Run from the repository root: python3 tools/make_icon.py  (needs Pillow; any OS)
Writes lib/whisper-dictation.png (runtime window/launcher icon),
lib/menubar-*.png (macOS menu bar images), lib/tray-recording.png,
assets/icon-1024.png, assets/icon-recording-1024.png, assets/AppIcon.icns (macOS),
assets/icon.ico (Windows), and the matching website icons in site/assets/.

One mark for both features: the "+" of Clipboard+, its crossbar drawn as the bars
of a voice level meter. It stays a plain "+" at 16 px, where detail is lost, and
reads as "clipboard that listens" at app-icon size. The bars turn red while
recording.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw  # type: ignore[import-not-found, unused-ignore]

ROOT = Path(__file__).resolve().parents[1]
SIZE = 1024
SCALE = 4  # Drawn larger, then reduced, for smooth edges.
AMBER = (248, 177, 66, 255)  # The Clipboard+ logo's orange.
TILE_TOP = (46, 42, 37, 255)  # Warm near-black, lighter at the top.
TILE_BOTTOM = (22, 20, 18, 255)
INK = (17, 17, 17, 255)
RECORDING = (239, 68, 68, 255)
# The crossbar's bars from the stem outwards: half-heights as a share of the stem's
# half-height. Tallest by the stem, settling to the bar's own thickness, so the
# outline stays a bold "+" and only the thin gaps show the voice level.
WAVE = (0.30, 0.24, 0.19)
GLYPH_WAVE = (0.30, 0.20)  # Fewer bars for the 18 pt menu bar: gaps would blur.


def _pill(draw: Any, cx: float, cy: float, width: float, half: float, fill: Any) -> None:
    """A vertical bar with fully rounded ends, centred on (cx, cy)."""
    draw.rounded_rectangle(
        [
            (cx - width / 2) * SCALE,
            (cy - half) * SCALE,
            (cx + width / 2) * SCALE,
            (cy + half) * SCALE,
        ],
        radius=width / 2 * SCALE,
        fill=fill,
    )


def _mark(
    draw: Any,
    cx: float,
    cy: float,
    half: float,
    stem: tuple[int, int, int, int],
    wave: tuple[int, int, int, int],
    levels: tuple[float, ...] = WAVE,
) -> None:
    """The "+": a stem `2 * half` tall, and a crossbar of level bars as wide."""
    stem_width = half * 0.38
    _pill(draw, cx, cy, stem_width, half, stem)
    reach = half - stem_width / 2  # Each arm, from the stem's edge to the tip.
    gap = half * 0.06
    width = (reach - gap * len(levels)) / len(levels)
    for index, level in enumerate(levels):
        offset = stem_width / 2 + gap + index * (width + gap) + width / 2
        for side in (-1, 1):
            _pill(draw, cx + side * offset, cy, width, half * level, wave)


def _tile(size: int) -> Any:
    """The app tile: a rounded square on the macOS icon grid, softly lit from above."""
    big = size * SCALE
    gradient = Image.new("RGBA", (1, big))
    for y in range(big):
        t = y / (big - 1)
        gradient.putpixel(
            (0, y), tuple(round(a + (b - a) * t) for a, b in zip(TILE_TOP, TILE_BOTTOM))
        )
    gradient = gradient.resize((big, big))
    mask = Image.new("L", (big, big), 0)
    inset = 100 / 1024 * big  # Apple's grid: an 824 px tile in a 1024 px canvas.
    ImageDraw.Draw(mask).rounded_rectangle(
        [inset, inset, big - inset, big - inset], radius=185 / 1024 * big, fill=255
    )
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    image.paste(gradient, mask=mask)
    return image


def render(recording: bool = False) -> Any:
    """The full-color icon on transparency, SIZE x SIZE."""
    image = _tile(SIZE)
    draw = ImageDraw.Draw(image)
    _mark(draw, SIZE / 2, SIZE / 2, 285, AMBER, RECORDING if recording else AMBER)
    return image.resize((SIZE, SIZE), Image.Resampling.LANCZOS)


def render_glyph(size: int, color: tuple[int, int, int, int]) -> Any:
    """The one-color mark for the macOS menu bar (a template image: macOS tints it)."""
    big = 1024 * SCALE
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    _mark(draw, 512, 512, 430, color, color, GLYPH_WAVE)
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
    # Windows/Linux tray while recording: the same tile, its level bars in red.
    recording = render(recording=True)
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
    # The desktop landing page, PWA manifest and native application show one mark.
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
