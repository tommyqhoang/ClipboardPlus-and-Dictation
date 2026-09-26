"""The floating recording pill: a live voice equalizer while recording, a flowing wave
while transcribing, then a short "Copied" confirmation that fades away.

Started by the dictation worker for one session (its token is the only argument). It
never records anything itself: the levels come from the tail of the audio file the
recorder is already writing, and its state from the session's state file. It never
takes the keyboard focus, so the paste target stays where the user left it. It adds to
the desktop notifications rather than replacing them.
"""

from __future__ import annotations

import math
import re
import shutil
import subprocess
import sys
import time
import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from tkinter import font
from typing import Any

import desktop
import dictation as d
import hotkeys

WIDTH, HEIGHT, DRAFT_HEIGHT = 480, 76, 104
MARGIN = 110  # Above the bottom edge of the screen (clear of docks and panels).
BARS = 19
FRAME_MS = 33  # About 30 frames a second.
SAMPLE_RATE = 16_000  # The recorder writes raw 16-bit mono PCM at this rate.
WINDOW_SAMPLES = 800  # 50 ms of audio per level reading.
HOLD = {"copied": 1.6, "empty": 2.2, "cancelled": 1.0, "error": 8.0}  # A click dismisses.
LIFETIME_SECONDS = 1200.0  # A stuck session never leaves the pill up for good.

BACKGROUND = "#13222d"
EDGE = "#2a3d4a"
TEXT = "#f3f6f8"
MUTED = "#9aa8b3"
ACCENT = "#2dd4bf"
ACCENT_DIM = "#0f766e"
RECORD = "#ef4444"
SUCCESS = "#34d399"
DANGER = "#f87171"
BUTTON = "#1f3240"
BUTTON_HOVER = "#2c4556"


def primary_monitor(listing: str) -> tuple[int, int, int, int] | None:
    """(x, y, width, height) of the primary monitor from `xrandr --listmonitors`."""
    shape = re.compile(r"(\d+)/\d+x(\d+)/\d+\+(-?\d+)\+(-?\d+)")
    first: tuple[int, int, int, int] | None = None
    for line in listing.splitlines():
        found = shape.search(line)
        if not found:
            continue
        width, height, x, y = (int(value) for value in found.groups())
        if "*" in line.split(found.group(0))[0]:
            return x, y, width, height
        first = first or (x, y, width, height)
    return first


def elapsed(seconds: float) -> str:
    whole = max(0, int(seconds))
    return f"{whole // 60}:{whole % 60:02d}"


@dataclass(frozen=True)
class View:
    """What the pill shows for one state of the session."""

    mode: str  # starting, recording, transcribing, copied, empty, cancelled, error, gone
    message: str = ""


def view(state: dict[str, Any], token: str) -> View:
    if state.get("token") != token:
        return View("gone")
    phase = str(state.get("phase", ""))
    message = str(state.get("message", ""))
    if phase in ("starting", "recording"):
        return View(phase, message)
    if phase in ("transcribing", "cancelling"):
        return View("transcribing" if phase == "transcribing" else "cancelled")
    if phase == "idle":
        result = str(state.get("result", ""))
        return View(result if result in HOLD else "cancelled", message)
    if phase in ("error", "interrupted"):
        return View("error", message or "Dictation stopped.")
    return View("gone")


class Overlay:
    def __init__(
        self,
        root: tk.Tk,
        paths: d.Paths,
        token: str,
        clock: Callable[[], float] = time.monotonic,
        launch: Callable[[list[str]], Any] | None = None,
    ) -> None:
        self.root, self.paths, self.token, self._clock = root, paths, token, clock
        self._launch = launch or self._spawn
        self.born = clock()
        self.shown_at = clock()
        self.mode = "starting"
        self.message = ""
        self.ended_at: float | None = None
        self.started_at: float | None = None  # When recording began (for the timer).
        self.recorded = 0.0
        self.history = [0.0] * (BARS // 2 + 1)  # Recent voice levels, newest first.
        self.heights = [0.0] * BARS
        self.draft = ""
        self._last_state = 0.0
        self.closed = False
        self._next: str | None = None  # The one scheduled frame.
        shortcut = hotkeys.Preferences(paths).shortcut()
        self.hint = f"{shortcut.label()} to stop"
        family = str(font.nametofont("TkDefaultFont").actual()["family"])
        self.fonts = {
            "title": (family, 12, "bold"),
            "small": (family, 10, "normal"),
            "draft": (family, 9, "italic"),
        }
        self._window()

    # -- the window --------------------------------------------------------
    def _window(self) -> None:
        root = self.root
        root.withdraw()
        root.overrideredirect(True)  # No title bar, no taskbar entry, never focused.
        root.configure(background=BACKGROUND)
        try:
            root.attributes("-topmost", True)
            root.attributes("-alpha", 0.0)
        except tk.TclError:
            pass
        self.canvas = tk.Canvas(
            root,
            width=WIDTH,
            height=HEIGHT,
            background=BACKGROUND,
            highlightthickness=2,  # A colored edge that says what is happening.
            highlightbackground=EDGE,
            borderwidth=0,
        )
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Button-1>", self._click)
        self.canvas.bind("<Motion>", self._hover)
        self.canvas.bind("<Leave>", lambda _: self._hover(None))
        self.hovered = ""
        self._place(HEIGHT)
        root.deiconify()

    def _place(self, height: int) -> None:
        self.height = height
        x0, y0, width, tall = self._screen()
        x = x0 + (width - WIDTH) // 2
        y = y0 + tall - height - MARGIN
        self.canvas.configure(height=height)
        self.root.geometry(f"{WIDTH}x{height}+{x}+{y}")

    def _screen(self) -> tuple[int, int, int, int]:
        """The primary monitor, so the pill never straddles two screens."""
        if desktop.platform_name() == "linux" and shutil.which("xrandr"):
            try:
                listing = subprocess.run(
                    ["xrandr", "--listmonitors"],
                    capture_output=True,
                    text=True,
                    timeout=2,
                    check=False,
                ).stdout
            except (OSError, subprocess.SubprocessError):
                listing = ""
            found = primary_monitor(listing)
            if found:
                return found
        return 0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight()

    # -- buttons -----------------------------------------------------------
    def _buttons(self) -> dict[str, tuple[int, int, int, int]]:
        if self.mode != "recording":
            return {}
        top = (HEIGHT - 32) // 2
        return {
            "stop": (WIDTH - 84, top, WIDTH - 52, top + 32),
            "cancel": (WIDTH - 44, top, WIDTH - 12, top + 32),
        }

    def _under(self, x: int, y: int) -> str:
        for name, (left, top, right, bottom) in self._buttons().items():
            if left <= x <= right and top <= y <= bottom:
                return name
        return ""

    def _hover(self, event: tk.Event[Any] | None) -> None:
        self.hovered = self._under(event.x, event.y) if event is not None else ""
        self.canvas.configure(cursor="hand2" if self.hovered else "")

    def _click(self, event: tk.Event[Any]) -> None:
        if self.ended_at is not None:
            self.close()  # Read it: a click dismisses the result.
            return
        name = self._under(event.x, event.y)
        if name:
            engine = str(Path(__file__).resolve().with_name("dictation.py"))
            self._launch([sys.executable, engine, *(["--cancel"] if name == "cancel" else [])])
            if name == "stop":
                self.recorded = self._since_start(self._clock())
            self.mode = "transcribing" if name == "stop" else "cancelled"
            if name == "cancel":
                self.ended_at = self._clock()

    @staticmethod
    def _spawn(command: list[str]) -> None:
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **desktop.process_options(detached=True),
        )

    # -- the session -------------------------------------------------------
    def _read_state(self) -> None:
        now = self._clock()
        if now - self._last_state < 0.1:
            return
        self._last_state = now
        current = view(d.read_json(self.paths.state), self.token)
        if current.mode == "gone":
            if self.ended_at is None:
                self.ended_at = now - HOLD["cancelled"]
            return
        if current.mode == "starting" and self.mode != "starting":
            return
        if current.mode == "transcribing" and self.mode == "recording":
            self.recorded = self._since_start(now)
        if current.mode == "recording" and self.started_at is None:
            self.started_at = now
        if current.mode in HOLD and self.ended_at is None:
            self.ended_at = now
        if current.mode == "error" and self.mode != "error":
            self._place(DRAFT_HEIGHT)  # Room for the whole message.
        self.mode, self.message = current.mode, current.message
        self._read_draft()

    def _since_start(self, now: float) -> float:
        return now - self.started_at if self.started_at is not None else 0.0

    def _read_draft(self) -> None:
        try:
            text = self.paths.preview.read_text(encoding="utf-8").strip()
        except OSError:
            text = ""
        draft = " ".join(text.split())[-64:]
        if draft != self.draft:
            self.draft = draft
            wanted = DRAFT_HEIGHT if draft and self.mode == "recording" else HEIGHT
            if wanted != self.height:
                self._place(wanted)

    def _listen(self) -> float:
        try:
            with self.paths.audio.open("rb") as audio:
                size = audio.seek(0, 2)
                audio.seek(max(0, size - WINDOW_SAMPLES * 2) // 2 * 2)
                return d.audio_level(audio.read(WINDOW_SAMPLES * 2))
        except OSError:
            return 0.0

    def _targets(self, now: float) -> list[float]:
        """Where each bar is heading this frame."""
        if self.mode == "recording":
            # The newest level enters in the middle and spreads out to both sides;
            # a slight breathing keeps the bars alive during pauses.
            self.history = [self._listen(), *self.history[:-1]]
            half = BARS // 2
            return [
                max(self.history[abs(i - half)], 0.06 + 0.04 * math.sin(now * 3 + i * 0.7))
                for i in range(BARS)
            ]
        if self.mode in ("transcribing", "starting"):
            return [0.25 + 0.2 * math.sin(now * 6 - i * 0.45) for i in range(BARS)]
        return [0.05] * BARS

    # -- drawing -----------------------------------------------------------
    def tick(self) -> None:
        if self.closed:
            return
        if self._next is not None:
            self.root.after_cancel(self._next)  # Never two animation loops at once.
        now = self._clock()
        self._read_state()
        for i, target in enumerate(self._targets(now)):
            # Rise quickly with the voice, fall back gently.
            speed = 0.6 if target > self.heights[i] else 0.18
            self.heights[i] += (target - self.heights[i]) * speed
        self._alpha(now)
        self.draw(now)
        if self._finished(now):
            self.close()
            return
        self._next = self.root.after(FRAME_MS, self.tick)

    def _finished(self, now: float) -> bool:
        if now - self.born > LIFETIME_SECONDS:
            return True
        if self.ended_at is None:
            return False
        return now - self.ended_at > HOLD.get(self.mode, 1.0) + 0.35

    def _alpha(self, now: float) -> None:
        appear = min(1.0, (now - self.shown_at) / 0.18)
        fade = 1.0
        if self.ended_at is not None:
            hold = HOLD.get(self.mode, 1.0)
            fade = max(0.0, 1.0 - max(0.0, now - self.ended_at - hold) / 0.35)
        try:
            self.root.attributes("-alpha", 0.96 * min(appear, fade))
        except tk.TclError:
            pass

    def draw(self, now: float) -> None:
        canvas = self.canvas
        canvas.delete("all")
        edge = {"recording": RECORD, "transcribing": ACCENT, "copied": SUCCESS, "error": DANGER}
        canvas.configure(highlightbackground=edge.get(self.mode, EDGE))
        self._badge(now)
        title, detail = self._words(now)
        canvas.create_text(
            64, HEIGHT // 2 - 11, text=title, anchor="w", fill=TEXT, font=self.fonts["title"]
        )
        if self.mode == "error":
            canvas.create_text(
                64, HEIGHT // 2 + 2, text=detail, anchor="nw", fill=MUTED, font=self.fonts["small"],
                width=WIDTH - 76,
            )  # fmt: skip
        else:
            canvas.create_text(
                64, HEIGHT // 2 + 12, text=detail, anchor="w", fill=MUTED, font=self.fonts["small"]
            )
        self._bars()
        for name, (left, top, right, bottom) in self._buttons().items():
            fill = BUTTON_HOVER if self.hovered == name else BUTTON
            canvas.create_oval(left, top, right, bottom, fill=fill, outline="")
            cx, cy = (left + right) // 2, (top + bottom) // 2
            if name == "stop":
                canvas.create_rectangle(cx - 5, cy - 5, cx + 5, cy + 5, fill=TEXT, outline="")
            else:
                for dx in (-5, 5):
                    canvas.create_line(cx - dx, cy - 5, cx + dx, cy + 5, fill=MUTED, width=2)
        if self.draft and self.mode == "recording":
            canvas.create_text(
                18,
                HEIGHT + 10,
                text="“…" + self.draft + "”",
                anchor="w",
                fill=MUTED,
                font=self.fonts["draft"],
                width=WIDTH - 36,
            )

    def _badge(self, now: float) -> None:
        """The round sign on the left: pulse, spinner, check or cross."""
        canvas, cx, cy = self.canvas, 34, HEIGHT // 2
        if self.mode == "recording":
            pulse = (now * 1.2) % 1.0
            ring = 7 + pulse * 11
            glow = _mix(RECORD, BACKGROUND, pulse)
            canvas.create_oval(cx - ring, cy - ring, cx + ring, cy + ring, outline=glow, width=2)
            canvas.create_oval(cx - 7, cy - 7, cx + 7, cy + 7, fill=RECORD, outline="")
        elif self.mode in ("transcribing", "starting"):
            canvas.create_oval(cx - 11, cy - 11, cx + 11, cy + 11, outline=EDGE, width=3)
            start = (now * 360) % 360
            canvas.create_arc(
                cx - 11, cy - 11, cx + 11, cy + 11,
                start=-start, extent=100, style="arc", outline=ACCENT, width=3,
            )  # fmt: skip
        elif self.mode == "copied":
            grow = min(1.0, (now - (self.ended_at or now)) / 0.2)
            size = 8 + 5 * grow
            canvas.create_oval(cx - size, cy - size, cx + size, cy + size, fill=SUCCESS, outline="")
            if grow >= 1.0:
                tick = [(cx - 6, cy), (cx - 2, cy + 4), (cx + 6, cy - 4)]
                canvas.create_line(tick, fill=BACKGROUND, width=3, capstyle="round")
        else:
            color = DANGER if self.mode == "error" else MUTED
            canvas.create_oval(cx - 12, cy - 12, cx + 12, cy + 12, outline=color, width=2)
            canvas.create_line(cx - 5, cy - 5, cx + 5, cy + 5, fill=color, width=2)
            canvas.create_line(cx - 5, cy + 5, cx + 5, cy - 5, fill=color, width=2)

    def _words(self, now: float) -> tuple[str, str]:
        dots = "." * (1 + int(now * 2.5) % 3)
        if self.mode == "recording":
            return "Listening", f"{elapsed(self._since_start(now))}  ·  {self.hint}"
        if self.mode == "starting":
            return "Starting" + dots, "Getting the microphone ready"
        if self.mode == "transcribing":
            said = (
                f"Recorded {elapsed(self.recorded)}"
                if self.recorded
                else "Turning speech into text"
            )
            return "Transcribing" + dots, said
        if self.mode == "copied":
            key = "Command+V" if desktop.platform_name() == "macos" else "Ctrl+V"
            return "Copied to the clipboard", f"Press {key} to paste"
        if self.mode == "empty":
            return "No speech detected", "Your previous transcript is kept"
        if self.mode == "cancelled":
            return "Recording cancelled", "Nothing was transcribed"
        message = self.message if len(self.message) <= 150 else self.message[:149] + "…"
        return "Dictation stopped", message

    def _bars(self) -> None:
        if self.mode not in ("recording", "transcribing", "starting"):
            return
        right = WIDTH - (96 if self.mode == "recording" else 20)
        left = right - BARS * 5
        middle = HEIGHT // 2
        color = ACCENT if self.mode == "recording" else ACCENT_DIM
        for i, height in enumerate(self.heights):
            reach = max(1.5, height * 24)
            x = left + i * 5
            fill = _mix(ACCENT_DIM, color, min(1.0, height * 1.6))
            self.canvas.create_line(
                x, middle - reach, x, middle + reach, fill=fill, width=3, capstyle="round"
            )

    def close(self) -> None:
        self.closed = True
        self.root.destroy()


def _mix(start: str, end: str, amount: float) -> str:
    """The color `amount` of the way from `start` to `end` (both #rrggbb)."""
    amount = min(1.0, max(0.0, amount))
    a = [int(start[i : i + 2], 16) for i in (1, 3, 5)]
    b = [int(end[i : i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * amount):02x}" for x, y in zip(a, b))


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: overlay.py TOKEN", file=sys.stderr)
        return 2
    try:
        root = tk.Tk(className="dictation-overlay")
    except tk.TclError:
        return 3  # No display: the notifications still tell the story.
    overlay = Overlay(root, d.Paths(), sys.argv[1])
    root.update_idletasks()
    overlay.tick()
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
