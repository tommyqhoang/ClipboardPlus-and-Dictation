"""Whisper Dictation desktop window and first-run walkthrough."""

from __future__ import annotations

import concurrent.futures
import os
import subprocess
import sys
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from tkinter import filedialog, font, messagebox, ttk
from typing import Any, Literal

import clipboardplus
import desktop
import dictation as d
import hotkeys
import workflow
from app_service import PROVIDERS, Remote, Service

ICON = Path(__file__).with_name("whisper-dictation.png")

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


class App:
    def __init__(self, root: tk.Tk, service: Service, page: str = "") -> None:
        self.root, self.service = root, service
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self.pending: concurrent.futures.Future[Any] | None = None
        self.done: Callable[[Any], None] = lambda value: None
        self.page = ""
        self.closing = False
        self.last_text = ""
        self.download = (0, 0)
        self.buttons: list[ttk.Button] = []
        self.root.title("Whisper Dictation")
        width = min(720, max(360, self.root.winfo_screenwidth() - 80))
        height = min(700, max(360, self.root.winfo_screenheight() - 100))
        self.wraplength = max(260, width - 110)
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(min(520, width), min(460, height))
        self.root.configure(background=BACKGROUND)
        self.icon = self.load_icon()
        if self.icon is not None:
            self.root.iconphoto(True, self.icon)
        self.status = tk.StringVar(value="")
        self.styles()
        self.header()
        self.bottom_bar()
        container = ttk.Frame(root)
        container.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(
            container, background=BACKGROUND, borderwidth=0, highlightthickness=0
        )
        self.scrollbar = ttk.Scrollbar(container, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scroll)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.frame = ttk.Frame(self.canvas, padding=(36, 28, 36, 28))
        self.frame_window = self.canvas.create_window((0, 0), window=self.frame, anchor="nw")
        self.frame.bind("<Configure>", self.resize_scroll_region)
        self.canvas.bind("<Configure>", self.resize_content)
        self.language = tk.StringVar(value="English")
        self.device = tk.StringVar(value="default")
        self.model = tk.StringVar(value="")
        self.model_source = tk.StringVar(value="download")
        self.provider = tk.StringVar(value=next(iter(PROVIDERS)))
        self.endpoint = tk.StringVar(value="")
        self.api_model = tk.StringVar(value="")
        self.api_key = tk.StringVar(value="")
        self.clip_key = tk.StringVar(value="")
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        # The tray/menu bar app owns everyday use; this window is for setup.
        self.tray = True
        if page == "shortcut":
            self.shortcut_page()
        elif service.completed() and page == "settings":
            self.settings()
        elif service.completed():
            self.home()
        else:
            self.welcome()
        self.timer = self.root.after(150, self.poll)

    def load_icon(self) -> tk.PhotoImage | None:
        try:
            return tk.PhotoImage(master=self.root, file=str(ICON))
        except tk.TclError:
            return None

    def styles(self) -> None:
        family = font.nametofont("TkDefaultFont").actual("family")
        self.fonts: dict[str, tuple[str, int, str]] = {
            "title": (family, 22, "bold"),
            "heading": (family, 14, "bold"),
            "body": (family, 13, "normal"),
            "small": (family, 11, "normal"),
            "brand": (family, 13, "bold"),
            "badge": (family, 12, "bold"),
            "record": (family, 15, "bold"),
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
        style.configure("Subtitle.TLabel", foreground=MUTED, font=self.fonts["body"])
        style.configure("Card.TLabel", background=SURFACE)
        style.configure("CardHeading.TLabel", background=SURFACE, font=self.fonts["heading"])
        style.configure(
            "CardHint.TLabel", background=SURFACE, foreground=MUTED, font=self.fonts["small"]
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
            padding=6,
        )
        style.map("TCombobox", fieldbackground=[("readonly", SURFACE)])
        style.configure(
            "TEntry",
            fieldbackground=SURFACE,
            bordercolor=BORDER,
            lightcolor=SURFACE,
            darkcolor=SURFACE,
            padding=6,
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
                padding=(18, 10),
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
        # Idle Record is neutral: the shortcut, not this button, is the main way in.
        for name in ("", "Primary.", "Danger."):
            style.configure(f"Record.{name}TButton", padding=(24, 16), font=self.fonts["record"])
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
        bar = ttk.Frame(self.root, style="Header.TFrame", padding=(20, 12))
        bar.pack(fill="x")
        brand = ttk.Frame(bar, style="Header.TFrame")
        brand.pack(side="left")
        if self.icon is not None:
            self.header_icon = self.icon.subsample(8)
            ttk.Label(brand, image=self.header_icon, style="Brand.TLabel").pack(
                side="left", padx=(0, 10)
            )
        ttk.Label(brand, text="Whisper Dictation", style="Brand.TLabel").pack(side="left")
        self.step = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.step, style="Step.TLabel").pack(side="right")
        tk.Frame(self.root, height=1, background=BORDER).pack(fill="x")

    def bottom_bar(self) -> None:
        tk.Frame(self.root, height=1, background=BORDER).pack(side="bottom", fill="x")
        bar = ttk.Frame(self.root, style="Header.TFrame", padding=(24, 14))
        bar.pack(side="bottom", fill="x", before=self.root.pack_slaves()[-1])
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

    def scroll(self, first: float, last: float) -> None:
        # Show the scrollbar only when the page is taller than the window.
        if float(first) <= 0 and float(last) >= 1:
            self.scrollbar.pack_forget()
        elif not self.scrollbar.winfo_manager():
            self.scrollbar.pack(side="right", fill="y", before=self.canvas)
        self.scrollbar.set(first, last)

    def resize_scroll_region(self, event: tk.Event[Any]) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def resize_content(self, event: tk.Event[Any]) -> None:
        self.canvas.itemconfigure(self.frame_window, width=event.width)

    def reset(self, page: str, title: str, subtitle: str, step: str = "") -> None:
        if self.page == "shortcut" and page != "shortcut":
            self.end_capture()
        self.page = page
        self.buttons = []
        for child in self.frame.winfo_children():
            child.destroy()
        for child in self.bar_actions.winfo_children():
            child.destroy()
        self.canvas.yview_moveto(0)
        # Home shows status beside Record; the bar keeps only the progress.
        if page == "home":
            self.status_label.pack_forget()
        elif not self.status_label.winfo_manager():
            self.status_label.pack(anchor="w")
        self.step.set(step)
        ttk.Label(self.frame, text=title, style="Title.TLabel").pack(anchor="w", pady=(0, 6))
        ttk.Label(
            self.frame, text=subtitle, wraplength=self.wraplength, style="Subtitle.TLabel"
        ).pack(anchor="w", pady=(0, 20))
        self.status.set("")

    def card(self, heading: str = "", hint: str = "") -> ttk.Frame:
        outline = tk.Frame(self.frame, background=BORDER, padx=1, pady=1)
        outline.pack(fill="x", pady=(0, 16))
        body = ttk.Frame(outline, style="Card.TFrame", padding=(22, 16))
        body.pack(fill="both", expand=True)
        if heading:
            ttk.Label(body, text=heading, style="CardHeading.TLabel").pack(anchor="w")
        if hint:
            ttk.Label(
                body, text=hint, style="CardHint.TLabel", wraplength=self.wraplength - 50
            ).pack(anchor="w", pady=(2, 12))
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
            "Your voice. Your words.",
            "Speak naturally, then paste anywhere. Setup takes about a minute.",
        )
        if self.icon is not None:
            self.hero_icon = self.icon.subsample(4)
            hero = ttk.Label(self.frame, image=self.hero_icon)
            hero.pack(anchor="w", pady=(0, 14), before=self.frame.winfo_children()[0])
        body = self.card("How it works")
        self.steps(
            body,
            [
                ("Pick your language and microphone", ""),
                ("Get the free speech model", "A one-time 148 MB download."),
                ("Record, stop, and paste anywhere", "Your words are copied for you."),
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
        if self.service.ready():
            self.tutorial()
        else:
            self.settings()

    def settings(self) -> None:
        self.reset(
            "settings",
            "Set up dictation",
            "Choose how you’ll speak. You can change this anytime.",
            "Step 1 of 2",
        )
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
        voice = self.card()
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
        self.clipboard_plus_card()
        self.button("Continue", self.prepare, True, self.actions(), "right")
        if self.service.completed():
            self.button("Cancel", self.leave, parent=self.actions(), side="right")
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
        self.submit(
            self.service.microphones,
            self.show_microphones,
            "Looking for microphones…",
        )

    def show_microphones(self, devices: list[str]) -> None:
        self.device_picker.configure(values=devices)
        if self.device.get() not in devices:
            self.device.set(devices[0])
        found = f"{len(devices)} microphone{'s' if len(devices) != 1 else ''} found."
        self.status.set(f"{found} Pick the one you’ll speak into.")

    def prepare(self) -> None:
        if self.clip_key.get().strip():
            self.status.set("Press Connect to link Clipboard+, or clear the key field to skip it.")
            return
        language = "en" if self.language.get() == "English" else "auto"
        device = self.device.get()
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

        self.submit(
            lambda: self.service.prepare(language, device, model, report, remote),
            lambda _: self.tutorial(),
            "Checking your settings. Nothing is recording."
            if remote
            else "Getting your speech model ready. Nothing is recording.",
        )

    def tutorial(self) -> None:
        self.reset(
            "tutorial",
            "You’re ready to speak",
            "Three steps, no commands to learn.",
            "Step 2 of 2",
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
                    f"A notification confirms it’s recording; the icon in the {place} turns red.",
                ),
                (
                    f"Press {shortcut} again to stop",
                    "You’ll see “Transcribing…”, then a notification when it’s ready.",
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
            text=f"Change the shortcut, microphone, or AI anytime from the {place} icon."
            + (
                " Not on GNOME? Assign the shortcut to ~/.local/bin/dictate-toggle in your"
                " keyboard settings."
                if desktop.platform_name() == "linux"
                else ""
            ),
            style="CardHint.TLabel",
            wraplength=self.wraplength - 50,
        ).pack(anchor="w", pady=(10, 0))
        self.clipboard_plus_card()
        self.button(
            "Done" if self.tray else "Start dictating",
            self.finish_setup,
            True,
            self.actions(),
            "right",
        )

    def clipboard_plus_card(self) -> None:
        """Optional: also save each transcript to the user's Clipboard+ history."""
        body = self.card(
            "Keep every transcript in Clipboard+",
            "Each dictation replaces what’s on your clipboard. Connect Clipboard+ and every "
            "transcript is also saved to your history, to search on the website or in the "
            "browser extension. Optional: only the text is sent, never audio.",
        )
        self.clip_body = ttk.Frame(body, style="Card.TFrame")
        self.clip_body.pack(fill="x")
        self.clip_buttons: list[ttk.Button] = []
        self.render_clipboard_plus()

    def render_clipboard_plus(self) -> None:
        # Drop the old buttons from the busy/idle list before their widgets are destroyed.
        self.buttons = [button for button in self.buttons if button not in self.clip_buttons]
        self.clip_buttons = []
        for child in self.clip_body.winfo_children():
            child.destroy()
        row = ttk.Frame(self.clip_body, style="Card.TFrame")
        actions: tuple[tuple[str, Callable[[], None]], ...]
        if self.service.clipboard_plus_linked():
            ttk.Label(
                self.clip_body,
                text="Connected. New transcripts are saved to your Clipboard+ history.",
                style="Card.TLabel",
                wraplength=self.wraplength - 50,
            ).pack(anchor="w", pady=(0, 10))
            row.pack(fill="x")
            actions = (
                ("Open history", lambda: hotkeys.open_link(clipboardplus.DASHBOARD_URL)),
                ("Disconnect", self.disconnect_clipboard_plus),
            )
        else:
            ttk.Label(
                self.clip_body,
                text="In your Clipboard+ account, open Developer API, generate a key with "
                "clipboard write access, and paste it here.",
                style="CardHint.TLabel",
                wraplength=self.wraplength - 50,
            ).pack(anchor="w", pady=(0, 6))
            ttk.Entry(self.clip_body, textvariable=self.clip_key, show="•").pack(fill="x")
            row.pack(fill="x", pady=(10, 0))
            actions = (
                ("Connect", self.connect_clipboard_plus),
                ("Get a key", lambda: hotkeys.open_link(clipboardplus.ACCOUNT_URL)),
                ("Get Clipboard+", lambda: hotkeys.open_link()),
            )
        for text, command in actions:
            self.clip_buttons.append(self.button(text, command, parent=row, side="left"))

    def connect_clipboard_plus(self) -> None:
        key = self.clip_key.get()

        def connected(_: Any) -> None:
            self.clip_key.set("")  # The key is saved privately; do not leave it on screen.
            self.render_clipboard_plus()
            self.status.set("Connected to Clipboard+.")

        self.submit(
            lambda: self.service.connect_clipboard_plus(key),
            connected,
            "Checking your Clipboard+ key…",
        )

    def disconnect_clipboard_plus(self) -> None:
        def disconnected(_: Any) -> None:
            self.render_clipboard_plus()
            self.status.set("Disconnected from Clipboard+.")

        self.submit(self.service.disconnect_clipboard_plus, disconnected, "Disconnecting…")

    def finish_setup(self) -> None:
        self.submit(self.service.complete, lambda _: self.leave(), "Saving your setup…")

    def leave(self) -> None:
        if self.tray:
            self.destroy()
        else:
            self.home()

    def shortcut_page(self) -> None:
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
        self.button("Cancel", self.destroy, parent=self.actions(), side="right")
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
        self.destroy()

    def home(self) -> None:
        shortcut = hotkeys.Preferences(self.service.paths).shortcut().label()
        platform = desktop.platform_name()
        paste = "Command\u00a0+\u00a0V" if platform == "macos" else "Ctrl\u00a0+\u00a0V"
        place = {"macos": "menu bar", "windows": "system tray"}.get(platform, "top bar")
        if hotkeys.shortcut_working(self.service.paths):
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
        self.record.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.cancel = ttk.Button(
            controls, text="Cancel recording", command=lambda: self.action("cancel")
        )
        self.cancel.pack(side="left", fill="y")
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
            height=8,
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
        self.help_button = self.button(
            "How it works", self.tutorial, parent=self.bar_actions, side="right"
        )
        self.settings_button = self.button(
            "Settings", self.settings, parent=self.bar_actions, side="right"
        )

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
        for button in (self.retry, self.discard):
            button.state(["!disabled"] if retained and not active else ["disabled"])
        for button in (self.settings_button, self.help_button):
            button.state(["disabled"] if active else ["!disabled"])
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
        text = source.read_text(encoding="utf-8") if source.exists() else ""
        if text != self.last_text:
            self.show_transcript(text)
        if self.closing and not active:
            self.destroy()

    def open_page(self, request: str) -> None:
        """Honor a Settings or shortcut request from the tray when it is safe to leave."""
        if self.pending is not None or self.page == request:
            return
        if request == "shortcut" and not d.busy(self.service.paths):
            self.shortcut_page()
        elif request == "settings" and self.service.completed() and not d.busy(self.service.paths):
            self.settings()

    def poll(self) -> None:
        try:
            activation = self.service.paths.runtime / "show-window"
            if activation.exists():
                request = activation.read_text(encoding="utf-8")
                activation.unlink()
                self.root.deiconify()
                self.root.lift()
                self.open_page(request)
            if self.pending is not None and not self.pending.done():
                self.show_download()
            if self.pending is not None and self.pending.done():
                future, self.pending = self.pending, None
                self.download = (0, 0)
                self.progress.stop()
                self.progress.pack_forget()
                for button in self.buttons:
                    button.state(["!disabled"])
                done, self.done = self.done, lambda _: None
                done(future.result())
            if self.page == "home" and self.pending is None:
                self.refresh()
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
        self.page = "closed"
        self.end_capture()
        self.root.after_cancel(self.timer)
        self.done = lambda _: None
        self.executor.shutdown(wait=True)
        self.root.destroy()


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    args = sys.argv[1:] if argv is None else argv
    page = next((flag[2:] for flag in ("--settings", "--shortcut") if flag in args), "")
    paths = d.Paths()
    fd = desktop.lock(paths.runtime / "app.lock")
    if fd is None:
        # The running window shows itself, on the requested page if any.
        d.atomic(paths.runtime / "show-window", page or "show")
        return 0
    try:
        root = tk.Tk(className="WhisperDictation")
        App(root, Service(paths), page)
        root.mainloop()
    finally:
        os.close(fd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
