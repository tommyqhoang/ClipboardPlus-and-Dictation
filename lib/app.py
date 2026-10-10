"""Clipboard+ window and first-run walkthrough."""

from __future__ import annotations

import concurrent.futures
import dataclasses
import functools
import json
import logging
import os
import shlex
import subprocess
import sys
import threading
import time
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Literal

import app_settings
import app_styles
import clipstore
import clipui
import cues
import desktop
import dictation as d
import hotkeys
import permissions
import rewriteui
import shortcut_panel
import telemetry
import updates
import workflow
from app_service import PROVIDERS, MicrophoneTest, Remote, Service
from app_styles import (  # noqa: F401 - the palette, kept importable from here
    ACCENT,
    ACCENT_ACTIVE,
    ACCENT_SOFT,
    BACKGROUND,
    BORDER,
    DANGER,
    DANGER_ACTIVE,
    HOVER,
    IDLE,
    MUTED,
    SURFACE,
    TEXT,
    WARNING,
)

try:
    import logsetup

    log = logsetup.get_logger("app")
except ImportError:
    log = logging.getLogger(__name__)

ICON = Path(__file__).with_name("whisper-dictation.png")
MODES = (
    ("dictation", "Dictation", "Press a shortcut, speak, and paste anywhere."),
    (
        "clipboard",
        "Clipboard history",
        "Save copies on this device so you can search them and copy them back.",
    ),
    (
        "both",
        "Both",
        "Dictation plus a searchable history of your copies on this device.",
    ),
)

# How long the scrollbar stays once shown, so a page at the window's height cannot
# make it appear and disappear forever.
SCROLLBAR_SETTLE = 0.4
LOOKUP_MS = 15  # How often finished background lookups are collected while any run.
PAD = 20  # The page's side padding.
# Past this, a maximized window centers a readable column instead of stretching
# buttons and fields across the whole screen.
CONTENT_MAX = 960
# Pages reached from the header tabs once setup is done; they need no big title.
TAB_PAGES = ("home", "clipboard", "settings")


class App:
    def __init__(self, root: tk.Tk, service: Service, page: str = "") -> None:
        self.init_state(root, service)
        self.size_window()
        self.build_chrome()
        self.build_scroller()
        self.bind_events()
        self.init_variables(page)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        # The tray/menu bar app owns everyday use; this window is for setup.
        self.tray = True
        self.show_first_page(page)
        self.timer = self.root.after(150, self.poll)

    def init_state(self, root: tk.Tk, service: Service) -> None:
        self.root, self.service = root, service
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        # Quiet lookups (microphones) that must not lock the page like `submit` does.
        self.helper = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self.lookups: list[tuple[concurrent.futures.Future[Any], Callable[[Any], None]]] = []
        self.lookup_timer: str | None = None
        self.clipboard_open_future: concurrent.futures.Future[Any] | None = None
        self.clipboard_queries: set[concurrent.futures.Future[Any]] = set()
        self.pending: concurrent.futures.Future[Any] | None = None
        self.done: Callable[[Any], None] = lambda value: None
        self.page = ""
        self.closing = False
        self.last_text = ""
        self.transcript_seen: tuple[Any, ...] = ()
        self.download = (0, 0)
        self.download_pause = threading.Event()
        self.pause_download: ttk.Button | None = None
        self.buttons: list[ttk.Button] = []
        self.root.title(hotkeys.APP_NAME)
        self.rewrap_timer: str | None = None
        self.bottom_timer: str | None = None
        self.gutter = 0  # Extra side space around the centered column on wide windows.
        self.toolbar_padding = (PAD, 12, PAD, 8)

    def size_window(self) -> None:
        """Fit the window to this screen, or to the size the user left it at."""
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        usable_width, usable_height = screen_width - 80, screen_height - 100
        if sys.platform == "darwin":
            # winfo_screenheight() is the full panel; a fixed guess at the Dock and menu
            # bar's height is wrong on a taller Dock and can push the bottom bar (Save,
            # Cancel…) under it. AppKit's visibleFrame already excludes both exactly.
            try:
                from AppKit import NSScreen

                visible = NSScreen.mainScreen().visibleFrame()
                usable_width = int(visible.size.width) - 40
                usable_height = int(visible.size.height) - 40
            except Exception as exc:  # noqa: BLE001 - falls back to the guess below.
                log.info("could not ask AppKit for the visible screen: %s", exc)
        width = min(780, max(360, usable_width))
        height = min(640, max(360, usable_height))
        saved = self.saved_size()
        if saved is not None:
            # The size the user left it at, as long as it still fits this screen.
            width = max(360, min(saved[0], screen_width - 40))
            height = max(360, min(saved[1], screen_height - 60))
        self.wraplength = max(260, width - 2 * PAD - 30)
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(min(480, width), min(400, height))

    def build_chrome(self) -> None:
        """The fixed parts: icon, styles, header, bottom bar and the toast."""
        self.root.configure(background=BACKGROUND)
        self.icon = self.load_icon()
        if self.icon is not None:
            self.root.iconphoto(True, self.icon)
        self.status = tk.StringVar(value="")
        self.toast = tk.StringVar(value="")
        self.toast_after: str | None = None
        self.styles()
        self.header()
        self.bottom_bar()
        self.toast_label = ttk.Label(self.root, textvariable=self.toast, style="Toast.TLabel")

    def build_scroller(self) -> None:
        """The toolbar and the scrolling page: canvas, scrollbar and content frame."""
        # Fixed controls above the scrolling page (the clipboard search stays in view).
        self.toolbar = ttk.Frame(self.root)
        container = self.container = ttk.Frame(self.root)
        container.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(
            container,
            background=BACKGROUND,
            borderwidth=0,
            highlightthickness=0,
            yscrollincrement=20,
        )
        self.scrollbar = ttk.Scrollbar(container, orient="vertical", command=self.canvas.yview)
        self.touchpad_pixels = 0
        self.scrollbar_shown_at = 0.0  # time.monotonic() when it last appeared.
        self.scrollbar_recheck: str | None = None
        self.canvas.configure(yscrollcommand=self.scroll)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.frame = ttk.Frame(self.canvas, padding=(PAD, 14, PAD, 20))
        self.frame_window = self.canvas.create_window((0, 0), window=self.frame, anchor="nw")
        self.frame.bind("<Configure>", self.resize_scroll_region)
        self.canvas.bind("<Configure>", self.resize_content)

    def bind_events(self) -> None:
        """Mouse wheel, touchpad and keyboard shortcuts for the whole window."""
        # Windows and macOS send <MouseWheel>; X11 sends buttons 4 and 5.
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.root.bind_all(sequence, self.wheel, add="+")
        # Tk 8.7/9 delivers precision trackpad gestures separately from MouseWheel.
        # Older Tk versions reject the event name; keep their MouseWheel path.
        wheel_sequences: tuple[str, ...] = ("MouseWheel", "Button-4", "Button-5")
        try:
            self.root.bind_all("<TouchpadScroll>", self.touchpad, add="+")
            wheel_sequences += ("TouchpadScroll",)
        except tk.TclError as exc:  # Older Tk versions have no touchpad event.
            log.debug("no TouchpadScroll event in this Tk: %s", exc)
        # Tk's dropdowns, spinboxes and sliders change value under the wheel, so
        # scrolling the page past one silently changed a setting. The wheel only scrolls.
        for widget_class in ("TCombobox", "TSpinbox", "Spinbox", "TScale", "Scale"):
            for sequence in wheel_sequences:
                for modifier in ("", "Shift-"):
                    self.root.unbind_class(widget_class, f"<{modifier}{sequence}>")
        command = "Command" if sys.platform == "darwin" else "Control"
        for key, action in (("f", self.find), ("comma", self.go_settings), ("w", self.close)):
            self.root.bind_all(
                f"<{command}-{key}>", functools.partial(self.on_key, action), add="+"
            )
        for key, amount, what in (
            ("Prior", -1, "pages"),
            ("Next", 1, "pages"),
            ("Home", -1, "end"),
            ("End", 1, "end"),
        ):
            self.root.bind_all(
                f"<{key}>", functools.partial(self.scroll_key, amount, what), add="+"
            )

    def init_variables(self, page: str) -> None:
        """The form variables and page state that every screen shares."""
        self.default_button: ttk.Button | None = None
        # The dictation fields as last saved, to catch leaving Settings with edits.
        self.settings_snapshot: tuple[str, ...] | None = None
        self.traces: list[tuple[tk.Variable, str]] = []
        self.apply_timer: str | None = None
        self.root.bind_all("<Return>", self.on_return, add="+")
        self.root.bind_all("<KP_Enter>", self.on_return, add="+")
        self.language = tk.StringVar(value="English")
        self.device = tk.StringVar(value="default")
        self.device_ids: dict[str, str] = {}  # Shown microphone name -> device id.
        self.mic_test: MicrophoneTest | None = None  # A running Test.
        self.model = tk.StringVar(value="")
        self.model_source = tk.StringVar(value="download")
        self.provider = tk.StringVar(value=next(iter(PROVIDERS)))
        self.endpoint = tk.StringVar(value="")
        self.api_model = tk.StringVar(value="")
        self.api_key = tk.StringVar(value="")
        self.account: clipui.AccountCard | None = None
        self.setup_mode = ""
        self.setup_steps: list[str] = []  # The first-run screens this setup shows.
        self.auto_prepare_recommended = False
        self.clipboard_page: clipui.ClipboardPage | None = None
        self.clipboard_opening = False
        # Opened by the history shortcut or menu: Esc (with no search typed) closes it.
        self.quick = page in ("clipboard", "clipboard-clear")
        self.clipboard_store: clipstore.Store | None = None
        # Settings rows that say whether a shortcut was heard: (label, kind, shortcut).
        self.shortcut_tests: list[tuple[ttk.Label, str, hotkeys.Shortcut]] = []
        self.shortcut_poll: str | None = None
        self.shortcut_panels: list[shortcut_panel.TestPanel] = []  # "Test it" widgets on the page.

    def show_first_page(self, page: str) -> None:
        """Open the page asked for, or the home page, or the first-run welcome."""
        if page == "shortcut":
            self.shortcut_page()
        elif page == "history-shortcut":
            self.shortcut_page(kind="history")
        elif (
            self.service.completed()
            and page in ("clipboard", "clipboard-clear")
            and self.features().clipboard
        ):
            self.clipboard()
        elif self.service.completed() and page == "settings":
            self.settings()
        elif self.service.completed() and not self.features().dictation:
            # Clipboard-only: there is no dictation page to show.
            self.clipboard()
        elif self.service.completed():
            self.home()
        else:
            self.welcome()

    def size_file(self) -> Path:
        return self.service.paths.config.parent / "window.json"

    def saved_size(self) -> tuple[int, int] | None:
        try:
            raw = d.read_json(self.size_file())
        except (d.DictationError, OSError, ValueError) as exc:
            log.debug("no saved window size: %s", exc)
            return None
        width, height = raw.get("width"), raw.get("height")
        if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
            return width, height
        return None

    def maximized(self) -> bool:
        try:
            if self.root.state() == "zoomed" or int(self.root.attributes("-fullscreen")):
                return True  # Zoomed on Windows and macOS.
            return sys.platform.startswith("linux") and bool(int(self.root.attributes("-zoomed")))
        except (tk.TclError, ValueError) as exc:
            log.debug("could not tell whether the window is maximized: %s", exc)
            return False

    def save_size(self) -> None:
        width, height = self.root.winfo_width(), self.root.winfo_height()
        if width < 200 or height < 200:
            return  # Never shown (or withdrawn): keep what was saved.
        if self.maximized():
            return  # Reopening at screen size, unmaximized, looks broken: keep the normal size.
        try:
            d.private_dir(self.size_file().parent)
            d.atomic(self.size_file(), json.dumps({"width": width, "height": height}))
        except OSError as exc:
            log.debug("could not save the window size: %s", exc)
            pass  # Only a convenience.

    def load_icon(self) -> tk.PhotoImage | None:
        try:
            return tk.PhotoImage(master=self.root, file=str(ICON))
        except tk.TclError as exc:
            log.debug("could not load the icon: %s", exc)
            return None

    def styles(self) -> None:
        self.fonts = app_styles.make_fonts()
        self.colors = app_styles.make_colors()
        self.modern = app_styles.apply(self.root, self.fonts)

    def panel(self, parent: tk.Misc, padding: Any) -> tuple[tk.Widget, ttk.Frame]:
        """A white, bordered surface: (what to pack, where its content goes). Rounded
        with the modern theme; a 1px outline around a frame with the classic one."""
        if self.modern:
            body = ttk.Frame(parent, style="Panel.TFrame", padding=padding)
            return body, body
        outline = tk.Frame(parent, background=BORDER, padx=1, pady=1)
        body = ttk.Frame(outline, style="Card.TFrame", padding=padding)
        body.pack(fill="both", expand=True)
        return outline, body

    def header(self) -> None:
        """Brand on the left; the tabs (or the setup step) on the right. Always in view."""
        bar = self.header_bar = ttk.Frame(self.root, style="Header.TFrame", padding=(PAD - 4, 10))
        bar.pack(fill="x")
        # Packed first so a narrow window clips the name, never the tabs.
        self.nav = ttk.Frame(bar, style="Header.TFrame")
        self.nav.pack(side="right")
        self.step = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.step, style="Step.TLabel").pack(side="right")
        brand = ttk.Frame(bar, style="Header.TFrame")
        brand.pack(side="left")
        if self.icon is not None:
            self.header_icon = self.icon.subsample(max(1, self.icon.width() // 28))
            ttk.Label(brand, image=self.header_icon, style="Brand.TLabel").pack(
                side="left", padx=(0, 8)
            )
        self.brand = ttk.Label(brand, text=hotkeys.APP_NAME, style="Brand.TLabel")
        self.brand.pack(side="left")
        tk.Frame(self.root, height=1, background=BORDER).pack(fill="x")

    def bottom_bar(self) -> None:
        """Actions and status. Hidden while it has nothing to show, to give the page room."""
        self.bottom = tk.Frame(self.root, background=BORDER)
        tk.Frame(self.bottom, height=1, background=BORDER).pack(fill="x")
        bar = self.bottom_inner = ttk.Frame(self.bottom, style="Header.TFrame", padding=(PAD, 8))
        bar.pack(fill="x")
        self.status.trace_add("write", lambda *_: self.update_bottom())
        self.bar_actions = ttk.Frame(bar, style="Header.TFrame")
        self.bar_actions.pack(side="right")
        self.bar_info = ttk.Frame(bar, style="Header.TFrame")
        self.bar_info.pack(side="left", fill="x", expand=True, padx=(0, 16))
        self.progress = ttk.Progressbar(self.bar_info, mode="indeterminate")
        self.status_label = ttk.Label(
            self.bar_info,
            textvariable=self.status,
            wraplength=max(200, self.wraplength - 180),
            style="Step.TLabel",
        )
        self.status_label.pack(anchor="w")
        # Wrap to the room the buttons leave, which a fixed guess got wrong in narrow windows.
        self.bar_info.bind(
            "<Configure>",
            lambda event: self.status_label.configure(wraplength=max(120, event.width)),
        )

    def show_toast(self, message: str) -> None:
        """Confirm an immediate preference save without making the user hunt for a button."""
        self.toast.set(message)
        self.toast_label.place(relx=1.0, rely=1.0, x=-18, y=-18, anchor="se")
        if self.toast_after is not None:
            self.root.after_cancel(self.toast_after)
        self.toast_after = self.root.after(2800, self.hide_toast)

    def hide_toast(self) -> None:
        self.toast_after = None
        self.toast_label.place_forget()
        self.toast.set("")

    def saved(self, message: str = "Settings saved.") -> None:
        self.status.set(message)
        self.show_toast(message)

    def show_toolbar(self, padding: tuple[int, int, int, int]) -> ttk.Frame:
        """The fixed area above the scrolling page, for this page's controls."""
        self.toolbar_padding = padding
        left, top, right, bottom = padding
        self.toolbar.configure(padding=(left + self.gutter, top, right + self.gutter, bottom))
        self.toolbar.pack(fill="x", before=self.container)
        return self.toolbar

    def update_bottom(self) -> None:
        self.bottom_timer = None
        wanted = bool(
            self.bar_actions.winfo_children() or self.status.get() or self.progress.winfo_manager()
        )
        if wanted and not self.bottom.winfo_manager():
            self.bottom.pack(side="bottom", fill="x", before=self.root.pack_slaves()[0])
        elif not wanted and self.bottom.winfo_manager():
            self.bottom.pack_forget()

    def scroll_key(self, amount: int, what: str, event: tk.Event[Any]) -> str | None:
        """Page Up/Down, Home and End scroll the page, except while typing in a field."""
        widget = event.widget
        if isinstance(widget, (ttk.Entry, tk.Entry, tk.Text)) or not self.scrollbar.winfo_manager():
            return None
        if what == "end":
            self.canvas.yview_moveto(0 if amount < 0 else 1)
        else:
            self.canvas.yview_scroll(amount, "pages")
        return "break"

    def scroll(self, first: float, last: float) -> None:
        # Show the scrollbar only when the page is taller than the window.
        fits = float(first) <= 0 and float(last) >= 1
        shown = bool(self.scrollbar.winfo_manager())
        if fits and shown:
            # Showing it narrowed the page and re-wrapped its text; hiding it again at
            # once can make the page too tall again, a loop that never settles (a page
            # just at the window's height, as with macOS fonts). So it stays a moment,
            # then goes if the settled page still fits.
            settled = time.monotonic() - self.scrollbar_shown_at >= SCROLLBAR_SETTLE
            if settled:
                self.scrollbar.pack_forget()
            elif self.scrollbar_recheck is None:
                self.scrollbar_recheck = self.root.after(
                    int(SCROLLBAR_SETTLE * 1000) + 50, self.recheck_scrollbar
                )
        elif not fits and not shown:
            self.scrollbar.pack(side="right", fill="y", before=self.canvas)
            self.scrollbar_shown_at = time.monotonic()
        self.scrollbar.set(first, last)

    def recheck_scrollbar(self) -> None:
        self.scrollbar_recheck = None
        self.scroll(*self.canvas.yview())

    def touchpad(self, event: tk.Event[Any]) -> None:
        """Tk packs signed X/Y pixel deltas into the high/low 16 bits."""
        widget = event.widget
        if (
            not self.scrollbar.winfo_manager()
            or not isinstance(widget, tk.Misc)
            or isinstance(widget, (tk.Text, tk.Listbox))
            or widget.winfo_toplevel() is not self.root
        ):
            return
        delta = int(event.delta) & 0xFFFF
        if delta >= 0x8000:
            delta -= 0x10000
        # The canvas scrolls in 20px rows. Keep sub-row gestures until they add
        # up, otherwise slow two-finger movement is rounded away on every event.
        self.touchpad_pixels -= delta
        steps = int(self.touchpad_pixels / 20)
        if steps:
            self.touchpad_pixels -= steps * 20
            self.canvas.yview_scroll(steps, "units")

    def wheel(self, event: tk.Event[Any]) -> None:
        """Scroll the page with the mouse wheel or trackpad, wherever the pointer is."""
        widget = event.widget
        if (
            not self.scrollbar.winfo_manager()
            or not isinstance(widget, tk.Misc)  # A combobox's list is only a name.
            or isinstance(widget, (tk.Text, tk.Listbox))  # They scroll themselves.
            or widget.winfo_toplevel() is not self.root
        ):
            return
        if event.num in (4, 5):
            steps = 3 if event.num == 5 else -3
        elif not event.delta:
            return
        elif sys.platform == "darwin":
            steps = -int(event.delta)  # Small, frequent deltas from trackpads.
        else:
            # A notch is 120; precision touchpads send less, which still moves a step.
            steps = -3 * int(event.delta / 120) or (-1 if event.delta > 0 else 1)
        if steps:
            self.canvas.yview_scroll(steps, "units")

    def resize_scroll_region(self, event: tk.Event[Any]) -> None:
        # From the canvas's left edge, so a centered column stays centered (a region
        # narrower than the canvas would be pinned to its left).
        bottom = (self.canvas.bbox("all") or (0, 0, 0, 0))[3]
        self.canvas.configure(scrollregion=(0, 0, self.canvas.winfo_width(), bottom))

    def resize_content(self, event: tk.Event[Any]) -> None:
        content = min(event.width, CONTENT_MAX)
        # Centered in the window, not the canvas, so the column (and the header lined up
        # with it) stays put when a scrollbar appears on a long page.
        full = max(event.width, self.container.winfo_width())
        gutter = max(0, min((full - content) // 2, event.width - content))
        self.canvas.itemconfigure(self.frame_window, width=content)
        self.canvas.coords(self.frame_window, gutter, 0)
        if gutter != self.gutter:
            self.gutter = gutter
            # The header, toolbar and bottom bar line up with the column.
            self.header_bar.configure(padding=(PAD - 4 + gutter, 8))
            self.bottom_inner.configure(padding=(PAD + gutter, 8))
            if self.toolbar.winfo_manager():
                self.show_toolbar(self.toolbar_padding)
        self.resize_scroll_region(event)
        # Re-wrap once the user stops dragging, not on every pixel.
        if self.rewrap_timer is not None:
            self.root.after_cancel(self.rewrap_timer)
        self.rewrap_timer = self.root.after(80, lambda: self.rewrap(content))

    def rewrap(self, canvas_width: int) -> None:
        """Let every wrapped label follow the column's width (they were sized for the old one)."""
        self.rewrap_timer = None
        # A narrow window keeps the icon and the tabs; the full name no longer fits.
        self.brand.configure(text=hotkeys.APP_NAME if canvas_width >= 700 else "")
        if self.clipboard_page is not None and self.page == "clipboard":
            self.clipboard_page.fit(canvas_width)
        wraplength = max(260, canvas_width - 2 * PAD - 30)  # As at start: padding and air.
        change = wraplength - self.wraplength
        if not change:
            return
        self.wraplength = wraplength
        stack: list[tk.Misc] = [self.frame]
        while stack:
            widget = stack.pop()
            stack.extend(widget.winfo_children())
            if widget.winfo_class() in ("TLabel", "Label"):
                current = int(str(widget.cget("wraplength") or 0))
                if current > 0:
                    widget.configure(wraplength=max(120, current + change))  # type: ignore[call-arg]

    def reset(self, page: str, title: str, subtitle: str, step: str = "") -> None:
        if self.page == "clipboard" and self.clipboard_page is not None:
            self.clipboard_page.cancel_search()
        if self.page == "shortcut" and page != "shortcut":
            self.end_capture()
        if self.shortcut_poll is not None:
            self.root.after_cancel(self.shortcut_poll)
            self.shortcut_poll = None
        self.shortcut_tests = []
        for panel in self.shortcut_panels:
            panel.cancel()
        self.shortcut_panels = []
        self.page = page
        self.settings_snapshot = None
        for variable, trace in self.traces:  # Page listeners on long-lived variables.
            variable.trace_remove("write", trace)
        self.traces = []
        self.buttons = []
        self.default_button = None
        self.account = None
        for area in (self.frame, self.bar_actions, self.toolbar, self.nav):
            for child in area.winfo_children():
                child.destroy()
        # An emptied frame keeps its old size in Tk, so the toolbar leaves until used again.
        self.toolbar.pack_forget()
        self.canvas.yview_moveto(0)
        # Home shows status beside Record; the bar keeps only the progress.
        if page == "home":
            self.status_label.pack_forget()
        elif not self.status_label.winfo_manager():
            self.status_label.pack(anchor="w")
        tabs = page in TAB_PAGES and self.service.completed()
        self.step.set("" if tabs else step)
        if tabs:
            self.draw_tabs(page)
        if title:
            ttk.Label(
                self.frame, text=title, style="Title.TLabel", wraplength=self.wraplength
            ).pack(anchor="w", pady=(0, 4))
        if subtitle:
            ttk.Label(
                self.frame, text=subtitle, wraplength=self.wraplength, style="Subtitle.TLabel"
            ).pack(anchor="w", pady=(0, 18))
        self.status.set("")
        # The page fills the bar after this; show or hide it once it has.
        if self.bottom_timer is None:
            self.bottom_timer = self.root.after_idle(self.update_bottom)

    def features(self) -> hotkeys.Features:
        return hotkeys.Preferences(self.service.paths).features()

    def tab_names(self) -> list[str]:
        features = self.features()
        return (
            (["clipboard"] if features.clipboard else [])
            + (["dictation"] if features.dictation else [])
            + ["settings"]
        )

    def settings_values(self) -> tuple[str, ...]:
        """The Transcription AI choice, which is applied explicitly (it may download)."""
        return (
            self.model_source.get(),
            self.model.get(),
            self.endpoint.get().strip(),
            self.api_model.get().strip(),
            self.api_key.get(),
        )

    def confirm_leave(self) -> bool:
        """Before leaving Settings with unsaved dictation changes, offer to save them."""
        if (
            self.page != "settings"
            or self.settings_snapshot is None
            or self.pending is not None
            or self.settings_values() == self.settings_snapshot
        ):
            return True
        answer = messagebox.askyesnocancel(
            "Save your changes?",
            "You changed the transcription AI but haven’t applied it.",
            parent=self.root,
        )
        if answer:
            self.prepare()  # Stays here to show how saving went.
        return answer is False

    def tab(self, name: str) -> None:
        if not self.confirm_leave():
            return
        self.quick = False  # Browsing now: Esc no longer closes the window.
        if name == "clipboard":
            self.clipboard()
        elif name == "dictation":
            self.home()
        else:
            self.settings()

    def draw_tabs(self, current: str) -> None:
        active = "dictation" if current == "home" else current
        labels = {"clipboard": "Clipboard", "dictation": "Dictation", "settings": "Settings"}
        for name in self.tab_names():
            ttk.Button(
                self.nav,
                text=labels[name],
                command=functools.partial(self.tab, name),
                style="Tab.Current.TButton" if name == active else "Tab.TButton",
                takefocus=False,
            ).pack(side="left", padx=(4, 0))

    def clipboard(self) -> None:
        self.reset("clipboard", "", "")
        if self.clipboard_store is None:
            self._show_clipboard_opening()
            if self.clipboard_opening:
                return
            self.clipboard_opening = True
            clipboard_path = self.service.paths.clipboard

            def open_store() -> tuple[clipstore.Store | None, Exception | None]:
                try:
                    return clipstore.Store(clipboard_path), None
                except Exception as exc:  # SQLite, filesystem, and schema errors are shown below.
                    return None, exc

            self.clipboard_open_future = self.background(open_store, self._clipboard_store_opened)
            return
        self.clipboard_page = clipui.ClipboardPage(self, self.clipboard_store)
        self.clipboard_page.render()

    def _show_clipboard_opening(self) -> None:
        body = self.card("Opening clipboard history", "Your saved items stay on this computer.")
        progress = ttk.Progressbar(body, mode="indeterminate", length=100)
        progress.pack(anchor="w", pady=(10, 0))
        progress.start(20)

    def _clipboard_store_opened(
        self, result: tuple[clipstore.Store | None, Exception | None]
    ) -> None:
        self.clipboard_open_future = None
        store, problem = result
        self.clipboard_opening = False
        if self.page != "clipboard":
            if store is not None:
                store.close()
            return
        if problem is not None or store is None:
            if problem is not None:
                telemetry.capture(problem, level="warning", page="clipboard")
            self.clipboard_page = None
            self.reset("clipboard", "", "")
            self.card(
                "Your clipboard history can’t be opened right now",
                str(problem)
                if isinstance(problem, clipstore.StoreError)
                else "The history file is busy or damaged. The clipboard service repairs "
                "a damaged file by itself; try again in a moment.",
            )
            self.button("Try again", self.clipboard, True, self.actions(), "right")
            return
        self.clipboard_store = store
        self.reset("clipboard", "", "")  # Drops the "Opening clipboard history" card.
        self.clipboard_page = clipui.ClipboardPage(self, store)
        self.clipboard_page.render()
        if getattr(self, "clipboard_clear_after_open", False):
            self.clipboard_clear_after_open = False
            self.root.after_idle(self.clear_clipboard_history)

    def card(self, heading: str = "", hint: str = "") -> ttk.Frame:
        outline, body = self.panel(self.frame, padding=(20, 16))
        outline.pack(fill="x", pady=(0, 14))
        if heading:
            ttk.Label(body, text=heading, style="CardHeading.TLabel").pack(anchor="w")
        if hint:
            ttk.Label(
                body, text=hint, style="CardHint.TLabel", wraplength=self.wraplength - 48
            ).pack(anchor="w", pady=(4, 10))
        return body

    def steps(self, parent: ttk.Frame, items: list[tuple[str, str]]) -> None:
        for index, (title, detail) in enumerate(items, start=1):
            row = ttk.Frame(parent, style="Card.TFrame")
            row.pack(fill="x", pady=5)
            badge = tk.Canvas(row, width=30, height=30, background=SURFACE, highlightthickness=0)
            badge.create_oval(1, 1, 29, 29, fill=ACCENT_SOFT, outline="")
            badge.create_text(15, 15, text=str(index), fill=ACCENT, font=self.fonts["badge"])
            badge.pack(side="left", anchor="n", padx=(0, 14))
            text = ttk.Frame(row, style="Card.TFrame")
            text.pack(side="left", fill="x", expand=True)
            ttk.Label(text, text=title, style="Card.TLabel").pack(anchor="w")
            if detail:
                ttk.Label(
                    text,
                    text=detail,
                    style="CardHint.TLabel",
                    wraplength=self.wraplength - 110,
                ).pack(anchor="w")

    def button(
        self,
        text: str,
        command: Callable[[], None],
        primary: bool = False,
        parent: tk.Misc | None = None,
        side: Literal["left", "right", ""] = "",
    ) -> ttk.Button:
        button = ttk.Button(
            parent or self.frame,
            text=text,
            command=command,
            style="Primary.TButton" if primary else "TButton",
        )
        if side:
            button.pack(side=side, padx=(8, 0) if side == "right" else (0, 8))
        else:
            button.pack(fill="x", pady=5)
        if primary and parent is self.bar_actions:
            self.default_button = button  # The page's main action: Enter presses it.
        self.buttons.append(button)
        return button

    def on_return(self, event: tk.Event[Any]) -> str | None:
        """Enter presses the focused button, or else the page's main action."""
        widget = event.widget
        if (
            not isinstance(widget, tk.Misc)
            or widget.winfo_toplevel() is not self.root
            or isinstance(widget, tk.Text)
            or self.page in ("clipboard", "shortcut", "closed")  # They handle Enter.
        ):
            return None
        target = widget if isinstance(widget, ttk.Button) else self.default_button
        if target is None or not target.winfo_exists() or target.instate(["disabled"]):
            return None
        target.invoke()
        return "break"

    def actions(self) -> ttk.Frame:
        return self.bar_actions

    def welcome(self) -> None:
        self.reset(
            "welcome",
            "Your voice. Your clipboard.",
            "Dictate anywhere and keep a searchable history of what you copy. Setup takes "
            "a few minutes, including a one-time speech model download.",
        )
        if self.icon is not None:
            self.hero_icon = self.icon.subsample(4)
            hero = ttk.Label(self.frame, image=self.hero_icon)
            hero.pack(anchor="w", pady=(0, 14), before=self.frame.winfo_children()[0])
        body = self.card("How it works")
        self.steps(
            body,
            [
                ("Choose what you want to use", "Dictation, clipboard history, or both."),
                ("Follow a short setup", "Only the steps for your choice."),
                ("Use it anywhere", "A shortcut and the menu icon are always there."),
            ],
        )
        privacy = self.card()
        ttk.Label(
            privacy,
            text="Private by design. Transcription runs on this computer — no account, no subscription. "
            "The microphone only turns on when you press Record.",
            style="CardHint.TLabel",
            wraplength=self.wraplength - 50,
        ).pack(anchor="w")
        # An explicit, optional choice, off until ticked (nothing is sent before this step).
        self.welcome_consent = tk.BooleanVar(master=self.root, value=False)
        ttk.Checkbutton(
            privacy,
            text="Share anonymous crash reports and usage statistics (optional)",
            variable=self.welcome_consent,
            style="Card.TCheckbutton",
        ).pack(anchor="w", pady=(10, 0))
        ttk.Label(
            privacy,
            text="Never clipboard contents, transcripts, audio or keys. Change it anytime in Settings.",
            style="CardHint.TLabel",
            wraplength=self.wraplength - 50,
        ).pack(anchor="w", padx=(26, 0))
        self.button("Get started", self.begin_setup, True, self.actions(), "right")

    def begin_setup(self) -> None:
        record = getattr(telemetry, "set_consent", None)
        choice = getattr(self, "welcome_consent", None)
        if record is not None and choice is not None:
            record(bool(choice.get()))
        self.choose_features()

    def choose_features(self, note: str = "") -> None:
        self.reset(
            "features",
            "What would you like to use?",
            "You can change this any time in Settings.",
            "Step 1",
        )
        self.mode_var = tk.StringVar(value=self.setup_mode or "both")
        card = self.card()
        for value, title, hint in MODES:
            ttk.Radiobutton(
                card, text=title, value=value, variable=self.mode_var, style="Card.TRadiobutton"
            ).pack(anchor="w", pady=(8, 0))
            ttk.Label(
                card, text=hint, style="CardHint.TLabel", wraplength=self.wraplength - 80
            ).pack(anchor="w", padx=(26, 0))
        if note:
            ttk.Label(self.frame, text=note, style="Hint.TLabel").pack(anchor="w")
        self.privacy_card()
        self.button("Back", self.welcome, parent=self.actions(), side="left")
        self.button(
            "Continue",
            lambda: self.after_features(self.mode_var.get()),
            True,
            self.actions(),
            "right",
        )

    def step_label(self, name: str) -> str:
        """ "Step 2 of 4" for a first-run screen, counted over the steps this setup needs."""
        if self.service.completed() or name not in self.setup_steps:
            return ""
        return f"Step {self.setup_steps.index(name) + 1} of {len(self.setup_steps)}"

    def after_features(self, mode: str) -> None:
        """Start only the setup steps the chosen features need."""
        self.setup_mode = mode
        dictation = mode != "clipboard"
        self.auto_prepare_recommended = dictation
        prefs = hotkeys.Preferences(self.service.paths)
        prefs.save(features=hotkeys.Features(dictation, mode != "dictation"))
        self.setup_steps = [
            "features",
            *(("dictation",) if dictation and not self.service.ready() else ()),
            "done",
        ]
        if self.service.ready():
            self.after_dictation_setup()
        else:
            self.settings()
            self.prepare_recommended_setup()

    def prepare_recommended_setup(self) -> None:
        """Start the recommended local model during first-run setup."""
        if (
            self.page != "settings"
            or not self.auto_prepare_recommended
            or self.service.completed()
            or self.model_source.get() != "download"
            or self.pending is not None
        ):
            return
        if desktop.platform_name() == "windows" and self.device.get() == "default":
            # Windows recording needs a concrete input device. Prefer the first device
            # discovered during setup, while keeping the picker available to change it.
            device = next((item for item in self.device_ids.values() if item != "default"), "")
            if not device:
                return  # show_microphones() will retry when discovery finishes.
            self.device.set(next(name for name, item in self.device_ids.items() if item == device))
        self.auto_prepare_recommended = False
        self.prepare()

    def after_dictation_setup(self) -> None:
        self.tutorial()

    def apply_mode(self, mode: str) -> None:
        features = {"dictation": (True, False), "clipboard": (False, True), "both": (True, True)}
        dictation, clipboard = features[mode]
        hotkeys.Preferences(self.service.paths).save(
            features=hotkeys.Features(dictation, clipboard)
        )
        self.settings()
        if dictation and not self.service.ready():
            # Setup now continues on this page; don't claim it's ready yet.
            self.status.set("Saved. Finish setting up dictation below, then choose Continue.")
        else:
            self.saved("Settings saved. Your tools are ready to use.")

    def features_card(self) -> None:
        card = self.card("What you use", "Changes apply at once.")
        current = self.features()
        mode = (
            "both"
            if current.dictation and current.clipboard
            else ("clipboard" if current.clipboard else "dictation")
        )
        self.settings_mode = tk.StringVar(value=mode)
        for value, title, _ in MODES:
            ttk.Radiobutton(
                card,
                text=title,
                value=value,
                variable=self.settings_mode,
                style="Card.TRadiobutton",
                command=lambda: self.apply_mode(self.settings_mode.get()),
            ).pack(anchor="w", pady=(6, 0))

    def sound_cues_row(self, parent: ttk.Frame, config: d.Config, setup: bool) -> None:
        """Choose the audible cues (start, stop, error): saved as soon as it is picked."""
        ttk.Label(parent, text="Sound cues", style="Card.TLabel").pack(anchor="w", pady=(14, 0))
        labels = dict(app_settings.SOUND_CHOICES)
        shown = tk.StringVar(master=self.root, value=labels.get(config.s("sounds"), ""))
        picker = ttk.Combobox(
            parent, textvariable=shown, state="readonly", values=tuple(labels.values())
        )
        picker.pack(fill="x", pady=(6, 0))

        def save(_: object = None) -> None:
            mode = next((key for key, text in labels.items() if text == shown.get()), "errors")
            try:
                self.service.set_sounds(mode)
            except (d.DictationError, OSError) as exc:
                self.status.set(str(exc))
                return
            telemetry.event("setting_changed", setting="notifications", on=mode != "off")
            cues.play(
                "error" if mode != "off" else "start", mode
            )  # A sample of what it sounds like.
            self.saved("Saved. It applies to your next recording.")

        if not setup:
            picker.bind("<<ComboboxSelected>>", save)

    def permissions_card(self) -> None:
        """What macOS still needs allowed (Accessibility, microphone), each with its Settings link."""
        needed = self.service.permission_help()
        if not needed:
            return
        card = self.card("Allow access", "Clipboard+ can’t do everything until macOS allows it.")
        for title, why, url in needed:
            ttk.Label(
                card, text=why, style="CardHint.TLabel", wraplength=self.wraplength - 80
            ).pack(anchor="w", pady=(8, 4))
            self.button(
                title,
                functools.partial(self.open_privacy_settings, url),
                parent=card,
                side="left",
            )

    def open_privacy_settings(self, url: str) -> None:
        if not permissions.open_settings(url):
            self.status.set(f"Open this in your browser or Settings: {url}")

    def general_card(self) -> None:
        card = self.card("General")
        prefs = hotkeys.Preferences(self.service.paths)
        self.open_at_login_var = tk.BooleanVar(master=self.root, value=prefs.open_at_login())

        def save() -> None:
            enabled = self.open_at_login_var.get()
            prefs.save(open_at_login=enabled)
            if desktop.platform_name() == "macos":
                bundle = desktop.macos_bundle()
                if bundle.endswith(".app"):
                    hotkeys.set_login_item(enabled, hotkeys.bundle_login_command(bundle))
            else:
                hotkeys.set_login_item(enabled, desktop.persistent_relaunch("tray"))
            self.saved("Preference saved.")

        ttk.Checkbutton(
            card,
            text="Open at login",
            variable=self.open_at_login_var,
            command=save,
            style="Card.TCheckbutton",
        ).pack(anchor="w", pady=(6, 0))

    def privacy_card(self) -> None:
        card = self.card(
            "Privacy",
            "Off unless you turn it on: optional crash reports and feature counts help "
            "improve the app. Reports never include clipboard contents, transcripts, audio "
            "or account keys.",
        )
        prefs = hotkeys.Preferences(self.service.paths)
        # telemetry's opt-in consent API when this build has it; the saved preference otherwise.
        consented = getattr(telemetry, "has_consent", None)
        self.share_usage = tk.BooleanVar(
            master=self.root, value=bool(consented()) if consented else prefs.share_usage()
        )

        def save() -> None:
            agree = self.share_usage.get()
            record = getattr(telemetry, "set_consent", None)
            if record is not None:
                record(agree)
            else:
                prefs.save(share_usage=agree)
            self.saved("Privacy preference saved." if agree else "Sharing is off.")

        ttk.Checkbutton(
            card,
            text="Share anonymous crash reports and usage statistics",
            variable=self.share_usage,
            command=save,
            style="Card.TCheckbutton",
        ).pack(anchor="w", pady=(6, 0))
        if os.environ.get("DO_NOT_TRACK", "") not in ("", "0") or os.environ.get(
            "DICTATION_TELEMETRY", ""
        ).lower() in ("0", "false", "off"):
            ttk.Label(
                card,
                text="Reporting is disabled by your environment settings.",
                style="CardHint.TLabel",
            ).pack(anchor="w", pady=(6, 0))

    def update_card(self) -> None:
        card = self.card(
            "Software Update",
            f"Clipboard+ checks once a day whether a new version is available. "
            f"You are on version {desktop.APP_VERSION}.",
        )
        prefs = hotkeys.Preferences(self.service.paths)
        self.auto_updates = tk.BooleanVar(master=self.root, value=prefs.auto_updates())
        self.update_status = tk.StringVar(value="")

        def save() -> None:
            prefs.save(auto_updates=self.auto_updates.get())
            self.saved("Update preference saved.")

        ttk.Checkbutton(
            card,
            text="Check for updates automatically",
            variable=self.auto_updates,
            command=save,
            style="Card.TCheckbutton",
        ).pack(anchor="w", pady=(6, 0))
        self.update_label = ttk.Label(card, textvariable=self.update_status, style="Card.TLabel")
        self.update_label.pack(anchor="w", pady=(6, 0))
        self.update_button = self.button("Check Now", self.check_update, parent=card)
        self.update_button.configure(style="TButton")
        self.update_button.pack_forget()
        self.update_button.pack(anchor="w", pady=(6, 0))
        # What the last check found, without touching the network to build this page.
        update_state = updates.read_state(self.service.paths)
        remembered = update_state.get("offered")
        if isinstance(remembered, dict) and remembered.get("version"):
            self.show_update(
                {
                    "version": str(remembered["version"]),
                    "tag": str(remembered.get("tag", "")),
                    "url": str(remembered.get("url", "")),
                }
                if updates.newer(str(remembered["version"]), desktop.APP_VERSION)
                else None
            )
        if update_state.get("status") == "failed":
            self.update_status.set(str(update_state.get("error") or "The update did not complete."))
        elif update_state.get("status") == "installed":
            self.update_status.set("Update installed. Restart Clipboard+ to use it.")

    def check_update(self) -> None:
        """Look for a newer version now (the once-a-day limit does not apply)."""
        self.update_button.state(["disabled"])
        self.update_status.set("Checking…")

        def check() -> dict[str, str] | updates.UpdateError | None:
            try:
                return updates.check(self.service.paths, force=True)
            except updates.UpdateError as exc:
                return exc

        self.submit(
            check,
            self.show_update,
            "",
        )

    def show_update(self, found: dict[str, str] | updates.UpdateError | None) -> None:
        self.update_button.state(["!disabled"])
        if isinstance(found, updates.UpdateError):
            self.update_button.configure(text="Check Now", command=self.check_update)
            self.update_status.set(str(found))
            return
        if found is None:
            self.update_button.configure(text="Check Now", command=self.check_update)
            self.update_status.set(
                "No published update is available yet."
                if updates.read_state(self.service.paths).get("published") is False
                else f"Clipboard+ {desktop.APP_VERSION} is up to date."
            )
            return
        self.update_status.set(f"Version {found['version']} is available.")
        self.update_button.configure(text=f"Update to {found['version']}")
        self.update_button.configure(command=lambda: self.install_update(found))

    def install_update(self, found: dict[str, str]) -> None:
        self.update_button.state(["disabled"])
        self.update_target = found["version"]
        self.update_status.set(
            f"Downloading version {found['version']}… This window can be closed."
        )
        try:
            updates.start_updater(found["version"], found["url"])
        except OSError:
            self.update_target = ""
            self.update_button.state(["!disabled"])
            self.update_status.set(
                "The update could not start. Try again, or re-run the installer."
            )

    def refresh_update_result(self) -> None:
        """Show the detached updater's outcome in an open Settings window."""
        target = getattr(self, "update_target", "")
        if not target or not self.update_button.winfo_exists():
            return
        state = updates.read_state(self.service.paths)
        if state.get("target") != target:
            return
        if state.get("status") == "failed":
            self.update_status.set(str(state.get("error") or "The update did not complete."))
            self.update_button.state(["!disabled"])
            self.update_target = ""
        elif state.get("status") == "installed":
            self.update_status.set("Update installed. Restart Clipboard+ to use it.")
            self.update_target = ""

    def save_clipboard_options(self, keep_items: int, keep_days: int, images: bool) -> bool:
        prefs = hotkeys.Preferences(self.service.paths)
        previous = prefs.clipboard()
        if (
            keep_items < previous.keep_items or keep_days < previous.keep_days
        ) and not messagebox.askyesno(
            "Remove older clipboard items?",
            "Reducing these limits may permanently delete older items on this computer. "
            "Favorites and your Clipboard+ account are kept. Apply these limits?",
            parent=self.root,
        ):
            return False
        prefs.save(
            clipboard=dataclasses.replace(
                prefs.clipboard(), keep_items=keep_items, keep_days=keep_days, images=images
            )
        )
        return True

    def clipboard_options_card(self) -> None:
        card = self.card(
            "Clipboard history", "Saved on this computer. Favorites are never removed."
        )
        saved = hotkeys.Preferences(self.service.paths).clipboard()
        items = tk.StringVar(value=str(saved.keep_items))
        days = tk.StringVar(value=str(saved.keep_days))
        images = tk.BooleanVar(value=saved.images)

        def save(*_: object) -> None:
            try:
                if not self.save_clipboard_options(int(items.get()), int(days.get()), images.get()):
                    current = hotkeys.Preferences(self.service.paths).clipboard()
                    items.set(str(current.keep_items))
                    days.set(str(current.keep_days))
                    images.set(current.images)
                    return
            except (d.DictationError, OSError, ValueError) as exc:
                telemetry.capture(exc, level="warning", page="settings")
                message = (
                    "Clipboard settings couldn’t be saved. Check storage space and permissions, "
                    "then try again."
                )
                self.status.set(message)
                self.show_toast("Settings weren’t saved.")
                return
            self.saved()

        for label, variable, values in (
            ("Keep up to this many items", items, ("100", "500", "1000", "5000", "10000")),
            ("Remove items older than (days)", days, ("7", "30", "90", "365")),
        ):
            ttk.Label(card, text=label, style="Card.TLabel").pack(anchor="w", pady=(8, 2))
            box = ttk.Combobox(card, textvariable=variable, values=values, state="readonly")
            box.pack(fill="x")
            box.bind("<<ComboboxSelected>>", save)
        ttk.Checkbutton(
            card, text="Save images", variable=images, style="Card.TCheckbutton", command=save
        ).pack(anchor="w", pady=(10, 0))
        row = ttk.Frame(card, style="Card.TFrame")
        row.pack(fill="x", pady=(10, 0))
        self.button(
            "Delete all clipboard data", self.delete_clipboard_data, parent=row, side="left"
        )
        pause_row = ttk.Frame(card, style="Card.TFrame")
        pause_row.pack(fill="x", pady=(10, 0))
        if saved.paused(time.time()):
            ttk.Label(pause_row, text="Capture is paused.", style="Card.TLabel").pack(side="left")
            self.button(
                "Resume capture",
                lambda: self.set_capture_paused(None),
                parent=pause_row,
                side="right",
            )
        else:
            self.button(
                "Pause until I resume",
                lambda: self.set_capture_paused(-1.0),
                parent=pause_row,
                side="right",
            )
            self.button(
                "Pause 1 hour",
                lambda: self.set_capture_paused(3600.0),
                parent=pause_row,
                side="right",
            )

    def set_capture_paused(self, seconds: float | None) -> None:
        """`None` resumes capture; -1 pauses until resumed; a positive number pauses that long."""
        prefs = hotkeys.Preferences(self.service.paths)
        until = 0.0 if seconds is None else seconds if seconds == -1.0 else time.time() + seconds
        try:
            prefs.save(clipboard=dataclasses.replace(prefs.clipboard(), paused_until=until))
        except (d.DictationError, OSError) as exc:
            telemetry.capture(exc, level="warning", page="settings")
            self.status.set(
                "Clipboard capture couldn’t be changed. Check storage space and permissions, "
                "then try again."
            )
            self.show_toast("Settings weren’t saved.")
            return
        self.saved("Capture resumed." if seconds is None else "Clipboard capture paused.")
        self.settings()

    def delete_clipboard_data(self) -> None:
        if messagebox.askyesno(
            "Delete all clipboard data?",
            "This erases your clipboard history, favorites and images on this computer. "
            "Your Clipboard+ account is not changed.",
            parent=self.root,
        ):
            self.service.delete_clipboard_data()
            self.status.set("Clipboard data deleted.")

    def shortcuts_card(self) -> None:
        """The global shortcuts. Each is recorded on the same page and reports the same way."""
        features = self.features()
        prefs = hotkeys.Preferences(self.service.paths)
        card = self.card(
            "Keyboard shortcuts",
            "They work in any app."
            + (
                " ⇧⌘D and ⇧⌘F also belong to some browsers and editors; Test it shows whether "
                "yours is heard, and Change… picks another in one click."
                if desktop.platform_name() == "macos"
                else " Test it shows whether a shortcut is heard, and offers another if not."
            ),
        )
        if features.dictation:
            self.shortcut_row(card, "dictation", prefs.shortcut())
        if features.clipboard:
            self.shortcut_row(card, "history", prefs.history_shortcut())
        if self.shortcut_tests:
            self.shortcut_poll = self.root.after(1000, self.poll_shortcut_tests)

    def shortcut_row(self, card: tk.Misc, kind: str, shortcut: hotkeys.Shortcut | None) -> None:
        paths = self.service.paths
        title = "Dictation" if kind == "dictation" else "Open clipboard history"
        row = ttk.Frame(card, style="Card.TFrame")
        row.pack(fill="x", pady=(0 if kind == "dictation" else 10, 0))
        ttk.Label(
            row, text=f"{title}: {shortcut.label() if shortcut else 'Off'}", style="Card.TLabel"
        ).pack(side="left")
        self.button(
            "Change…",
            lambda: self.shortcut_page(back=self.settings, kind=kind),
            parent=row,
            side="right",
        )
        if kind == "history" and shortcut is not None:
            self.button("Turn off", self.turn_off_history_shortcut, parent=row, side="right")
        if shortcut is None:
            return
        wrap = self.wraplength - 50
        panel = shortcut_panel.TestPanel(self.root, paths, card, kind, wrap)
        self.shortcut_panels.append(panel)
        self.button("Test it", panel.start, parent=row, side="right")
        test = ttk.Label(card, text="", style="CardHint.TLabel", wraplength=wrap)
        test.pack(anchor="w", pady=(2, 0))
        self.shortcut_tests.append((test, kind, shortcut))
        test.configure(text=self.shortcut_test_line(kind, shortcut))
        panel.frame.pack(anchor="w", pady=(2, 0))
        status = hotkeys.STATUS_NAMES[kind]
        conflict = hotkeys.shortcut_conflict(paths, status)
        if conflict is not None:
            self.conflict_note(card, shortcut, conflict, status, self.settings)
        elif not hotkeys.shortcut_working(paths, status):
            command = (
                hotkeys.history_command(Path(__file__).resolve().parent)
                if kind == "history"
                else ["~/.local/bin/dictate-toggle"]
            )
            command = hotkeys.via_shortcut(command)
            problem = app_settings.shortcut_problem(
                shortcut.label(),
                desktop.platform_name(),
                hotkeys.shortcut_message(paths, status),
                shlex.join(command),
            )
            ttk.Label(card, text=problem, style="CardHint.TLabel", wraplength=wrap).pack(
                anchor="w", pady=(6, 0)
            )

    def shortcut_test_line(self, kind: str, shortcut: hotkeys.Shortcut) -> str:
        """ "Ready — press it to test", or that it was just heard, or why it can't work."""
        paths = self.service.paths
        status = hotkeys.STATUS_NAMES[kind]
        working = hotkeys.shortcut_working(paths, status)
        platform = desktop.platform_name()
        return app_settings.shortcut_test_text(
            shortcut.label(),
            working and hotkeys.heard_recently(paths, kind, shortcut),
            working,
            hotkeys.shortcut_message(paths, status),
            hotkeys.log_location("menubar" if platform == "macos" else "tray"),
            platform,
            hotkeys.shortcut_outcome(paths, kind, shortcut),
        )

    def poll_shortcut_tests(self) -> None:
        """Refresh the "heard it" lines once a second while Settings is open."""
        self.shortcut_poll = None
        if self.page != "settings" or not self.shortcut_tests:
            return  # Stops when the page changes.
        for label, kind, shortcut in self.shortcut_tests:
            label.configure(text=self.shortcut_test_line(kind, shortcut))
        self.shortcut_poll = self.root.after(1000, self.poll_shortcut_tests)

    def turn_off_history_shortcut(self) -> None:
        hotkeys.Preferences(self.service.paths).save(history_shortcut=False)
        self.settings()
        self.status.set("The clipboard history shortcut is off.")
        self.show_toast("Settings saved.")

    def conflict_note(
        self,
        parent: tk.Misc,
        shortcut: hotkeys.Shortcut,
        conflict: hotkeys.Conflict,
        status: str,
        redraw: Callable[[], None],
    ) -> None:
        """Explain that another shortcut takes these keys, with a way to take them back."""
        text = f"{shortcut.label()} is also used by {conflict.name}, which gets it first." + (
            "" if conflict.path else " Choose another shortcut."
        )
        style = "CardHint.TLabel" if str(parent.cget("style")).startswith("Card") else "Hint.TLabel"
        ttk.Label(parent, text=text, style=style, wraplength=self.wraplength - 50).pack(
            anchor="w", pady=(6, 0)
        )
        if conflict.path:
            self.button(
                f"Use {shortcut.label()} here instead",
                lambda: self.take_over(conflict, status, redraw),
                parent=parent,
            ).pack_configure(anchor="w", fill="none", pady=(6, 0))

    def clipboard_settings(self) -> None:
        title, subtitle = (
            ("Clipboard settings", "Manage history, sync, privacy and app behavior.")
            if not self.service.completed()
            else ("Settings", "Manage your clipboard history, account access and app behavior.")
        )
        self.reset("settings", title, subtitle)
        if self.service.completed():  # During setup the choice was just made.
            self.features_card()
            self.shortcuts_card()
            self.general_card()
            self.permissions_card()
        self.clipboard_options_card()
        self.clipboard_plus_card()
        self.privacy_card()
        self.update_card()
        self.button("Done", self.leave, True, self.actions(), "right")

    def tutorial_clipboard(self) -> None:
        self.reset(
            "tutorial",
            "You’re all set",
            "Copy things as usual. We keep them for you.",
            self.step_label("done"),
        )
        place = {"macos": "menu bar", "windows": "system tray"}.get(
            desktop.platform_name(), "top bar"
        )
        history = hotkeys.Preferences(self.service.paths).history_shortcut()
        body = self.card()
        self.steps(
            body,
            [
                ("Copy anything", "Text, links and images are saved on this computer."),
                (
                    "Open your history",
                    f"Press {history.label()} in any app, or choose Clipboard History… "
                    f"from the {place} icon. Change the shortcut in Settings."
                    if history
                    else f"From the {place} icon, choose Clipboard History…",
                ),
                ("Search, star, copy back", "Favorites are kept when you clear the history."),
            ],
        )
        self.clipboard_plus_card()
        if self.service.completed():  # Reopened to reread: back to the history.
            self.button("Back", self.clipboard, True, self.actions(), "right")
            return
        self.button("Done", self.finish_setup, True, self.actions(), "right")
        self.button("Back", self.choose_features, parent=self.actions(), side="left")

    def settings(self) -> None:
        if not self.features().dictation:
            self.clipboard_settings()
            return
        setup = not self.service.completed()
        if setup:
            self.reset(
                "settings",
                "Set up dictation",
                "Choose how you’ll speak. You can change this anytime.",
                self.step_label("dictation"),
            )
        else:
            self.reset(
                "settings",
                "Settings",
                "Manage Dictation, clipboard history, account access and app behavior.",
            )
        try:
            config = d.Config(self.service.paths)
        except d.DictationError as exc:
            telemetry.capture(exc, level="warning", page="settings")
            self.card("Your dictation settings can’t be read", str(exc))
            self.button(
                "Reset dictation settings",
                self.reset_dictation_settings,
                True,
                self.actions(),
                "right",
            )
            return
        self.load_settings(config)
        remote = config.s("backend") == "http"
        self.voice_card(config, setup)
        self.ai_card(config, setup, remote)
        if not setup:  # During setup the choice was just made; changing it here strands it.
            self.rewrite_options_card(config)
            self.features_card()
            self.shortcuts_card()
            self.general_card()
            self.permissions_card()
        if self.features().clipboard:
            self.clipboard_options_card()
        self.clipboard_plus_card()
        if not setup:
            self.privacy_card()
            self.update_card()
        if setup:
            self.button("Continue", self.prepare, True, self.actions(), "right")
            self.button("Back", self.choose_features, parent=self.actions(), side="left")
        else:
            self.settings_snapshot = self.settings_values()
        # Discovery only lists devices; it never opens the microphone.
        self.root.after_idle(self.find_microphones)

    def load_settings(self, config: d.Config) -> None:
        """Put the saved dictation settings into the form variables."""
        self.language.set(app_settings.language_label(config.s("language")))
        self.device.set(config.s("device"))
        existing = config.s("model") if os.path.isfile(config.s("model")) else ""
        self.model.set(existing)
        remote = config.s("backend") == "http"
        self.model_source.set("service" if remote else "file" if existing else "download")
        self.endpoint.set(config.s("endpoint"))
        self.api_model.set(config.s("api_model"))
        self.api_key.set("")
        self.provider.set(app_settings.provider_name(config.s("endpoint"), remote, PROVIDERS))

    def rewrite_options_card(self, config: d.Config) -> None:
        """Expose optional concise-draft setup alongside the other dictation settings."""
        configured = bool(config.s("rewrite_endpoint") and config.s("rewrite_model"))
        card = self.card(
            "Concise drafts",
            "Make a shorter draft from a transcript, review it, then choose whether to copy it.",
        )
        detail = (
            "Configured. Transcripts are sent only when you choose Make concise."
            if configured
            else "Optional. Set up a local text service or your own HTTPS service."
        )
        ttk.Label(card, text=detail, style="CardHint.TLabel", wraplength=self.wraplength - 60).pack(
            anchor="w", pady=(2, 8)
        )
        self.button(
            "Change settings" if configured else "Set up concise drafts",
            self.open_rewrite_settings,
            parent=card,
        )

    def open_rewrite_settings(self) -> None:
        rewriteui.SettingsDialog(
            self.root,
            d.Config(self.service.paths),
            self.service.paths,
            self.concise_settings_saved,
        )

    def concise_settings_saved(self) -> None:
        self.settings()
        self.saved("Concise draft settings saved.")

    def voice_card(self, config: d.Config, setup: bool) -> None:
        """Language, microphone (with Test) and the dictation switches."""
        voice = self.card("Dictation")
        ttk.Label(voice, text="Language", style="Card.TLabel").pack(anchor="w")
        language = ttk.Combobox(
            voice,
            textvariable=self.language,
            state="readonly",
            values=app_settings.LANGUAGES,
        )
        language.pack(fill="x", pady=(6, 16))
        ttk.Label(voice, text="Microphone", style="Card.TLabel").pack(anchor="w")
        row = ttk.Frame(voice, style="Card.TFrame")
        row.pack(fill="x", pady=(6, 0))
        self.device_picker = ttk.Combobox(
            row, textvariable=self.device, values=("default",), state="readonly"
        )
        self.device_picker.pack(side="left", fill="x", expand=True)
        if not setup:  # Saved as chosen, like the switches below.
            for picker in (language, self.device_picker):
                picker.bind("<<ComboboxSelected>>", lambda _: self.save_voice())
        self.button("Refresh", self.find_microphones, parent=row, side="right")
        self.mic_button = self.button("Test", self.test_microphone, parent=row, side="right")
        # A live level meter while testing (hidden otherwise).
        self.meter = tk.Canvas(voice, height=10, background=SURFACE, highlightthickness=0)
        for key, text in app_settings.VOICE_SWITCHES:
            switch = tk.BooleanVar(master=self.root, value=config.b(key))
            ttk.Checkbutton(
                voice,
                text=text,
                variable=switch,
                style="Card.TCheckbutton",
                command=functools.partial(self.set_option, key, switch),
            ).pack(anchor="w", pady=(10 if key == "overlay" else 4, 0))
        self.sound_cues_row(voice, config, setup)

    def ai_card(self, config: d.Config, setup: bool, remote: bool) -> None:
        """Where transcription runs: on-device, a model file, or an AI service."""
        ai = self.card("Transcription AI", "What turns your voice into text.")
        self.choices: dict[str, tuple[ttk.Frame, ttk.Widget]] = {}
        for value, title, hint in app_settings.MODEL_CHOICES:
            anchor: ttk.Widget = ttk.Radiobutton(
                ai,
                text=title,
                value=value,
                variable=self.model_source,
                style="Card.TRadiobutton",
                command=self.show_choice,
            )
            anchor.pack(anchor="w", pady=(8, 0))
            if hint:
                anchor = ttk.Label(
                    ai, text=hint, style="CardHint.TLabel", wraplength=self.wraplength - 80
                )
                anchor.pack(anchor="w", padx=(26, 0))
            self.choices[value] = (ttk.Frame(ai, style="Card.TFrame"), anchor)
        chooser = self.choices["file"][0]
        self.button("Choose file…", self.choose_model, parent=chooser, side="left")
        self.model_label = ttk.Label(chooser, style="CardHint.TLabel")
        self.model_label.pack(side="left", fill="x", expand=True)
        self.show_model()
        fields = self.choices["service"][0]
        provider = ttk.Combobox(
            fields, textvariable=self.provider, state="readonly", values=tuple(PROVIDERS)
        )
        provider.pack(fill="x", pady=(8, 0))
        provider.bind("<<ComboboxSelected>>", lambda _: self.choose_provider())
        for label, variable, secret in (
            ("Service URL", self.endpoint, False),
            ("Model", self.api_model, False),
            ("API key", self.api_key, True),
        ):
            ttk.Label(fields, text=label, style="CardHint.TLabel").pack(anchor="w", pady=(8, 2))
            ttk.Entry(fields, textvariable=variable, show="•" if secret else "").pack(fill="x")
        ttk.Label(
            fields,
            text="Your key is saved only on this computer, readable only by you."
            + (
                " Leave it blank to keep the saved key."
                if d.key_file(config.paths).is_file()
                else ""
            ),
            style="CardHint.TLabel",
            wraplength=self.wraplength - 80,
        ).pack(anchor="w", pady=(6, 0))
        if not remote:
            self.choose_provider()
        self.show_choice()
        if not setup:
            # The AI choice is applied on purpose: it can download a model or check a key.
            self.apply_row = ttk.Frame(ai, style="Card.TFrame")
            self.apply_button = self.button("Apply", self.prepare, True, self.apply_row, "right")
            self.default_button = self.apply_button
            ttk.Label(self.apply_row, text="Not applied yet.", style="CardHint.TLabel").pack(
                side="right"
            )
            for variable in (
                self.model_source,
                self.model,
                self.endpoint,
                self.api_model,
                self.api_key,
            ):
                self.traces.append(
                    (variable, variable.trace_add("write", lambda *_: self.after_edit()))
                )

    def save_voice(self) -> None:
        language = app_settings.language_code(self.language.get())
        device = self.device_ids.get(self.device.get(), self.device.get())
        try:
            self.service.set_voice(language, device)
        except (d.DictationError, OSError) as exc:
            self.status.set(str(exc))
            return
        telemetry.event("setting_changed", setting="voice", on=True)
        self.saved("Saved. It applies to your next recording.")

    def after_edit(self) -> None:
        if self.apply_timer is None:
            self.apply_timer = self.root.after_idle(self.show_apply)

    def show_apply(self) -> None:
        """Show Apply only while the AI choice differs from what's in use."""
        self.apply_timer = None
        if self.page != "settings" or not self.apply_row.winfo_exists():
            return
        changed = (
            self.settings_snapshot is not None and self.settings_values() != self.settings_snapshot
        )
        if changed and not self.apply_row.winfo_manager():
            self.apply_row.pack(fill="x", pady=(12, 0))
        elif not changed and self.apply_row.winfo_manager():
            self.apply_row.pack_forget()

    def reset_dictation_settings(self) -> None:
        """Set a damaged settings file aside (kept, not deleted) and start from defaults."""
        config = self.service.paths.config
        try:
            os.replace(config, config.with_name(config.name + ".bak"))
        except OSError as exc:
            self.status.set(f"Couldn’t reset the settings: {exc.strerror or exc}")
            return
        self.settings()
        self.status.set("Settings reset. The old file was kept as config.json.bak.")

    def show_choice(self) -> None:
        # Only the selected option shows its details, keeping the card short.
        for value, (frame, anchor) in self.choices.items():
            if value == self.model_source.get() and value != "download":
                frame.pack(fill="x", padx=(26, 0), pady=(6, 0), after=anchor)
            else:
                frame.pack_forget()

    def choose_provider(self) -> None:
        url, model = PROVIDERS[self.provider.get()]
        self.endpoint.set(url)
        self.api_model.set(model)

    def show_model(self) -> None:
        path = self.model.get()
        self.model_label.configure(text=Path(path).name if path else "No file chosen")

    def choose_model(self) -> None:
        path = filedialog.askopenfilename(
            parent=self.root,
            title="Choose a whisper.cpp model",
            filetypes=[("Whisper GGML model", "*.bin")],
        )
        if path:
            self.model.set(path)
            self.model_source.set("file")
            self.show_model()

    def find_microphones(self) -> None:
        if self.page != "settings":
            return
        self.status.set("Looking for microphones…")
        self.background(self.service.microphones, self.show_microphones)

    def set_option(self, key: str, value: tk.BooleanVar) -> None:
        self.service.set_option(key, bool(value.get()))
        telemetry.event("setting_changed", setting=key, on=bool(value.get()))
        self.saved("Settings saved. It applies to your next recording.")

    def test_microphone(self) -> None:
        """Listen for three seconds with a live meter, then say how it sounded."""
        if self.mic_test is not None:
            return
        device = self.device_ids.get(self.device.get(), self.device.get())
        try:
            self.mic_test = MicrophoneTest(self.service.paths, device)
        except d.DictationError as exc:
            self.status.set(str(exc))
            return
        self.mic_button.state(["disabled"])
        self.meter.pack(fill="x", pady=(8, 0))
        self.status.set("Say something…")
        self.meter_tick()

    def meter_tick(self) -> None:
        test = self.mic_test
        if test is None:
            return
        if not self.meter.winfo_exists():
            test.stop()  # The page changed mid-test.
            self.mic_test = None
            return
        width = max(1, self.meter.winfo_width())
        self.meter.delete("all")
        self.meter.create_rectangle(0, 2, width, 8, fill=BORDER, outline="")
        filled = int(width * test.level)
        color = ACCENT if test.level > 0.35 else WARNING if test.level > 0.1 else IDLE
        self.meter.create_rectangle(0, 2, filled, 8, fill=color, outline="")
        peak = int(width * test.peak)
        self.meter.create_line(peak, 0, peak, 10, fill=TEXT)
        if not test.done():
            self.root.after(50, self.meter_tick)
            return
        test.stop()
        self.mic_test = None
        self.mic_button.state(["!disabled"])
        self.status.set(test.verdict())
        telemetry.event("mic_test", result=test.outcome())

    def background(
        self, work: Callable[[], Any], done: Callable[[Any], None]
    ) -> concurrent.futures.Future[Any]:
        """Run `work` off the UI thread; `poll` passes its result to `done`. Nothing locks."""
        future = self.helper.submit(work)
        self.lookups.append((future, done))
        self.watch_lookups()
        return future

    def show_microphones(self, devices: list[str]) -> None:
        if self.page != "settings" or not self.device_picker.winfo_exists():
            return  # The user moved on while we were looking.
        if not devices:
            self.status.set("No microphones found. Connect a microphone and choose Refresh.")
            return
        names = getattr(self.service, "microphone_names", {})
        # Friendly names in the list; `prepare` saves the device they stand for.
        self.device_ids = {names.get(device, device): device for device in devices}
        self.device_picker.configure(values=list(self.device_ids))
        current = self.device_ids.get(self.device.get(), self.device.get())
        if current not in devices:
            # Never silently pick an arbitrary device: the system default is safest.
            current = "default" if "default" in devices else devices[0]
        self.device.set(names.get(current, current))
        found = f"{len(devices)} microphone{'s' if len(devices) != 1 else ''} found."
        if self.pending is None:
            self.status.set(f"{found} Pick the one you’ll speak into.")
        if self.auto_prepare_recommended:
            self.prepare_recommended_setup()

    def prepare(self) -> None:
        language = app_settings.language_code(self.language.get())
        device = self.device_ids.get(self.device.get(), self.device.get())
        source = self.model_source.get()
        model = self.model.get() if source == "file" else ""
        if source == "file" and not model:
            self.status.set("Choose a model file, or select the recommended download.")
            return
        remote = None
        if source == "service":
            if not self.endpoint.get().strip():
                self.status.set("Enter your transcription service URL.")
                return
            remote = Remote(self.endpoint.get(), self.api_model.get(), self.api_key.get())
        self.download = (0, 0)
        self.download_pause.clear()

        def report(done: int, total: int) -> None:
            self.download = (done, total)

        setup = not self.service.completed()
        began = time.monotonic()

        def done(_: object) -> None:
            if self.pause_download is not None and self.pause_download.winfo_exists():
                self.pause_download.pack_forget()
            telemetry.event(
                "dictation_setup",
                source=source,
                language=language,
                first_run=setup,
                seconds=round(time.monotonic() - began),
            )
            if setup:
                self.after_dictation_setup()
            else:
                self.saved("Transcription AI applied.")  # Stay here: no walkthrough again.
                if self.page == "settings":
                    self.settings_snapshot = self.settings_values()
                    self.show_apply()

        if source == "download":
            if self.pause_download is None or not self.pause_download.winfo_exists():
                self.pause_download = ttk.Button(self.actions(), text="Pause download")
            self.pause_download.configure(text="Pause download", command=self.pause_model)
            self.pause_download.pack(side="left")
        self.submit(
            lambda: self.service.prepare(
                language, device, model, report, remote, self.download_pause
            ),
            done,
            "Checking your settings. Nothing is recording."
            if remote
            else "Getting your speech model ready. Nothing is recording.",
        )

    def pause_model(self) -> None:
        self.download_pause.set()
        if self.pause_download is not None:
            self.pause_download.configure(text="Resume download", command=self.prepare)
        self.status.set("Pausing download. Downloaded data is kept; choose Resume to continue.")

    def tutorial(self) -> None:
        if not self.features().dictation:
            self.tutorial_clipboard()
            return
        self.reset(
            "tutorial",
            "Your first dictation" if not self.service.completed() else "How Dictation works",
            "Follow these steps once; then use the shortcut anywhere.",
            self.step_label("done"),
        )
        paste = "Command + V" if desktop.platform_name() == "macos" else "Ctrl + V"
        place = {"macos": "menu bar", "windows": "system tray"}.get(
            desktop.platform_name(), "top bar"
        )
        shortcut = hotkeys.Preferences(self.service.paths).shortcut().label()
        auto_paste = (d.DEFAULTS | d.read_json(self.service.paths.config)).get("auto_paste") is True
        body = self.card()
        self.steps(
            body,
            [
                (
                    f"Press {shortcut} in any app to start speaking",
                    "A small bar near the top of your screen shows it’s listening; "
                    f"the icon in the {place} turns red.",
                ),
                (
                    f"Press {shortcut} again to stop",
                    "The bar shows “Transcribing…”, then “Pasted” or “Copied” when your "
                    "text is ready.",
                ),
                (
                    "Your words are ready to paste",
                    f"They’re also copied, so {paste} pastes them anywhere else.",
                )
                if auto_paste
                else ("Paste anywhere", f"Your words are already copied. Press {paste}."),
            ],
        )
        if self.features().dictation:
            check = self.card(
                "Try your shortcut", "Choose Test shortcut, then press the keys shown."
            )
            panel = shortcut_panel.TestPanel(
                self.root, self.service.paths, check, "dictation", self.wraplength - 50
            )
            self.shortcut_panels.append(panel)
            self.button("Test shortcut", panel.start, parent=check)
            self.button(
                "Choose a shortcut",
                lambda: self.shortcut_page(back=self.tutorial),
                parent=check,
            )
            panel.frame.pack(anchor="w", pady=(4, 0))
            if not self.service.completed():
                sample = self.card(
                    "Try a recording",
                    "Choose Record, say the phrase below, then choose Stop and transcribe to see it turn into text.",
                )
                ttk.Label(
                    sample,
                    text="“Hello world. New paragraph. This is my first dictation.”",
                    style="Card.TLabel",
                    wraplength=self.wraplength - 50,
                ).pack(anchor="w", pady=(4, 8))
                self.button(
                    "Open recorder",
                    lambda: self.finish_setup(try_recording=True),
                    primary=True,
                    parent=sample,
                )
        tips = self.card("Change settings anytime")
        ttk.Label(
            tips,
            text="Change the shortcut, microphone, or AI anytime in Settings, from this "
            f"window or the {place} icon."
            + (
                " Not on GNOME? Assign the shortcut to ~/.local/bin/dictate-toggle --via-shortcut in your"
                " keyboard settings."
                if desktop.platform_name() == "linux"
                else ""
            ),
            style="CardHint.TLabel",
            wraplength=self.wraplength - 50,
        ).pack(anchor="w", pady=(10, 0))
        if self.service.completed():  # "How it works" from the Dictation tab.
            self.button("Back", self.home, True, self.actions(), "right")
            return
        self.button(
            "Done" if self.tray else "Start dictating",
            self.finish_setup,
            True,
            self.actions(),
            "right",
        )
        self.button("Back", self.choose_features, parent=self.actions(), side="left")

    def clipboard_plus_card(self) -> None:
        """The account card, where the clipboard history is (or an old key still lives)."""
        self.account = None
        if self.features().clipboard or self.service.clipboard_plus_linked():
            self.account = clipui.AccountCard(self)

    def sharing_usage(self) -> bool:
        """Whether the user has agreed to share reports (the consent API when this build has one)."""
        consented = getattr(telemetry, "has_consent", None)
        if consented is not None:
            return bool(consented())
        return hotkeys.Preferences(self.service.paths).share_usage()

    def finish_setup(self, *, try_recording: bool = False) -> None:
        features = self.features()
        telemetry.event(
            "setup_complete",
            mode=self.setup_mode or "both",
            dictation=features.dictation,
            clipboard=features.clipboard,
            share_usage=self.sharing_usage(),
        )
        self.submit(
            self.service.complete,
            lambda _: self.start_first_recording() if try_recording else self.leave(),
            "Saving your setup…",
        )

    def start_first_recording(self) -> None:
        """Open the recorder so the user can verify a real transcription after setup."""
        self.home()
        self.status.set(
            "Choose Record, say “Hello world. New paragraph. This is my first dictation,” then choose Stop."
        )
        self.record.focus_set()
        self.bring_forward()

    def leave(self) -> None:
        if self.tray:
            self.destroy()
        else:
            self.home()

    def shortcut_page(
        self, back: Callable[[], None] | None = None, kind: str = "dictation"
    ) -> None:
        """Record a new shortcut, for dictation or the clipboard history. `back` returns to
        a page; otherwise the window closes."""
        self.shortcut_back = back
        self.shortcut_kind = kind
        history = kind == "history"
        self.reset(
            "shortcut",
            "Choose your clipboard history shortcut" if history else "Choose your shortcut",
            (
                "Hold ⌃, ⌥ or ⌘ and press a letter, number or Space — "
                if desktop.platform_name() == "macos"
                else "Hold Ctrl, Alt or Win/Super and press a letter, number or Space — "
            )
            + "or press a function key (F1–F12). Enter saves, Esc cancels.",
        )
        self.end_capture()  # A flag left from the other kind.
        self.capture = self.service.paths.runtime / hotkeys.CAPTURE_FLAGS[kind]
        d.private_dir(self.capture.parent)
        d.atomic(self.capture, "capturing")  # Pauses the tray's current shortcuts.
        self.held: set[str] = set()
        self.captured: hotkeys.Shortcut | None = None
        features = self.features()
        prefs = hotkeys.Preferences(self.service.paths)
        current = prefs.history_shortcut() if history else prefs.shortcut()
        default = (
            hotkeys.default_history_shortcut(desktop.platform_name())
            if history
            else (hotkeys.default_shortcut(desktop.platform_name()))
        )
        # The other feature's shortcut: the same keys can't do both jobs.
        other_kind = "dictation" if history else "history"
        other = (
            (prefs.shortcut() if features.dictation else None)
            if history
            else (prefs.history_shortcut() if features.clipboard else None)
        )
        self.shortcut_other = (other, other_kind)
        body = self.card()
        self.shortcut_label = ttk.Label(
            body,
            text=current.label() if current else "Off",
            style="Card.TLabel",
            font=self.fonts["title"],
        )
        self.shortcut_label.pack(anchor="w")
        self.shortcut_hint = ttk.Label(
            body,
            text="Press the keys now, or pick one below.",
            style="CardHint.TLabel",
            wraplength=self.wraplength,
        )
        self.shortcut_hint.pack(anchor="w", pady=(6, 0))
        ttk.Label(
            body,
            text="Your current shortcut is paused while you choose. After saving you can test "
            "it right here: press it and this page confirms it was heard."
            + (
                f" {default.label()} is also used by some browsers and editors, which it "
                "overrides while it is on; pick another below if that gets in the way."
                if desktop.platform_name() == "macos"
                else ""
            ),
            style="CardHint.TLabel",
            wraplength=self.wraplength,
        ).pack(anchor="w", pady=(6, 0))
        picks = ttk.Frame(body, style="Card.TFrame")
        picks.pack(fill="x", pady=(10, 0))
        platform = desktop.platform_name()
        for preset in hotkeys.history_presets(platform) if history else hotkeys.presets(platform):
            ttk.Button(
                picks,
                text=preset.label(),
                command=functools.partial(self.consider_shortcut, preset),
            ).pack(side="left", padx=(0, 6))
        self.save_shortcut_button = self.button(
            "Save", self.save_shortcut, True, self.actions(), "right"
        )
        self.save_shortcut_button.state(["disabled"])
        self.button("Cancel", self.shortcut_done, parent=self.actions(), side="right")
        if history:
            self.button("Turn off", self.turn_off_shortcut, parent=self.actions(), side="left")
        self.root.bind("<KeyPress>", self.shortcut_key)
        self.root.bind("<KeyRelease>", self.shortcut_release)
        self.root.focus_force()

    @staticmethod
    def event_number(event: tk.Event[Any], name: str) -> int:
        value = getattr(event, name, 0)
        return value if isinstance(value, int) else 0

    def shortcut_key(self, event: tk.Event[Any]) -> str | None:
        state = self.event_number(event, "state")
        if not self.held and not hotkeys.modifiers_from_state(state):
            # Bare Tab, Esc and Enter can't be shortcuts; they move, cancel and save.
            if event.keysym in ("Tab", "ISO_Left_Tab"):
                return None
            if event.keysym == "Escape":
                self.shortcut_done()
                return "break"
            if event.keysym in ("Return", "KP_Enter"):
                if self.captured is not None:
                    self.save_shortcut()
                return "break"
        modifier = hotkeys.tk_modifier(event.keysym)
        if modifier:
            self.held.add(modifier)
            return "break"
        shortcut = hotkeys.from_tk(
            event.keysym, self.held, state, self.event_number(event, "keycode")
        )
        if shortcut is not None:
            self.consider_shortcut(shortcut)
        return "break"

    def consider_shortcut(self, shortcut: hotkeys.Shortcut) -> None:
        """Show a recorded or picked shortcut and allow saving it if it can work."""
        other, other_kind = self.shortcut_other
        problem = hotkeys.choice_problem(shortcut, other, other_kind)
        self.shortcut_label.configure(text=shortcut.label())
        self.shortcut_hint.configure(text=problem or "Press Save to use this shortcut.")
        self.captured = None if problem else shortcut
        self.save_shortcut_button.state(["disabled"] if problem else ["!disabled"])

    def shortcut_release(self, event: tk.Event[Any]) -> None:
        self.held.discard(hotkeys.tk_modifier(event.keysym))

    def save_shortcut(self) -> None:
        saved = self.captured
        if saved is None:
            self.shortcut_done()
            return
        prefs = hotkeys.Preferences(self.service.paths)
        if self.shortcut_kind == "history":
            prefs.save(history_shortcut=saved)
        else:
            prefs.save(shortcut=saved)
        self.shortcut_test_page(saved)

    def shortcut_test_page(self, saved: hotkeys.Shortcut) -> None:
        """After Save: stay here and ask for a press, so it is known to work before leaving."""
        kind = self.shortcut_kind
        self.end_capture()  # The tray or menu bar registers the new shortcut now.
        back = self.shortcut_back
        self.reset("shortcut", "Test your shortcut", "Saved. Now make sure it works.")
        self.shortcut_back = back
        self.shortcut_kind = kind
        card = self.card()
        ttk.Label(card, text=saved.label(), style="Card.TLabel", font=self.fonts["title"]).pack(
            anchor="w"
        )
        panel = shortcut_panel.TestPanel(
            self.root, self.service.paths, card, kind, self.wraplength - 50
        )
        self.shortcut_panels.append(panel)
        panel.frame.pack(anchor="w", pady=(8, 0))
        panel.start(fresh=True)
        self.button("Done", lambda: self.shortcut_test_done(saved), True, self.actions(), "right")

    def shortcut_test_done(self, saved: hotkeys.Shortcut) -> None:
        kind = self.shortcut_kind
        self.shortcut_done()
        if self.page != "closed":
            name = "Clipboard history" if kind == "history" else "Dictation"
            self.status.set(f"{name} shortcut: {saved.label()}.")

    def turn_off_shortcut(self) -> None:
        """Switch the clipboard history shortcut off (dictation always has one)."""
        hotkeys.Preferences(self.service.paths).save(history_shortcut=False)
        self.shortcut_done()
        if self.page != "closed":
            self.status.set("The clipboard history shortcut is off.")

    def shortcut_done(self) -> None:
        back = getattr(self, "shortcut_back", None)
        if back is None:
            self.destroy()
        else:
            back()  # Leaving the page ends the capture.

    def home(self) -> None:
        paths = self.service.paths
        chosen = hotkeys.Preferences(paths).shortcut()
        shortcut = chosen.label()
        conflict = hotkeys.shortcut_conflict(paths)
        working = hotkeys.shortcut_working(paths)
        outcome = hotkeys.shortcut_outcome(paths, "dictation", chosen)
        title, subtitle = app_settings.home_text(
            shortcut,
            conflict,
            working,
            desktop.platform_name(),
            hotkeys.shortcut_message(paths),
            outcome,
        )
        self.reset("home", title, subtitle)
        self.home_fixes(shortcut, conflict, title)
        if working and outcome != "heard" and self.features().dictation:
            self.home_test()
        self.home_record_controls()
        self.home_status_line()
        self.home_transcript_actions()

    def home_fixes(self, shortcut: str, conflict: hotkeys.Conflict | None, title: str) -> None:
        """Buttons that fix a shortcut that isn't working."""
        if conflict is None and title == f"{shortcut} is taken":
            fixes = ttk.Frame(self.frame)
            fixes.pack(fill="x", pady=(0, 12))
            self.button(
                "Choose another shortcut",
                lambda: self.shortcut_page(back=self.home),
                True,
                fixes,
                "left",
            )
        if conflict is not None:
            fixes = ttk.Frame(self.frame)
            fixes.pack(fill="x", pady=(0, 12))
            if conflict.path:
                self.button(
                    f"Use {shortcut} for dictation",
                    lambda: self.take_over(conflict),
                    True,
                    fixes,
                    "left",
                )
            self.button(
                "Choose another shortcut",
                lambda: self.shortcut_page(back=self.home),
                parent=fixes,
                side="left",
            )

    def home_test(self) -> None:
        """ "Test your shortcut", for one that has not been heard yet."""
        panel = shortcut_panel.TestPanel(
            self.root, self.service.paths, self.frame, "dictation", self.wraplength
        )
        self.shortcut_panels.append(panel)
        fixes = ttk.Frame(self.frame)
        fixes.pack(fill="x", pady=(0, 6))
        self.button("Test your shortcut", panel.start, parent=fixes, side="left")
        panel.frame.pack(anchor="w", pady=(0, 12))

    def home_record_controls(self) -> None:
        """The optional Record and Cancel buttons."""
        ttk.Label(
            self.frame,
            text="Optional: record from here instead",
            style="Hint.TLabel",
            wraplength=self.wraplength,
        ).pack(anchor="w", pady=(0, 8))
        controls = ttk.Frame(self.frame)
        controls.pack(fill="x")
        self.record = ttk.Button(
            controls,
            text="Record",
            command=lambda: self.action("toggle"),
            style="Record.TButton",
        )
        self.record.pack(side="left", fill="x", expand=True)
        self.cancel = ttk.Button(
            controls, text="Cancel recording", command=lambda: self.action("cancel")
        )
        self.cancel.pack(side="left", fill="y", padx=(10, 0))
        self.buttons.extend([self.record, self.cancel])

    def home_status_line(self) -> None:
        """The status dot and text, then the transcript box."""
        line = ttk.Frame(self.frame)
        line.pack(fill="x", pady=(14, 8))
        self.indicator = tk.Canvas(
            line, width=10, height=10, background=BACKGROUND, highlightthickness=0
        )
        self.dot = self.indicator.create_oval(1, 1, 9, 9, fill=IDLE, outline="")
        self.indicator.pack(side="left", padx=(0, 8))
        ttk.Label(
            line, textvariable=self.status, style="Hint.TLabel", wraplength=self.wraplength
        ).pack(side="left", fill="x")
        outline, body = self.panel(self.frame, padding=(6, 6) if self.modern else 0)
        outline.pack(fill="both", expand=True, pady=(4, 10))
        self.transcript = tk.Text(
            body,
            height=6,
            wrap="word",
            font=self.fonts["body"],
            background=SURFACE,
            foreground=TEXT,
            relief="flat",
            highlightthickness=0,
            padx=16,
            pady=14,
            spacing2=3,
        )
        self.transcript_seen = ()  # A new text box: read the transcript again.
        self.transcript.tag_configure("placeholder", foreground=IDLE)
        self.transcript.pack(fill="both", expand=True)
        self.show_transcript("")

    def home_transcript_actions(self) -> None:
        """Copy, Make concise, Retry and Discard, shown only when they apply."""
        row = ttk.Frame(self.frame)
        row.pack(fill="x")
        self.copy = ttk.Button(row, text="Copy transcript", command=lambda: self.action("copy"))
        self.copy.pack(side="left")
        self.concise = ttk.Button(row, text="Make concise", command=self.make_concise)
        self.concise.pack(side="left", padx=(8, 0))
        self.retry = ttk.Button(
            row, text="Retry saved recording", command=lambda: self.action("transcribe")
        )
        self.retry.pack(side="right")
        self.discard = ttk.Button(row, text="Discard", command=self.discard_audio)
        self.discard.pack(side="right", padx=(0, 8))
        self.buttons.extend([self.copy, self.concise, self.retry, self.discard])
        # Shown only when they apply: Cancel while recording, Retry/Discard for saved audio.
        # (Retry is shown before Discard, so Discard lands to its left as designed.)
        self.situational: dict[ttk.Button, Callable[[], None]] = {
            self.cancel: lambda: self.cancel.pack(side="left", fill="y", padx=(10, 0)),
            self.retry: lambda: self.retry.pack(side="right"),
            self.discard: lambda: self.discard.pack(side="right", padx=(0, 8)),
        }
        for button in self.situational:
            button.pack_forget()
        self.help_button = self.button(
            "How it works", self.tutorial, parent=self.bar_actions, side="right"
        )

    def take_over(
        self,
        conflict: hotkeys.Conflict,
        status: str = "shortcut-status",
        redraw: Callable[[], None] | None = None,
    ) -> None:
        """Unbind the other custom shortcut (kept in the keyboard settings) so ours works."""
        released = hotkeys.gnome_release(conflict.path)
        if released:
            # GNOME gave the keys to the other binding; ours must grab them again.
            hotkeys.gnome_regrab(
                hotkeys.GNOME_HISTORY_PATH
                if status == "history-shortcut-status"
                else hotkeys.GNOME_PATH
            )
        telemetry.event("shortcut_take_over", ok=released, which=status)
        if released:
            hotkeys.record_status(self.service.paths, True, status)
            (redraw or self.home)()
            self.status.set(f"Done. The shortcut belongs to {hotkeys.APP_NAME} now.")
        else:
            self.status.set("Couldn’t change the keyboard settings. Try your desktop’s settings.")

    def situate(self, button: ttk.Button, wanted: bool) -> None:
        """Show or hide one of the Dictation tab's situational buttons."""
        if wanted and not button.winfo_manager():
            self.situational[button]()
        elif not wanted and button.winfo_manager():
            button.pack_forget()

    def show_transcript(self, text: str) -> None:
        self.transcript.configure(state="normal")
        self.transcript.delete("1.0", "end")
        if text:
            self.transcript.insert("1.0", text)
        else:
            self.transcript.insert("1.0", "Your transcript will appear here.", "placeholder")
        self.transcript.configure(state="disabled")
        self.last_text = text

    def action(self, action: str) -> None:
        self.submit(
            lambda: self.service.action(action),
            lambda _: self.status.set("Copied to clipboard." if action == "copy" else ""),
            "Working…",
        )

    def make_concise(self) -> None:
        config = d.Config(self.service.paths)
        if not config.s("rewrite_endpoint") or not config.s("rewrite_model"):
            rewriteui.SettingsDialog(
                self.root,
                config,
                self.service.paths,
                self.make_concise,
                make_after_save=True,
            )
            return

        def show_draft(text: object) -> None:
            clipui.TextPreview(
                self.root,
                "Concise draft — review before copying",
                str(text),
                lambda: self.action("copy-concise"),
            )

        self.submit(
            self.service.concise,
            show_draft,
            "Making a concise draft… Your original transcript is kept.",
        )

    def copy_text(self, text: str) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(text)

    def discard_audio(self) -> None:
        if messagebox.askyesno(
            "Discard recording?",
            "Delete the saved audio? Your last transcript will be kept.",
            parent=self.root,
        ):
            self.action("discard")

    def submit(self, work: Callable[[], Any], done: Callable[[Any], None], message: str) -> None:
        if self.pending is not None:
            return
        self.status.set(message)
        for button in self.buttons:
            button.state(["disabled"])
        self.progress.configure(mode="indeterminate", value=0)
        if self.status_label.winfo_manager():
            self.progress.pack(fill="x", pady=(2, 8), before=self.status_label)
        else:
            self.progress.pack(fill="x", pady=(2, 8))
        self.progress.start(15)
        self.update_bottom()
        self.done = done
        self.pending = self.executor.submit(work)

    def show_download(self) -> None:
        done, total = self.download
        if not done:
            return
        megabytes = f"{done / 1e6:.0f} MB"
        if total:
            if str(self.progress.cget("mode")) != "determinate":
                self.progress.stop()
                self.progress.configure(mode="determinate", maximum=total)
            self.progress.configure(value=done)
            megabytes = f"{megabytes} of {total / 1e6:.0f} MB ({100 * done // total}%)"
        self.status.set(f"Downloading the speech model · {megabytes}")

    def refresh(self) -> None:
        current = workflow.snapshot(self.service.paths)
        active = bool(current["active"])
        phase = str(current["phase"])
        recording = active and phase == "recording"
        retained = current["retained_audio"]
        message = workflow.display_text(str(current.get("message", ""))).strip()
        label = "Stop and transcribe" if recording else "Record"
        if str(self.record.cget("text")) != label:  # Restyling every tick flickers.
            self.record.configure(
                text=label,
                style="Record.Danger.TButton" if recording else "Record.TButton",
            )
        self.record.state(
            ["!disabled"] if recording or (not active and not retained) else ["disabled"]
        )
        self.cancel.state(["!disabled"] if recording else ["disabled"])
        self.situate(self.cancel, recording)
        for button in (self.retry, self.discard):
            button.state(["!disabled"] if retained and not active else ["disabled"])
            self.situate(button, bool(retained) and not active)
        self.help_button.state(["disabled"] if active else ["!disabled"])
        self.copy.state(["!disabled"] if self.service.paths.text.exists() else ["disabled"])
        self.concise.state(["!disabled"] if self.service.paths.text.exists() else ["disabled"])
        color = ACCENT
        if active:
            color = DANGER if recording else WARNING
            self.status.set(
                f"{'Recording' if recording else 'Transcribing'} · {int(current.get('elapsed_seconds', 0))}s"
                + (f" — {message}" if message else "")
            )
        elif phase in ("error", "interrupted"):
            color = DANGER
            self.status.set(
                message
                or "The session stopped unexpectedly. Retry the saved recording or check Settings."
            )
        elif retained:
            color = WARNING
            self.status.set("A recording is saved. Retry or discard it before starting another.")
        elif message:
            self.status.set(message)
        elif not self.status.get() or self.status.get().startswith(
            ("Recording", "Transcribing", "Working")
        ):
            self.status.set(
                "Ready. Your last transcript is shown below."
                if self.service.paths.text.exists()
                else "Ready to record."
            )
        self.indicator.itemconfigure(self.dot, fill=color)
        source = (
            self.service.paths.preview
            if active and self.service.paths.preview.exists()
            else self.service.paths.text
        )
        try:
            info = source.stat()
            seen: tuple[Any, ...] = (source, info.st_mtime_ns, info.st_size)
        except OSError:
            seen = (source, 0, -1)
        if seen != self.transcript_seen:  # Read the file only when it changed.
            self.transcript_seen = seen
            text = source.read_text(encoding="utf-8") if seen[2] >= 0 else ""
            if text != self.last_text:
                self.show_transcript(text)

    def open_page(self, request: str) -> None:
        """Honor a page request from the tray or a shortcut when it is safe to leave."""
        clear_history = request == "clipboard-clear"
        if clear_history:
            request = "clipboard"
        if request == "clipboard":
            self.quick = True  # Opened to pick something: Esc closes the window.
        if request == "clipboard" and self.page == "clipboard" and self.clipboard_page:
            self.clipboard_page.focus_search()
            if clear_history:
                self.root.after_idle(self.clear_clipboard_history)
            return
        if self.pending is not None or self.page == request:
            return
        if request == "clipboard" and self.service.completed() and self.features().clipboard:
            if clear_history:
                self.clipboard_clear_after_open = True
            self.clipboard()
            if clear_history and self.clipboard_page is not None:
                self.clipboard_clear_after_open = False
                self.root.after_idle(self.clear_clipboard_history)
        elif request == "shortcut" and not d.busy(self.service.paths):
            self.shortcut_page()
        elif request == "history-shortcut" and not d.busy(self.service.paths):
            self.shortcut_page(kind="history")
        elif request == "settings" and self.service.completed() and not d.busy(self.service.paths):
            self.settings()

    def clear_clipboard_history(self) -> None:
        """Show the existing confirmation only while Clipboard is available."""
        if self.features().clipboard and self.page == "clipboard" and self.clipboard_page:
            self.clipboard_page.clear()

    def on_key(self, action: Callable[[], None], _event: object) -> str:
        action()
        return "break"

    def can_navigate(self) -> bool:
        """Whether a keyboard shortcut may switch pages now (not mid-setup or mid-task)."""
        return (
            self.pending is None
            and self.page not in ("shortcut", "closed")
            and self.service.completed()
            and not d.busy(self.service.paths)
        )

    def find(self) -> None:
        """Ctrl/⌘+F: search the clipboard history from any page."""
        if not self.features().clipboard or not self.can_navigate() or not self.confirm_leave():
            return
        if self.page != "clipboard" or self.clipboard_page is None:
            self.clipboard()
        if self.clipboard_page is not None:
            self.clipboard_page.focus_search()

    def go_settings(self) -> None:
        if self.page != "settings" and self.can_navigate():
            self.settings()

    def bring_forward(self) -> None:
        """Show the window above others and give it the keyboard (a shortcut opened it)."""
        self.root.update_idletasks()  # Lay the page out before it is mapped, so it never resizes on screen.
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)  # Otherwise many desktops only flash it.
        self.root.after_idle(lambda: self.root.attributes("-topmost", False))
        self.root.focus_force()

    def deliver_lookups(self) -> None:
        """Hand each finished background lookup to the code waiting for it."""
        for lookup in [entry for entry in self.lookups if entry[0].done()]:
            self.lookups.remove(lookup)
            future, finished = lookup
            if future.cancelled():
                continue
            finished(future.result())  # A failure is reported by the caller.

    def watch_lookups(self) -> None:
        """Check for finished lookups every few milliseconds while any are running.

        The slow poll below would otherwise hold every result back by up to half a
        second: a search would answer a beat after the typing, and a page would sit on
        its "loading" card long after its data had arrived.
        """
        if self.lookup_timer is None and self.lookups and self.page != "closed":
            self.lookup_timer = self.root.after(LOOKUP_MS, self.drain_lookups)

    def drain_lookups(self) -> None:
        self.lookup_timer = None
        try:
            self.deliver_lookups()
        except Exception as exc:  # noqa: BLE001 - one failed callback must not stop the rest.
            self.report_poll_failure(exc)
        self.watch_lookups()

    def report_poll_failure(self, exc: Exception) -> None:
        if isinstance(exc, d.DictationError):  # Those are explained on screen.
            self.status.set(str(exc))
        elif isinstance(exc, (OSError, ValueError, subprocess.SubprocessError)):
            telemetry.capture(exc, page=self.page)
            self.status.set(
                "Something went wrong. Check microphone permissions, connections, and free disk space, then retry."
            )
        else:
            log.exception("window update failed")
            telemetry.capture(exc, page=self.page)
            self.status.set("Something went wrong. Please try again.")

    def poll(self) -> None:
        try:
            activation = self.service.paths.runtime / "show-window"
            if activation.exists():
                request = activation.read_text(encoding="utf-8")
                activation.unlink()
                self.bring_forward()
                self.open_page(request)
            self.deliver_lookups()
            if self.pending is not None and not self.pending.done():
                self.show_download()
            if self.pending is not None and self.pending.done():
                future, self.pending = self.pending, None
                self.download = (0, 0)
                self.progress.stop()
                self.progress.pack_forget()
                self.update_bottom()
                for button in self.buttons:
                    button.state(["!disabled"])
                done, self.done = self.done, lambda _: None
                if self.closing:
                    self.destroy()
                    return
                done(future.result())
            if self.page == "home" and self.pending is None:
                self.refresh()
            if self.account is not None and self.pending is None:
                self.account_polls = getattr(self, "account_polls", 0) + 1
                if self.account_polls % 10 == 0:  # About every two seconds.
                    self.account.refresh()
            if self.page == "clipboard" and self.clipboard_page is not None:
                self.polls = getattr(self, "polls", 0) + 1
                if self.polls % 5 == 0:  # About once a second.
                    self.clipboard_page.refresh()
            self.refresh_update_result()
        except Exception as exc:  # noqa: BLE001 - never let one failure stop the window updating.
            self.report_poll_failure(exc)
        if self.page != "closed":
            # Half a second is plenty when idle; recording screens update their own
            # state file in the meantime, and account/clipboard refresh still lands
            # within about a second.
            self.timer = self.root.after(500 if self.pending is None else 200, self.poll)

    def close(self) -> None:
        """Close the window. A recording runs on its own and is not touched."""
        if not self.confirm_leave():
            return
        if self.pending is None:
            self.destroy()
            return
        downloading = (
            self.pause_download is not None
            and self.pause_download.winfo_exists()
            and bool(self.pause_download.winfo_manager())
            and not self.download_pause.is_set()
        )
        if not downloading:
            messagebox.showinfo(
                "Please wait",
                "An operation is finishing. Keep this window open until it completes.",
                parent=self.root,
            )
        elif messagebox.askokcancel(
            "Pause the download and close?",
            "What’s downloaded so far is kept. Open Settings later to finish it.",
            parent=self.root,
        ):
            self.pause_model()
            self.closing = True  # Closes once the download has stopped.

    def end_capture(self) -> None:
        """Stop recording keys for a new shortcut; the tray re-enables the shortcuts."""
        for name in hotkeys.CAPTURE_FLAGS.values():
            (self.service.paths.runtime / name).unlink(missing_ok=True)
        self.root.unbind("<KeyPress>")
        self.root.unbind("<KeyRelease>")

    def _close_pending_clipboard_open(self) -> None:
        opening = self.clipboard_open_future
        self.clipboard_open_future = None
        if opening is None:
            return

        def close_opened_store(future: concurrent.futures.Future[Any]) -> None:
            try:
                store, _ = future.result()
            except Exception:
                return
            if store is not None:
                store.close()

        opening.add_done_callback(close_opened_store)

    def destroy(self) -> None:
        if self.root.winfo_viewable():
            self.save_size()
        self.page = "closed"
        for panel in self.shortcut_panels:
            panel.cancel()
        self.end_capture()
        self.root.after_cancel(self.timer)
        if self.toast_after is not None:
            self.root.after_cancel(self.toast_after)
        for timer in (self.rewrap_timer, self.bottom_timer):
            if timer is not None:
                self.root.after_cancel(timer)
        self.done = lambda _: None
        self.executor.shutdown(wait=True)
        self.helper.shutdown(wait=False, cancel_futures=True)  # Lookups only; nothing to save.
        self._close_pending_clipboard_open()
        if self.clipboard_store is not None:
            active_queries = [future for future in self.clipboard_queries if not future.done()]
            if active_queries:
                store = self.clipboard_store
                lock = threading.Lock()
                remaining = len(active_queries)

                def close_store(_: concurrent.futures.Future[Any]) -> None:
                    nonlocal remaining
                    with lock:
                        remaining -= 1
                        if remaining == 0:
                            store.close()

                for future in active_queries:
                    future.add_done_callback(close_store)
            else:
                self.clipboard_store.close()
        # Idle callbacks still queued would run against destroyed widgets.
        for pending in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            # Tk 9/Python 3.14 can return each `after info` entry as a tuple
            # containing the script and its id. Call Tcl directly: tkinter's
            # after_cancel assumes the script is a string and fails on Tk 9 tuples.
            timer = pending[0] if isinstance(pending, (tuple, list)) else pending
            self.root.tk.call("after", "cancel", timer)
        self.root.destroy()


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    args = sys.argv[1:] if argv is None else argv
    page = next(
        (
            flag[2:]
            for flag in (
                "--settings",
                "--shortcut",
                "--history-shortcut",
                "--clipboard-clear",
                "--clipboard",
            )
            if flag in args
        ),
        "",
    )
    paths = d.Paths()
    if hotkeys.VIA_SHORTCUT in args and page == "clipboard":
        hotkeys.acknowledge(paths, "history")  # The desktop ran us because the keys were pressed.
    fd = desktop.lock(paths.runtime / "app.lock")
    if fd is None:
        # The running window shows itself, on the requested page if any.
        d.atomic(paths.runtime / "show-window", page or "show")
        return 0
    telemetry.install("window")
    try:
        set_macos_app_identity()
        root = tk.Tk(className="ClipboardPlus")

        telemetry.watch_tk(root)
        telemetry.event("app_open", page=page or "home")
        picker = page in ("clipboard", "clipboard-clear")
        if picker:
            root.withdraw()  # Shown once, raised and focused, instead of mapped then moved.
        window = App(root, Service(paths), page)
        if picker:
            root.after_idle(window.bring_forward)  # Opened by its shortcut: ready to type.
        if page == "clipboard-clear":
            root.after_idle(window.clear_clipboard_history)
        root.mainloop()
    finally:
        os.close(fd)
    return 0


def set_macos_app_identity() -> None:
    """Give Tk's Dock tile and application menu the Clipboard+ name, not Python."""
    if sys.platform != "darwin":
        return
    try:
        import Foundation
    except ImportError:
        return  # Tk can still open if optional macOS GUI bindings are unavailable.

    bundle = Foundation.NSBundle.mainBundle()
    if bundle is not None:
        for info in (bundle.localizedInfoDictionary(), bundle.infoDictionary()):
            if info is not None:
                info["CFBundleName"] = hotkeys.APP_NAME
                info["CFBundleDisplayName"] = hotkeys.APP_NAME
    Foundation.NSProcessInfo.processInfo().setProcessName_(hotkeys.APP_NAME)


if __name__ == "__main__":
    raise SystemExit(main())
