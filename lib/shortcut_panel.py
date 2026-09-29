"""The "Test it" widget for a shortcut, shared by Settings, the recorder page and the
first-run tutorial (the same on every platform; the logic is in shortcut_test.py)."""

from __future__ import annotations

import time
import tkinter as tk
from collections.abc import Callable
from tkinter import ttk

import desktop
import dictation as d
import hotkeys
import shortcut_test

POLL_MS = 400


class TestPanel:
    """A status line and buttons: "Press X now" -> "It works", or why it didn't and what
    to try next. Never leaves the person without a next step."""

    __test__ = False  # Not a test case, whatever pytest thinks of the name.

    def __init__(
        self,
        root: tk.Misc,
        paths: d.Paths,
        parent: tk.Misc,
        kind: str,
        wraplength: int = 420,
        clock: Callable[[], float] = time.time,
        platform: str | None = None,
        blocked: Callable[[hotkeys.Shortcut], str] | None = None,
    ) -> None:
        self.root, self.paths, self.kind = root, paths, kind
        self.clock = clock
        self.platform = platform or desktop.platform_name()
        self.blocked = blocked or shortcut_test.desktop_blocked(kind, self.platform)
        self.frame = ttk.Frame(parent, style="Card.TFrame")
        self.line = ttk.Label(
            self.frame, text="", style="CardHint.TLabel", wraplength=wraplength, justify="left"
        )
        self.line.pack(anchor="w")
        self.buttons = ttk.Frame(self.frame, style="Card.TFrame")
        self.buttons.pack(anchor="w", pady=(6, 0))
        self.tried: list[hotkeys.Shortcut] = []  # This session's shortcuts that were silent.
        self.shortcut: hotkeys.Shortcut | None = None
        self.started = 0.0
        self.fresh = False
        self.timer: str | None = None
        self.phase = "idle"  # idle, waiting, heard, silent

    # -- state ------------------------------------------------------------------

    def current(self) -> hotkeys.Shortcut | None:
        prefs = hotkeys.Preferences(self.paths)
        return prefs.history_shortcut() if self.kind == "history" else prefs.shortcut()

    def other(self) -> hotkeys.Shortcut | None:
        """The other feature's shortcut, which a candidate must not equal."""
        prefs = hotkeys.Preferences(self.paths)
        features = prefs.features()
        if self.kind == "history":
            return prefs.shortcut() if features.dictation else None
        return prefs.history_shortcut() if features.clipboard else None

    def start(self, fresh: bool = False) -> None:
        """Ask for a press of the saved shortcut and start listening for it."""
        self.cancel()
        self.shortcut = self.current()
        if self.shortcut is None:
            self.phase = "idle"
            self.line.configure(text="This shortcut is off.")
            self.clear_buttons()
            return
        self.started, self.fresh, self.phase = self.clock(), fresh, "waiting"
        self.line.configure(text=shortcut_test.prompt(self.shortcut, self.platform) + "…")
        self.clear_buttons()
        self.timer = self.root.after(POLL_MS, self.poll)

    def cancel(self) -> None:
        if self.timer is not None:
            try:
                self.root.after_cancel(self.timer)
            except tk.TclError:
                pass
            self.timer = None

    def poll(self) -> None:
        self.timer = None
        if not self.frame.winfo_exists() or self.shortcut is None:
            return
        result = shortcut_test.assess(
            self.paths, self.kind, self.shortcut, self.started, self.clock(), self.fresh,
            self.platform,
        )  # fmt: skip
        if result.phase == "waiting":
            self.timer = self.root.after(POLL_MS, self.poll)
            return
        heard = result.phase == "heard"
        self.phase = result.phase
        try:
            hotkeys.record_outcome(self.paths, self.kind, self.shortcut, heard)
        except OSError:
            pass  # The verdict on screen still stands.
        self.clear_buttons()
        if heard:
            self.line.configure(text=shortcut_test.heard_text(self.shortcut, self.platform))
            return
        self.tried.append(self.shortcut)
        self.line.configure(text=shortcut_test.silent_text(result.reason))
        self.offer_next()

    # -- what to do about silence -----------------------------------------------

    def offer_next(self) -> None:
        assert self.shortcut is not None
        self.button("Test again", self.retry)
        option = shortcut_test.next_candidate(
            self.kind, self.platform, self.shortcut, self.tried, self.other(), self.blocked
        )
        if option is not None:
            self.button(f"Try {option.label(self.platform)}", lambda: self.try_shortcut(option))
            return
        parts = shortcut_test.command_parts(self.kind)
        manual = shortcut_test.manual_text(self.kind, self.shortcut, self.platform, parts)
        self.line.configure(text=self.line.cget("text") + "\nNo other shortcut to try. " + manual)
        if self.platform == "linux":
            line = shortcut_test.command_line(parts)
            self.button("Copy command", lambda: self.copy(line))

    def retry(self) -> None:
        self.start(fresh=False)

    def try_shortcut(self, option: hotkeys.Shortcut) -> None:
        """Switch to `option` (the tray or menu bar registers it) and test that."""
        prefs = hotkeys.Preferences(self.paths)
        if self.kind == "history":
            prefs.save(history_shortcut=option)
        else:
            prefs.save(shortcut=option)
        self.start(fresh=True)

    def copy(self, text: str) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        for child in self.buttons.winfo_children():
            if isinstance(child, ttk.Button) and child.cget("text") == "Copy command":
                child.configure(text="Copied")

    # -- widgets ----------------------------------------------------------------

    def button(self, text: str, command: Callable[[], None]) -> ttk.Button:
        button = ttk.Button(self.buttons, text=text, command=command)
        button.pack(side="left", padx=(0, 6))
        return button

    def clear_buttons(self) -> None:
        for child in self.buttons.winfo_children():
            child.destroy()
