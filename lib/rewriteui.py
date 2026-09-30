"""In-app setup for optional concise transcript drafts."""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from tkinter import ttk

import dictation as d
import rewriting


class SettingsDialog:
    """Configure a compatible text service without using a terminal."""

    DEFAULT_ENDPOINT = "http://localhost:11434/v1/chat/completions"

    def __init__(
        self,
        parent: tk.Misc,
        config: d.Config,
        paths: d.Paths,
        on_saved: Callable[[], None],
        *,
        make_after_save: bool = False,
    ) -> None:
        self.paths = paths
        self.on_saved = on_saved
        self.endpoint = tk.StringVar(
            master=parent, value=config.s("rewrite_endpoint") or self.DEFAULT_ENDPOINT
        )
        self.model = tk.StringVar(master=parent, value=config.s("rewrite_model"))
        self.api_key = tk.StringVar(master=parent)
        self.clear_key = tk.BooleanVar(master=parent, value=False)
        self.allow_remote = tk.BooleanVar(master=parent, value=config.b("rewrite_allow_remote"))
        self.error = tk.StringVar(master=parent)

        window = self.window = tk.Toplevel(parent)
        window.title("Concise drafts")
        window.resizable(False, False)
        window.transient(parent)  # type: ignore[call-overload]
        body = ttk.Frame(window, padding=20)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Make concise", style="CardHeading.TLabel").pack(anchor="w")
        ttk.Label(
            body,
            text=(
                "Choose a text model to turn a transcript into a shorter draft. "
                "Your original stays unchanged, and you review the draft before copying it."
            ),
            style="CardHint.TLabel",
            wraplength=430,
            justify="left",
        ).pack(anchor="w", pady=(3, 12))

        self.endpoint_entry = self._field(body, "Service URL", self.endpoint)
        ttk.Label(
            body,
            text="For a local service, use its chat-completions address. The default is for Ollama.",
            style="CardHint.TLabel",
            wraplength=430,
        ).pack(anchor="w", pady=(3, 8))
        self._field(body, "Text model name", self.model)
        self._field(body, "API key (optional)", self.api_key, secret=True)
        if rewriting.has_saved_key(paths):
            ttk.Label(
                body,
                text="A key is saved privately on this computer. Leave this blank to keep it.",
                style="CardHint.TLabel",
                wraplength=430,
            ).pack(anchor="w", pady=(3, 4))
            ttk.Checkbutton(
                body,
                text="Remove the saved key",
                variable=self.clear_key,
                style="Card.TCheckbutton",
            ).pack(anchor="w", pady=(0, 6))
        ttk.Label(
            body,
            text=(
                "Make concise sends the transcript to this service when you choose it. "
                "Remote services must use HTTPS and may charge you. A local server can also "
                "use a cloud model, so check how your service handles text."
            ),
            style="CardHint.TLabel",
            wraplength=430,
        ).pack(anchor="w", pady=(6, 4))
        ttk.Checkbutton(
            body,
            text="Allow sending transcripts to this remote service",
            variable=self.allow_remote,
            style="Card.TCheckbutton",
        ).pack(anchor="w")
        ttk.Label(
            body,
            textvariable=self.error,
            style="CardError.TLabel",
            wraplength=430,
            justify="left",
        ).pack(anchor="w", pady=(8, 0))
        actions = ttk.Frame(body)
        actions.pack(fill="x", pady=(14, 0))
        ttk.Button(actions, text="Cancel", command=self.close).pack(side="right")
        ttk.Button(
            actions,
            text="Save and make concise" if make_after_save else "Save settings",
            style="Primary.TButton",
            command=self.save,
        ).pack(side="right", padx=(0, 8))
        window.protocol("WM_DELETE_WINDOW", self.close)
        window.bind("<Escape>", lambda _: self.close())
        window.grab_set()
        self.endpoint_entry.focus_set()

    @staticmethod
    def _field(
        parent: ttk.Frame, caption: str, variable: tk.StringVar, secret: bool = False
    ) -> ttk.Entry:
        ttk.Label(parent, text=caption, style="Card.TLabel").pack(anchor="w", pady=(6, 2))
        entry = ttk.Entry(parent, textvariable=variable, show="•" if secret else "")
        entry.pack(fill="x")
        return entry

    def save(self) -> None:
        try:
            rewriting.save_settings(
                d.Config(self.paths),
                self.paths,
                self.endpoint.get(),
                self.model.get(),
                self.allow_remote.get(),
                self.api_key.get(),
                self.clear_key.get(),
            )
        except (d.DictationError, OSError) as exc:
            self.error.set(str(exc))
            return
        self.window.grab_release()
        self.window.destroy()
        self.on_saved()

    def close(self) -> None:
        if self.window.winfo_exists():
            self.window.grab_release()
            self.window.destroy()
