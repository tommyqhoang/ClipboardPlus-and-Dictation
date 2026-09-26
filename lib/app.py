"""Clipboard+ Desktop window and first-run walkthrough."""

from __future__ import annotations

import concurrent.futures
import dataclasses
import functools
import json
import os
import shlex
import subprocess
import sys
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from tkinter import filedialog, font, messagebox, ttk
from typing import Any, Literal

import clipstore
import clipui
import desktop
import dictation as d
import hotkeys
import workflow
from app_service import PROVIDERS, MicrophoneTest, Remote, Service

ICON = Path(__file__).with_name("whisper-dictation.png")
MODES = (
    ("dictation", "Dictation", "Press a shortcut, speak, and paste anywhere."),
    ("clipboard", "Clipboard history", "Keep what you copy, search it, and copy it back."),
    ("both", "Both", "Dictation and clipboard history, together."),
)

# One palette for every surface; the icon uses the same teal.
BACKGROUND = "#f3f6f8"
SURFACE = "#ffffff"
BORDER = "#dce3e9"
TEXT = "#13222d"
MUTED = "#5a6b78"
ACCENT = "#0f766e"
ACCENT_ACTIVE = "#0b5c56"
ACCENT_SOFT = "#e2f1ee"
DANGER = "#c93a2e"
DANGER_ACTIVE = "#a82e24"
WARNING = "#c98a12"
IDLE = "#9aa8b3"
HOVER = "#eef5f4"  # A list row under the pointer.
PAD = 20  # The page's side padding.
# Pages reached from the header tabs once setup is done; they need no big title.
TAB_PAGES = ("home", "clipboard", "settings")


class App:
    def __init__(self, root: tk.Tk, service: Service, page: str = "") -> None:
        self.root, self.service = root, service
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        # Quiet lookups (microphones) that must not lock the page like `submit` does.
        self.helper = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self.lookups: list[tuple[concurrent.futures.Future[Any], Callable[[Any], None]]] = []
        self.pending: concurrent.futures.Future[Any] | None = None
        self.done: Callable[[Any], None] = lambda value: None
        self.page = ""
        self.closing = False
        self.last_text = ""
        self.transcript_seen: tuple[Any, ...] = ()
        self.download = (0, 0)
        self.buttons: list[ttk.Button] = []
        self.root.title(hotkeys.APP_NAME)
        self.rewrap_timer: str | None = None
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        width = min(780, max(360, screen_width - 80))
        height = min(640, max(360, screen_height - 100))
        saved = self.saved_size()
        if saved is not None:
            # The size the user left it at, as long as it still fits this screen.
            width = max(360, min(saved[0], screen_width - 40))
            height = max(360, min(saved[1], screen_height - 60))
        self.wraplength = max(260, width - 2 * PAD - 30)
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(min(480, width), min(400, height))
        self.root.configure(background=BACKGROUND)
        self.icon = self.load_icon()
        if self.icon is not None:
            self.root.iconphoto(True, self.icon)
        self.status = tk.StringVar(value="")
        self.styles()
        self.header()
        self.bottom_bar()
        # Fixed controls above the scrolling page (the clipboard search stays in view).
        self.toolbar = ttk.Frame(root)
        container = self.container = ttk.Frame(root)
        container.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(
            container,
            background=BACKGROUND,
            borderwidth=0,
            highlightthickness=0,
            yscrollincrement=20,
        )
        self.scrollbar = ttk.Scrollbar(container, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scroll)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.frame = ttk.Frame(self.canvas, padding=(PAD, 14, PAD, 20))
        self.frame_window = self.canvas.create_window((0, 0), window=self.frame, anchor="nw")
        self.frame.bind("<Configure>", self.resize_scroll_region)
        self.canvas.bind("<Configure>", self.resize_content)
        # Windows and macOS send <MouseWheel>; X11 sends buttons 4 and 5.
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.root.bind_all(sequence, self.wheel, add="+")
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
        self.clipboard_page: clipui.ClipboardPage | None = None
        # Opened by the history shortcut or menu: like a picker, it closes once you copy.
        self.quick = page == "clipboard"
        self.clipboard_store: clipstore.Store | None = None
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        # The tray/menu bar app owns everyday use; this window is for setup.
        self.tray = True
        if page == "shortcut":
            self.shortcut_page()
        elif service.completed() and page == "clipboard" and self.features().clipboard:
            self.clipboard()
        elif service.completed() and page == "settings":
            self.settings()
        elif service.completed():
            self.home()
        else:
            self.welcome()
        self.timer = self.root.after(150, self.poll)

    def size_file(self) -> Path:
        return self.service.paths.config.parent / "window.json"

    def saved_size(self) -> tuple[int, int] | None:
        try:
            raw = d.read_json(self.size_file())
        except (d.DictationError, OSError, ValueError):
            return None
        width, height = raw.get("width"), raw.get("height")
        if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
            return width, height
        return None

    def save_size(self) -> None:
        width, height = self.root.winfo_width(), self.root.winfo_height()
        if width < 200 or height < 200:
            return  # Never shown (or withdrawn): keep what was saved.
        try:
            d.private_dir(self.size_file().parent)
            d.atomic(self.size_file(), json.dumps({"width": width, "height": height}))
        except OSError:
            pass  # Only a convenience.

    def load_icon(self) -> tk.PhotoImage | None:
        try:
            return tk.PhotoImage(master=self.root, file=str(ICON))
        except tk.TclError:
            return None

    def styles(self) -> None:
        system = font.nametofont("TkDefaultFont").actual()
        family = str(system["family"])
        # Follow the desktop's own text size (and its display scaling) instead of fixed,
        # oversized points: 13 on a Mac, about 10 on Linux and 9 on Windows.
        base = max(9, min(13, abs(int(system["size"])) or 10))
        self.fonts: dict[str, tuple[str, int, str]] = {
            "title": (family, base + 5, "bold"),
            "heading": (family, base + 1, "bold"),
            "body": (family, base, "normal"),
            "small": (family, max(8, base - 1), "normal"),
            "brand": (family, base + 1, "bold"),
            "badge": (family, base, "bold"),
            "record": (family, base + 2, "bold"),
            "icon": (family, base + 3, "normal"),
        }
        # For widgets drawn outside ttk (the clipboard list), whose rows change on hover.
        self.colors = {
            "surface": SURFACE,
            "hover": HOVER,
            "selected": ACCENT_SOFT,  # The row Enter copies.
            "border": BORDER,
            "text": TEXT,
            "muted": MUTED,
            "accent": ACCENT,
            "danger": DANGER,
            "star": WARNING,
        }
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", background=BACKGROUND, foreground=TEXT, font=self.fonts["body"])
        style.configure("TFrame", background=BACKGROUND)
        style.configure("Card.TFrame", background=SURFACE)
        style.configure("Header.TFrame", background=SURFACE)
        style.configure("TLabel", background=BACKGROUND, foreground=TEXT)
        style.configure("Title.TLabel", font=self.fonts["title"])
        style.configure("Hint.TLabel", foreground=MUTED, font=self.fonts["small"])
        style.configure("Error.TLabel", foreground=DANGER, font=self.fonts["small"])
        style.configure("Subtitle.TLabel", foreground=MUTED, font=self.fonts["body"])
        style.configure("Card.TLabel", background=SURFACE)
        style.configure("CardHeading.TLabel", background=SURFACE, font=self.fonts["heading"])
        style.configure(
            "CardHint.TLabel", background=SURFACE, foreground=MUTED, font=self.fonts["small"]
        )
        style.configure(
            "CardError.TLabel", background=SURFACE, foreground=DANGER, font=self.fonts["body"]
        )
        style.configure("Brand.TLabel", background=SURFACE, font=self.fonts["brand"])
        style.configure(
            "Step.TLabel", background=SURFACE, foreground=MUTED, font=self.fonts["small"]
        )
        style.configure(
            "Card.TRadiobutton",
            background=SURFACE,
            foreground=TEXT,
            font=self.fonts["body"],
            indicatorcolor=SURFACE,
            indicatorbackground=SURFACE,
        )
        style.map(
            "Card.TRadiobutton",
            background=[("active", SURFACE)],
            indicatorcolor=[("selected", ACCENT)],
        )
        style.configure(
            "TCombobox",
            fieldbackground=SURFACE,
            background=SURFACE,
            bordercolor=BORDER,
            lightcolor=SURFACE,
            darkcolor=SURFACE,
            arrowcolor=MUTED,
            padding=4,
        )
        style.map("TCombobox", fieldbackground=[("readonly", SURFACE)])
        style.configure(
            "TEntry",
            fieldbackground=SURFACE,
            bordercolor=BORDER,
            lightcolor=SURFACE,
            darkcolor=SURFACE,
            padding=5,
        )
        style.map("TEntry", bordercolor=[("focus", ACCENT)], lightcolor=[("focus", ACCENT)])
        buttons = {
            "TButton": (SURFACE, TEXT, "#c9d3db", "#eef2f5", SURFACE),
            "Primary.TButton": (ACCENT, "white", ACCENT, ACCENT_ACTIVE, "#8db8b3"),
            "Danger.TButton": (DANGER, "white", DANGER, DANGER_ACTIVE, "#e0a39d"),
        }
        for name, (fill, ink, edge, active, muted) in buttons.items():
            style.configure(
                name,
                background=fill,
                foreground=ink,
                bordercolor=edge,
                lightcolor=fill,
                darkcolor=fill,
                focuscolor=fill,
                relief="solid",
                borderwidth=1,
                padding=(12, 5),
                font=self.fonts["body"],
            )
            style.map(
                name,
                background=[("disabled", muted), ("pressed", active), ("active", active)],
                lightcolor=[("disabled", muted), ("pressed", active), ("active", active)],
                darkcolor=[("disabled", muted), ("pressed", active), ("active", active)],
                bordercolor=[("disabled", BORDER if fill == SURFACE else muted)],
                foreground=[("disabled", "#9fb0bc" if fill == SURFACE else "white")],
            )
        # Compact buttons for dense lists (clipboard rows, filters).
        # The theme's buttons are at least 11 characters wide; small ones fit their words.
        for name in ("Small.TButton", "Small.Primary.TButton", "Small.Danger.TButton"):
            style.configure(name, padding=(10, 2), font=self.fonts["small"], width=-6)
        # Idle Record is neutral: the shortcut, not this button, is the main way in.
        for name in ("", "Primary.", "Danger."):
            style.configure(f"Record.{name}TButton", padding=(18, 10), font=self.fonts["record"])
        # Header tabs: quiet text, the current one filled.
        style.configure(
            "Tab.TButton",
            background=SURFACE,
            foreground=MUTED,
            bordercolor=SURFACE,
            lightcolor=SURFACE,
            darkcolor=SURFACE,
            focuscolor=SURFACE,
            relief="flat",
            padding=(12, 4),
            font=self.fonts["body"],
        )
        style.map(
            "Tab.TButton",
            background=[("active", ACCENT_SOFT)],
            lightcolor=[("active", ACCENT_SOFT)],
            darkcolor=[("active", ACCENT_SOFT)],
            bordercolor=[("active", ACCENT_SOFT)],
            foreground=[("active", ACCENT)],
        )
        style.configure(
            "Tab.Current.TButton",
            background=ACCENT_SOFT,
            foreground=ACCENT,
            bordercolor=ACCENT_SOFT,
            lightcolor=ACCENT_SOFT,
            darkcolor=ACCENT_SOFT,
            focuscolor=ACCENT_SOFT,
            relief="flat",
            padding=(12, 4),
            font=self.fonts["brand"],
        )
        style.configure("Toolbar.TFrame", background=BACKGROUND)
        style.configure(
            "Card.TCheckbutton",
            background=SURFACE,
            foreground=TEXT,
            font=self.fonts["body"],
            indicatorbackground=SURFACE,
        )
        style.map(
            "Card.TCheckbutton",
            background=[("active", SURFACE)],
            indicatorcolor=[("selected", ACCENT)],
        )
        style.configure(
            "Placeholder.TLabel", background=SURFACE, foreground=IDLE, font=self.fonts["body"]
        )
        style.configure(
            "Link.TButton",
            background=BACKGROUND,
            foreground=ACCENT,
            bordercolor=BACKGROUND,
            lightcolor=BACKGROUND,
            darkcolor=BACKGROUND,
            focuscolor=BACKGROUND,
            relief="flat",
            padding=(4, 6),
        )
        style.map(
            "Link.TButton",
            foreground=[("disabled", IDLE), ("active", ACCENT_ACTIVE)],
            background=[("active", BACKGROUND)],
        )
        style.configure(
            "Vertical.TScrollbar",
            background="#cfd8df",
            troughcolor=BACKGROUND,
            bordercolor=BACKGROUND,
            lightcolor="#cfd8df",
            darkcolor="#cfd8df",
            arrowcolor=MUTED,
            relief="flat",
        )
        style.configure(
            "TProgressbar",
            background=ACCENT,
            troughcolor="#e4eaee",
            bordercolor="#e4eaee",
            lightcolor=ACCENT,
            darkcolor=ACCENT,
            thickness=6,
        )

    def header(self) -> None:
        """Brand on the left; the tabs (or the setup step) on the right. Always in view."""
        bar = ttk.Frame(self.root, style="Header.TFrame", padding=(PAD - 4, 8))
        bar.pack(fill="x")
        # Packed first so a narrow window clips the name, never the tabs.
        self.nav = ttk.Frame(bar, style="Header.TFrame")
        self.nav.pack(side="right")
        self.step = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.step, style="Step.TLabel").pack(side="right")
        brand = ttk.Frame(bar, style="Header.TFrame")
        brand.pack(side="left")
        if self.icon is not None:
            self.header_icon = self.icon.subsample(max(1, self.icon.width() // 24))
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
        bar = ttk.Frame(self.bottom, style="Header.TFrame", padding=(PAD, 8))
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

    def show_toolbar(self, padding: tuple[int, int, int, int]) -> ttk.Frame:
        """The fixed area above the scrolling page, for this page's controls."""
        self.toolbar.configure(padding=padding)
        self.toolbar.pack(fill="x", before=self.container)
        return self.toolbar

    def update_bottom(self) -> None:
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
        if float(first) <= 0 and float(last) >= 1:
            self.scrollbar.pack_forget()
        elif not self.scrollbar.winfo_manager():
            self.scrollbar.pack(side="right", fill="y", before=self.canvas)
        self.scrollbar.set(first, last)

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
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def resize_content(self, event: tk.Event[Any]) -> None:
        self.canvas.itemconfigure(self.frame_window, width=event.width)
        # Re-wrap once the user stops dragging, not on every pixel.
        if self.rewrap_timer is not None:
            self.root.after_cancel(self.rewrap_timer)
        self.rewrap_timer = self.root.after(80, lambda: self.rewrap(event.width))

    def rewrap(self, canvas_width: int) -> None:
        """Let every wrapped label follow the window's width (they were sized for the old one)."""
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
        self.status_label.configure(wraplength=max(200, self.wraplength - 180))

    def reset(self, page: str, title: str, subtitle: str, step: str = "") -> None:
        if self.page == "shortcut" and page != "shortcut":
            self.end_capture()
        self.page = page
        self.buttons = []
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
            ).pack(anchor="w", pady=(0, 14))
        self.status.set("")
        # The page fills the bar after this; show or hide it once it has.
        self.root.after_idle(self.update_bottom)

    def bordered(self, parent: tk.Misc, pady: tuple[int, int] = (0, 8)) -> ttk.Frame:
        """A white, outlined panel inside `parent`."""
        outline = tk.Frame(parent, background=BORDER, padx=1, pady=1)
        outline.pack(fill="x", pady=pady)
        body = ttk.Frame(outline, style="Card.TFrame", padding=(12, 8))
        body.pack(fill="both", expand=True)
        return body

    def features(self) -> hotkeys.Features:
        return hotkeys.Preferences(self.service.paths).features()

    def tab_names(self) -> list[str]:
        features = self.features()
        return (
            (["clipboard"] if features.clipboard else [])
            + (["dictation"] if features.dictation else [])
            + ["settings"]
        )

    def tab(self, name: str) -> None:
        self.quick = False  # Browsing now: copying no longer closes the window.
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
            self.clipboard_store = clipstore.Store(self.service.paths.clipboard)
        self.clipboard_page = clipui.ClipboardPage(self, self.clipboard_store)
        self.clipboard_page.render()

    def clipboard_optin(self, after: Callable[[bool], None]) -> None:
        """Ask before anything is captured: the choice is explicit and reversible."""
        self.reset(
            "clipboard-optin",
            "Keep a history of what you copy?",
            "Search it, star favorites and copy things back, on every computer you use.",
            self.step_label("clipboard"),
        )
        card = self.card(
            "How it works",
            "It saves text, links and images you copy so you can find them again. Everything "
            "stays on this computer unless you connect a Clipboard+ account, and even then "
            "images are never uploaded. Anything a password manager marks as secret is "
            "skipped. You can pause or turn it off at any time.",
        )

        def choose(enabled: bool) -> None:
            if enabled:
                prefs = hotkeys.Preferences(self.service.paths)
                features = prefs.features()
                prefs.save(features=hotkeys.Features(features.dictation, True))
            after(enabled)

        row = ttk.Frame(card, style="Card.TFrame")
        row.pack(fill="x")
        self.button("Turn on", lambda: choose(True), True, row, "left")
        self.button("Not now", lambda: choose(False), False, row, "left")

    def card(self, heading: str = "", hint: str = "") -> ttk.Frame:
        outline = tk.Frame(self.frame, background=BORDER, padx=1, pady=1)
        outline.pack(fill="x", pady=(0, 10))
        body = ttk.Frame(outline, style="Card.TFrame", padding=(16, 12))
        body.pack(fill="both", expand=True)
        if heading:
            ttk.Label(body, text=heading, style="CardHeading.TLabel").pack(anchor="w")
        if hint:
            ttk.Label(
                body, text=hint, style="CardHint.TLabel", wraplength=self.wraplength - 40
            ).pack(anchor="w", pady=(1, 8))
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
        self.buttons.append(button)
        return button

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
        self.button("Get started", self.begin_setup, True, self.actions(), "right")

    def begin_setup(self) -> None:
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
        self.setup_steps = [
            "features",
            *(("dictation",) if dictation and not self.service.ready() else ()),
            *(("clipboard",) if mode != "dictation" else ()),
            "done",
        ]
        if mode == "clipboard":
            self.clipboard_optin(self.after_optin)
            return
        if mode == "dictation":
            hotkeys.Preferences(self.service.paths).save(features=hotkeys.Features(True, False))
        if self.service.ready():
            self.after_dictation_setup()
        else:
            self.settings()

    def after_dictation_setup(self) -> None:
        if self.setup_mode == "both":
            self.clipboard_optin(self.after_optin)
        else:
            self.tutorial()

    def after_optin(self, enabled: bool) -> None:
        prefs = hotkeys.Preferences(self.service.paths)
        if self.setup_mode == "clipboard":
            if not enabled:
                self.choose_features("Choose at least one thing to use.")
                return
            prefs.save(features=hotkeys.Features(False, True))
        else:
            prefs.save(features=hotkeys.Features(True, enabled))
        self.tutorial()

    def apply_mode(self, mode: str) -> None:
        features = {"dictation": (True, False), "clipboard": (False, True), "both": (True, True)}
        dictation, clipboard = features[mode]
        hotkeys.Preferences(self.service.paths).save(
            features=hotkeys.Features(dictation, clipboard)
        )
        self.settings()

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

    def save_clipboard_options(self, keep_items: int, keep_days: int, images: bool) -> None:
        prefs = hotkeys.Preferences(self.service.paths)
        prefs.save(
            clipboard=dataclasses.replace(
                prefs.clipboard(), keep_items=keep_items, keep_days=keep_days, images=images
            )
        )

    def clipboard_options_card(self) -> None:
        card = self.card(
            "Clipboard history", "Saved on this computer. Favorites are never removed."
        )
        saved = hotkeys.Preferences(self.service.paths).clipboard()
        items = tk.StringVar(value=str(saved.keep_items))
        days = tk.StringVar(value=str(saved.keep_days))
        images = tk.BooleanVar(value=saved.images)

        def save(*_: object) -> None:
            self.save_clipboard_options(int(items.get()), int(days.get()), images.get())

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
        """The global shortcuts: dictation's (recorded) and the clipboard history's (chosen)."""
        features = self.features()
        prefs = hotkeys.Preferences(self.service.paths)
        card = self.card("Keyboard shortcuts", "They work in any app.")
        dictation = prefs.shortcut() if features.dictation else None
        if dictation is not None:
            row = ttk.Frame(card, style="Card.TFrame")
            row.pack(fill="x")
            ttk.Label(row, text=f"Dictation: {dictation.label()}", style="Card.TLabel").pack(
                side="left"
            )
            self.button(
                "Change…",
                lambda: self.shortcut_page(back=self.settings),
                parent=row,
                side="right",
            )
        if not features.clipboard:
            return
        ttk.Label(card, text="Open clipboard history", style="Card.TLabel").pack(
            anchor="w", pady=(10, 2)
        )
        choices = {s.label(): s for s in hotkeys.HISTORY_PRESETS if s != dictation}
        current = prefs.history_shortcut()
        if current is not None:
            choices.setdefault(current.label(), current)
        chosen = tk.StringVar(master=self.root, value=current.label() if current else "Off")
        box = ttk.Combobox(card, textvariable=chosen, values=(*choices, "Off"), state="readonly")
        box.pack(fill="x")

        def choose(_: object) -> None:
            picked = choices.get(chosen.get())
            prefs.save(history_shortcut=picked or False)
            self.status.set(
                f"Press {picked.label()} anywhere to open your clipboard history."
                if picked
                else "The clipboard history shortcut is off."
            )

        box.bind("<<ComboboxSelected>>", choose)
        conflict = hotkeys.shortcut_conflict(self.service.paths, hotkeys.HISTORY_STATUS)
        if current is not None and conflict is not None:
            self.conflict_note(card, current, conflict, hotkeys.HISTORY_STATUS, self.settings)
        elif current is not None and not hotkeys.shortcut_working(
            self.service.paths, hotkeys.HISTORY_STATUS
        ):
            problem = (
                "This desktop can’t set shortcuts automatically. In your keyboard settings, "
                f"assign {current.label()} to: "
                + shlex.join(hotkeys.history_command(Path(__file__).resolve().parent))
                if desktop.platform_name() == "linux"
                else f"Another app already uses {current.label()}. Choose another."
            )
            ttk.Label(
                card, text=problem, style="CardHint.TLabel", wraplength=self.wraplength - 50
            ).pack(anchor="w", pady=(6, 0))

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
        self.reset("settings", "", "")
        if self.service.completed():  # During setup the choice was just made.
            self.features_card()
            self.shortcuts_card()
        self.clipboard_options_card()
        self.clipboard_plus_card()
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
                    f"from the {place} icon."
                    if history
                    else f"From the {place} icon, choose Clipboard History…",
                ),
                ("Search, star, copy back", "Favorites are kept when you clear the history."),
            ],
        )
        self.clipboard_plus_card()
        self.button("Done", self.finish_setup, True, self.actions(), "right")

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
            self.reset("settings", "", "")
        config = d.Config(self.service.paths)
        self.language.set(
            "English" if config.s("language") == "en" else "Multilingual / auto-detect"
        )
        self.device.set(config.s("device"))
        existing = config.s("model") if os.path.isfile(config.s("model")) else ""
        self.model.set(existing)
        remote = config.s("backend") == "http"
        self.model_source.set("service" if remote else "file" if existing else "download")
        self.endpoint.set(config.s("endpoint"))
        self.api_model.set(config.s("api_model"))
        self.api_key.set("")
        self.provider.set(
            next(
                (
                    name
                    for name, (url, _) in PROVIDERS.items()
                    if url and url == config.s("endpoint")
                ),
                list(PROVIDERS)[-1] if remote else next(iter(PROVIDERS)),
            )
        )
        voice = self.card("Dictation")
        ttk.Label(voice, text="Language", style="Card.TLabel").pack(anchor="w")
        ttk.Combobox(
            voice,
            textvariable=self.language,
            state="readonly",
            values=("English", "Multilingual / auto-detect"),
        ).pack(fill="x", pady=(6, 16))
        ttk.Label(voice, text="Microphone", style="Card.TLabel").pack(anchor="w")
        row = ttk.Frame(voice, style="Card.TFrame")
        row.pack(fill="x", pady=(6, 0))
        self.device_picker = ttk.Combobox(row, textvariable=self.device, values=("default",))
        self.device_picker.pack(side="left", fill="x", expand=True)
        self.button("Refresh", self.find_microphones, parent=row, side="right")
        self.mic_button = self.button("Test", self.test_microphone, parent=row, side="right")
        # A live level meter while testing (hidden otherwise).
        self.meter = tk.Canvas(voice, height=10, background=SURFACE, highlightthickness=0)
        for key, text in (
            ("overlay", "Show the recording bar (voice levels, then “Copied”)"),
            ("live", "Show a live draft while recording (uses more processing)"),
        ):
            switch = tk.BooleanVar(master=self.root, value=config.b(key))
            ttk.Checkbutton(
                voice,
                text=text,
                variable=switch,
                style="Card.TCheckbutton",
                command=functools.partial(self.set_option, key, switch),
            ).pack(anchor="w", pady=(10 if key == "overlay" else 4, 0))
        ai = self.card("Transcription AI", "What turns your voice into text.")
        self.choices: dict[str, tuple[ttk.Frame, ttk.Widget]] = {}
        for value, title, hint in (
            (
                "download",
                "Free on-device AI (recommended)",
                "Private: audio never leaves this computer. One-time 148 MB download.",
            ),
            ("file", "A Whisper model file I already have", ""),
            (
                "service",
                "My own AI service",
                "Fast on any computer. Audio is sent to the service you choose, which may charge.",
            ),
        ):
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
        if not setup:  # During setup the choice was just made; changing it here strands it.
            self.features_card()
            self.shortcuts_card()
        if self.features().clipboard:
            self.clipboard_options_card()
        self.clipboard_plus_card()
        self.button("Continue" if setup else "Save", self.prepare, True, self.actions(), "right")
        if not setup:
            self.button("Close", self.leave, parent=self.actions(), side="right")
        # Discovery only lists devices; it never opens the microphone.
        self.root.after_idle(self.find_microphones)

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
        self.status.set("Saved. It applies to your next recording.")

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

    def background(self, work: Callable[[], Any], done: Callable[[Any], None]) -> None:
        """Run `work` off the UI thread; `poll` passes its result to `done`. Nothing locks."""
        self.lookups.append((self.helper.submit(work), done))

    def show_microphones(self, devices: list[str]) -> None:
        if self.page != "settings" or not self.device_picker.winfo_exists():
            return  # The user moved on while we were looking.
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
        self.status.set(f"{found} Pick the one you’ll speak into.")

    def prepare(self) -> None:
        language = "en" if self.language.get() == "English" else "auto"
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

        def report(done: int, total: int) -> None:
            self.download = (done, total)

        setup = not self.service.completed()

        def done(_: object) -> None:
            if setup:
                self.after_dictation_setup()
            else:
                self.status.set("Settings saved.")  # Stay here: no walkthrough again.

        self.submit(
            lambda: self.service.prepare(language, device, model, report, remote),
            done,
            "Checking your settings. Nothing is recording."
            if remote
            else "Getting your speech model ready. Nothing is recording.",
        )

    def tutorial(self) -> None:
        if not self.features().dictation:
            self.tutorial_clipboard()
            return
        self.reset(
            "tutorial",
            "You’re ready to speak",
            "Three steps, no commands to learn.",
            self.step_label("done"),
        )
        paste = "Command + V" if desktop.platform_name() == "macos" else "Ctrl + V"
        place = {"macos": "menu bar", "windows": "system tray"}.get(
            desktop.platform_name(), "top bar"
        )
        shortcut = hotkeys.Preferences(self.service.paths).shortcut().label()
        body = self.card()
        self.steps(
            body,
            [
                (
                    f"Press {shortcut} in any app and speak",
                    "A small bar at the bottom of your screen shows it’s listening; "
                    f"the icon in the {place} turns red.",
                ),
                (
                    f"Press {shortcut} again to stop",
                    "The bar shows “Transcribing…”, then “Copied” when your text is ready.",
                ),
                ("Paste anywhere", f"Your words are already copied. Press {paste}."),
            ],
        )
        tips = self.card("Try saying")
        ttk.Label(
            tips,
            text="“Hello world. New paragraph. This is my first dictation.”",
            style="Card.TLabel",
            wraplength=self.wraplength - 50,
        ).pack(anchor="w")
        ttk.Label(
            tips,
            text="Change the shortcut, microphone, or AI anytime in Settings, from this "
            f"window or the {place} icon."
            + (
                " Not on GNOME? Assign the shortcut to ~/.local/bin/dictate-toggle in your"
                " keyboard settings."
                if desktop.platform_name() == "linux"
                else ""
            ),
            style="CardHint.TLabel",
            wraplength=self.wraplength - 50,
        ).pack(anchor="w", pady=(10, 0))
        self.button(
            "Done" if self.tray else "Start dictating",
            self.finish_setup,
            True,
            self.actions(),
            "right",
        )

    def clipboard_plus_card(self) -> None:
        """The account card, where the clipboard history is (or an old key still lives)."""
        self.account = None
        if self.features().clipboard or self.service.clipboard_plus_linked():
            self.account = clipui.AccountCard(self)

    def finish_setup(self) -> None:
        self.submit(self.service.complete, lambda _: self.leave(), "Saving your setup…")

    def leave(self) -> None:
        if self.tray:
            self.destroy()
        else:
            self.home()

    def shortcut_page(self, back: Callable[[], None] | None = None) -> None:
        """Record a new dictation shortcut. `back` returns to a page; otherwise the window closes."""
        self.shortcut_back = back
        self.reset(
            "shortcut",
            "Choose your shortcut",
            (
                "Hold ⌃, ⌥ or ⌘ and press a letter, number or Space — "
                if desktop.platform_name() == "macos"
                else "Hold Ctrl, Alt or Win/Super and press a letter, number or Space — "
            )
            + "or press a function key (F1–F12).",
        )
        self.capture = self.service.paths.runtime / "shortcut-capture"
        d.private_dir(self.capture.parent)
        d.atomic(self.capture, "capturing")  # Pauses the tray's current shortcut.
        self.held: set[str] = set()
        self.captured: hotkeys.Shortcut | None = None
        body = self.card()
        current = hotkeys.Preferences(self.service.paths).shortcut()
        self.shortcut_label = ttk.Label(
            body, text=current.label(), style="Card.TLabel", font=self.fonts["title"]
        )
        self.shortcut_label.pack(anchor="w")
        self.shortcut_hint = ttk.Label(
            body, text="Press the keys now.", style="CardHint.TLabel", wraplength=self.wraplength
        )
        self.shortcut_hint.pack(anchor="w", pady=(6, 0))
        self.save_shortcut_button = self.button(
            "Save", self.save_shortcut, True, self.actions(), "right"
        )
        self.save_shortcut_button.state(["disabled"])
        self.button("Cancel", self.shortcut_done, parent=self.actions(), side="right")
        self.root.bind("<KeyPress>", self.shortcut_key)
        self.root.bind("<KeyRelease>", self.shortcut_release)
        self.root.focus_force()

    def shortcut_key(self, event: tk.Event[Any]) -> str:
        if event.keysym in hotkeys.TK_MODIFIERS:
            self.held.add(hotkeys.TK_MODIFIERS[event.keysym])
            return "break"
        shortcut = hotkeys.from_tk(event.keysym, self.held)
        if shortcut is not None:
            problem = shortcut.problem()
            self.shortcut_label.configure(text=shortcut.label())
            self.shortcut_hint.configure(text=problem or "Press Save to use this shortcut.")
            self.captured = None if problem else shortcut
            self.save_shortcut_button.state(["disabled"] if problem else ["!disabled"])
        return "break"

    def shortcut_release(self, event: tk.Event[Any]) -> None:
        self.held.discard(hotkeys.TK_MODIFIERS.get(event.keysym, ""))

    def save_shortcut(self) -> None:
        if self.captured is not None:
            hotkeys.Preferences(self.service.paths).save(shortcut=self.captured)
        self.shortcut_done()
        if self.captured is not None and self.page != "closed":
            self.status.set(f"Dictation shortcut: {self.captured.label()}.")

    def shortcut_done(self) -> None:
        back = getattr(self, "shortcut_back", None)
        if back is None:
            self.destroy()
        else:
            back()  # Leaving the page ends the capture.

    def home(self) -> None:
        shortcut = hotkeys.Preferences(self.service.paths).shortcut().label()
        platform = desktop.platform_name()
        paste = "Command\u00a0+\u00a0V" if platform == "macos" else "Ctrl\u00a0+\u00a0V"
        if platform == "linux":
            paste += " (Ctrl\u00a0+\u00a0Shift\u00a0+\u00a0V in a terminal)"
        place = {"macos": "menu bar", "windows": "system tray"}.get(platform, "top bar")
        conflict = hotkeys.shortcut_conflict(self.service.paths)
        if conflict is not None:
            title = f"{shortcut} is taken by something else"
            subtitle = (
                f"{conflict.name[0].upper() + conflict.name[1:]} also uses {shortcut} and gets it "
                "first, so dictation doesn’t start. Until you fix it, record from here."
            )
        elif hotkeys.shortcut_working(self.service.paths):
            title = f"Press {shortcut} to dictate"
            subtitle = (
                f"It works in any app: press it, speak, press it again, then paste with {paste}. "
                "You don’t need this window."
            )
        elif platform == "linux":
            title = "Set up your shortcut"
            subtitle = (
                "This desktop can’t set shortcuts automatically. In your keyboard settings, "
                f"assign {shortcut} to ~/.local/bin/dictate-toggle. Until then, record from here."
            )
        else:
            title = f"{shortcut} is taken"
            subtitle = (
                f"Another app already uses {shortcut}. Choose a different shortcut from the "
                f"{place} icon. Until then, record from here."
            )
        self.reset("home", title, subtitle)
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
        outline = tk.Frame(self.frame, background=BORDER, padx=1, pady=1)
        outline.pack(fill="both", expand=True, pady=(4, 10))
        self.transcript = tk.Text(
            outline,
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
        row = ttk.Frame(self.frame)
        row.pack(fill="x")
        self.copy = ttk.Button(row, text="Copy transcript", command=lambda: self.action("copy"))
        self.copy.pack(side="left")
        self.retry = ttk.Button(
            row, text="Retry saved recording", command=lambda: self.action("transcribe")
        )
        self.retry.pack(side="right")
        self.discard = ttk.Button(row, text="Discard", command=self.discard_audio)
        self.discard.pack(side="right", padx=(0, 8))
        self.buttons.extend([self.copy, self.retry, self.discard])
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
        if hotkeys.gnome_release(conflict.path):
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
        if self.closing and not active:
            self.destroy()

    def open_page(self, request: str) -> None:
        """Honor a page request from the tray or a shortcut when it is safe to leave."""
        if request == "clipboard":
            self.quick = True  # Opened to pick something: copying closes the window.
        if request == "clipboard" and self.page == "clipboard" and self.clipboard_page:
            self.clipboard_page.focus_search()
            return
        if self.pending is not None or self.page == request:
            return
        if request == "clipboard" and self.service.completed() and self.features().clipboard:
            self.clipboard()
        elif request == "shortcut" and not d.busy(self.service.paths):
            self.shortcut_page()
        elif request == "settings" and self.service.completed() and not d.busy(self.service.paths):
            self.settings()

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
        if not self.features().clipboard or not self.can_navigate():
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
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)  # Otherwise many desktops only flash it.
        self.root.after_idle(lambda: self.root.attributes("-topmost", False))
        self.root.focus_force()

    def poll(self) -> None:
        try:
            activation = self.service.paths.runtime / "show-window"
            if activation.exists():
                request = activation.read_text(encoding="utf-8")
                activation.unlink()
                self.bring_forward()
                self.open_page(request)
            for lookup in [entry for entry in self.lookups if entry[0].done()]:
                self.lookups.remove(lookup)
                future, finished = lookup
                finished(future.result())  # A failure is reported below, like any other.
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
        except (d.DictationError, OSError, ValueError, subprocess.SubprocessError) as exc:
            self.status.set(
                str(exc)
                if isinstance(exc, d.DictationError)
                else "Something went wrong. Check microphone permissions, connections, and free disk space, then retry."
            )
        except Exception:  # noqa: BLE001 - never let one failure stop the window updating.
            self.status.set("Something went wrong. Please try again.")
        if self.page != "closed":
            self.timer = self.root.after(200, self.poll)

    def close(self) -> None:
        if self.pending is not None:
            messagebox.showinfo(
                "Please wait",
                "An operation is finishing. Keep this window open until it completes.",
                parent=self.root,
            )
        elif d.busy(self.service.paths):
            if messagebox.askyesno(
                "Finish recording?",
                "Finish the current recording/transcription before closing?",
                parent=self.root,
            ):
                self.closing = True
                self.action("toggle")
        else:
            self.destroy()

    def end_capture(self) -> None:
        """Stop recording keys for a new shortcut; the tray re-enables the shortcut."""
        (self.service.paths.runtime / "shortcut-capture").unlink(missing_ok=True)
        self.root.unbind("<KeyPress>")
        self.root.unbind("<KeyRelease>")

    def destroy(self) -> None:
        if self.root.winfo_viewable():
            self.save_size()
        self.page = "closed"
        self.end_capture()
        self.root.after_cancel(self.timer)
        if self.rewrap_timer is not None:
            self.root.after_cancel(self.rewrap_timer)
        self.done = lambda _: None
        self.executor.shutdown(wait=True)
        self.helper.shutdown(wait=False, cancel_futures=True)  # Lookups only; nothing to save.
        if self.clipboard_store is not None:
            self.clipboard_store.close()
        self.root.destroy()


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    args = sys.argv[1:] if argv is None else argv
    page = next(
        (flag[2:] for flag in ("--settings", "--shortcut", "--clipboard") if flag in args), ""
    )
    paths = d.Paths()
    fd = desktop.lock(paths.runtime / "app.lock")
    if fd is None:
        # The running window shows itself, on the requested page if any.
        d.atomic(paths.runtime / "show-window", page or "show")
        return 0
    try:
        root = tk.Tk(className="WhisperDictation")
        window = App(root, Service(paths), page)
        if page == "clipboard":
            root.after_idle(window.bring_forward)  # Opened by its shortcut: ready to type.
        root.mainloop()
    finally:
        os.close(fd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
