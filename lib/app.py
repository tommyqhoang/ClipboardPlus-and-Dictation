"""Whisper Dictation desktop window and first-run walkthrough."""

from __future__ import annotations

import concurrent.futures
import os
import subprocess
import tkinter as tk
from collections.abc import Callable
from tkinter import filedialog, messagebox, ttk
from typing import Any

import desktop
import dictation as d
import workflow
from app_service import Service


class App:
    def __init__(self, root: tk.Tk, service: Service) -> None:
        self.root, self.service = root, service
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self.pending: concurrent.futures.Future[Any] | None = None
        self.done: Callable[[Any], None] = lambda value: None
        self.page = ""
        self.closing = False
        self.last_text = ""
        self.buttons: list[ttk.Button] = []
        self.root.title("Whisper Dictation")
        width = min(760, max(360, self.root.winfo_screenwidth() - 80))
        height = min(700, max(360, self.root.winfo_screenheight() - 100))
        self.wraplength = max(260, width - 100)
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(min(520, width), min(420, height))
        self.root.configure(background="#f5f7fa")
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("TFrame", background="#f5f7fa")
        style.configure(
            "TLabel", background="#f5f7fa", foreground="#152737", font=("TkDefaultFont", 12)
        )
        style.configure("Title.TLabel", font=("TkDefaultFont", 25, "bold"))
        style.configure("Hint.TLabel", foreground="#536777", font=("TkDefaultFont", 11))
        style.configure("TButton", font=("TkDefaultFont", 12), padding=(14, 10))
        style.configure("Primary.TButton", background="#126b65", foreground="white")
        style.map("Primary.TButton", background=[("active", "#0d5651"), ("disabled", "#8baba8")])
        container = ttk.Frame(root)
        container.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(
            container, background="#f5f7fa", borderwidth=0, highlightthickness=0
        )
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.frame = ttk.Frame(self.canvas, padding=30)
        self.frame_window = self.canvas.create_window((0, 0), window=self.frame, anchor="nw")
        self.frame.bind("<Configure>", self.resize_scroll_region)
        self.canvas.bind("<Configure>", self.resize_content)
        self.status = tk.StringVar(value="")
        self.language = tk.StringVar(value="English")
        self.device = tk.StringVar(value="default")
        self.model = tk.StringVar(value="")
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        if service.completed():
            self.home()
        else:
            self.welcome()
        self.timer = self.root.after(150, self.poll)

    def resize_scroll_region(self, event: tk.Event[Any]) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def resize_content(self, event: tk.Event[Any]) -> None:
        self.canvas.itemconfigure(self.frame_window, width=event.width)

    def reset(self, page: str, title: str, subtitle: str) -> None:
        self.page = page
        self.buttons = []
        for child in self.frame.winfo_children():
            child.destroy()
        self.canvas.yview_moveto(0)
        ttk.Label(self.frame, text="WHISPER DICTATION", style="Hint.TLabel").pack(
            anchor="w", pady=(0, 18)
        )
        ttk.Label(self.frame, text=title, style="Title.TLabel").pack(anchor="w", pady=(0, 10))
        ttk.Label(self.frame, text=subtitle, wraplength=self.wraplength, style="Hint.TLabel").pack(
            anchor="w", pady=(0, 22)
        )
        self.status.set("")

    def button(self, text: str, command: Callable[[], None], primary: bool = False) -> ttk.Button:
        button = ttk.Button(
            self.frame,
            text=text,
            command=command,
            style="Primary.TButton" if primary else "TButton",
        )
        button.pack(fill="x", pady=5)
        self.buttons.append(button)
        return button

    def footer(self) -> None:
        self.progress = ttk.Progressbar(self.frame, mode="indeterminate")
        self.status_label = ttk.Label(
            self.frame,
            textvariable=self.status,
            wraplength=self.wraplength,
            style="Hint.TLabel",
        )
        self.status_label.pack(anchor="w")

    def welcome(self) -> None:
        self.reset(
            "welcome",
            "Your voice. Your words.",
            "Welcome! Let’s get you ready to dictate without learning any commands.",
        )
        ttk.Label(
            self.frame,
            text="1   Choose your language and microphone\n\n2   Get a free local speech model\n\n3   Record, stop, and paste anywhere",
            wraplength=self.wraplength,
        ).pack(anchor="w", pady=18)
        ttk.Label(
            self.frame,
            text="Local transcription runs on your computer. No account or subscription. We won’t turn on your microphone until you press Record.",
            wraplength=self.wraplength,
            style="Hint.TLabel",
        ).pack(anchor="w", pady=18)
        self.button("Get started", self.begin_setup, True)
        self.footer()

    def begin_setup(self) -> None:
        if self.service.ready():
            self.tutorial()
        else:
            self.settings()

    def settings(self) -> None:
        self.reset(
            "settings",
            "Let’s set things up",
            "Choose local transcription settings. You can change these later inside the app.",
        )
        config = d.Config(self.service.paths)
        self.language.set(
            "English" if config.s("language") == "en" else "Multilingual / auto-detect"
        )
        self.device.set(config.s("device"))
        self.model.set(config.s("model") if os.path.isfile(config.s("model")) else "")
        ttk.Label(self.frame, text="Language").pack(anchor="w")
        ttk.Combobox(
            self.frame,
            textvariable=self.language,
            state="readonly",
            values=("English", "Multilingual / auto-detect"),
        ).pack(fill="x", pady=(5, 14))
        ttk.Label(self.frame, text="Microphone").pack(anchor="w")
        self.device_picker = ttk.Combobox(self.frame, textvariable=self.device, values=("default",))
        self.device_picker.pack(fill="x", pady=5)
        self.button("Find microphones", self.find_microphones)
        ttk.Label(
            self.frame,
            text="Speech model — leave blank for a verified free download (148 MB)",
            style="Hint.TLabel",
            wraplength=self.wraplength,
        ).pack(anchor="w", pady=(14, 5))
        ttk.Entry(self.frame, textvariable=self.model).pack(fill="x")
        self.button("Use an existing model file…", self.choose_model)
        self.button("Save and continue", self.prepare, True)
        self.footer()

    def choose_model(self) -> None:
        path = filedialog.askopenfilename(
            parent=self.root,
            title="Choose a whisper.cpp model",
            filetypes=[("Whisper GGML model", "*.bin")],
        )
        if path:
            self.model.set(path)

    def find_microphones(self) -> None:
        self.submit(
            self.service.microphones,
            self.show_microphones,
            "Looking for microphones. Allow access if your system asks.",
        )

    def show_microphones(self, devices: list[str]) -> None:
        self.device_picker.configure(values=devices)
        if self.device.get() not in devices:
            self.device.set(devices[0])
        self.status.set("Choose your microphone above.")

    def prepare(self) -> None:
        language = "en" if self.language.get() == "English" else "auto"
        device, model = self.device.get(), self.model.get()
        self.submit(
            lambda: self.service.prepare(language, device, model),
            lambda _: self.tutorial(),
            "Preparing your speech model. A first download may take a few minutes. Nothing is recording.",
        )

    def tutorial(self) -> None:
        self.reset("tutorial", "You’re ready to speak", "Here’s all you need to know.")
        paste = "Command + V" if desktop.platform_name() == "macos" else "Ctrl + V"
        ttk.Label(
            self.frame,
            text=f"1   Press Record and speak naturally.\n\n2   Press Stop when you’re finished.\n\n3   Your words are copied. Paste with {paste}.\n\nSay “new paragraph” to start a new paragraph.",
            wraplength=self.wraplength,
        ).pack(anchor="w", pady=18)
        ttk.Label(
            self.frame,
            text="Try: “Hello world. New paragraph. This is my first dictation.”\n\nIf transcription fails, use Retry. Cancel stops and discards an active recording. You can replay this guide from Help.",
            wraplength=self.wraplength,
            style="Hint.TLabel",
        ).pack(anchor="w", pady=18)
        self.button("Open dictation", self.finish_setup, True)
        self.footer()

    def finish_setup(self) -> None:
        self.submit(self.service.complete, lambda _: self.home(), "Saving your setup…")

    def home(self) -> None:
        self.reset(
            "home",
            "What’s on your mind?",
            "Press Record, speak, then Stop. Your transcript is copied for you to paste into any app.",
        )
        controls = ttk.Frame(self.frame)
        controls.pack(fill="x", pady=5)
        self.record = ttk.Button(
            controls, text="Record", command=lambda: self.action("toggle"), style="Primary.TButton"
        )
        self.record.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.cancel = ttk.Button(
            controls, text="Cancel recording", command=lambda: self.action("cancel")
        )
        self.cancel.pack(side="left")
        self.buttons.extend([self.record, self.cancel])
        self.transcript = tk.Text(
            self.frame,
            height=7,
            wrap="word",
            font=("TkDefaultFont", 13),
            background="white",
            foreground="#152737",
            relief="flat",
            padx=14,
            pady=14,
        )
        self.transcript.pack(fill="both", expand=True, pady=12)
        self.transcript.configure(state="disabled")
        self.last_text = ""
        self.copy = self.button("Copy transcript", lambda: self.action("copy"))
        recovery = ttk.Frame(self.frame)
        recovery.pack(fill="x")
        self.retry = ttk.Button(
            recovery, text="Retry saved recording", command=lambda: self.action("transcribe")
        )
        self.retry.pack(side="left", expand=True, fill="x", padx=(0, 5))
        self.discard = ttk.Button(
            recovery, text="Discard saved recording", command=self.discard_audio
        )
        self.discard.pack(side="left", expand=True, fill="x")
        self.buttons.extend([self.retry, self.discard])
        navigation = ttk.Frame(self.frame)
        navigation.pack(fill="x", pady=8)
        self.settings_button = ttk.Button(navigation, text="Settings", command=self.settings)
        self.settings_button.pack(side="left", expand=True, fill="x", padx=(0, 5))
        self.help_button = ttk.Button(navigation, text="Help / walkthrough", command=self.tutorial)
        self.help_button.pack(side="left", expand=True, fill="x")
        self.buttons.extend([self.settings_button, self.help_button])
        self.footer()

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
        self.progress.pack(fill="x", pady=(16, 6), before=self.status_label)
        self.progress.start(15)
        self.done = done
        self.pending = self.executor.submit(work)

    def refresh(self) -> None:
        current = workflow.snapshot(self.service.paths)
        active = bool(current["active"])
        phase = str(current["phase"])
        recording = active and phase == "recording"
        retained = current["retained_audio"]
        message = workflow.display_text(str(current.get("message", ""))).strip()
        self.record.configure(text="Stop and transcribe" if recording else "Record")
        self.record.state(
            ["!disabled"] if recording or (not active and not retained) else ["disabled"]
        )
        self.cancel.state(["!disabled"] if recording else ["disabled"])
        for button in (self.retry, self.discard):
            button.state(["!disabled"] if retained and not active else ["disabled"])
        for button in (self.settings_button, self.help_button):
            button.state(["disabled"] if active else ["!disabled"])
        self.copy.state(["!disabled"] if self.service.paths.text.exists() else ["disabled"])
        if active:
            self.status.set(
                f"{'Recording' if recording else 'Transcribing'} · {int(current.get('elapsed_seconds', 0))}s"
                + (f" — {message}" if message else "")
            )
        elif phase in ("error", "interrupted"):
            self.status.set(
                message
                or "The session stopped unexpectedly. Retry the saved recording or check Settings."
            )
        elif retained:
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
        source = (
            self.service.paths.preview
            if active and self.service.paths.preview.exists()
            else self.service.paths.text
        )
        text = source.read_text(encoding="utf-8") if source.exists() else ""
        if text != self.last_text:
            self.transcript.configure(state="normal")
            self.transcript.delete("1.0", "end")
            self.transcript.insert("1.0", text)
            self.transcript.configure(state="disabled")
            self.last_text = text
        if self.closing and not active:
            self.destroy()

    def poll(self) -> None:
        try:
            activation = self.service.paths.runtime / "show-window"
            if activation.exists():
                activation.unlink()
                self.root.deiconify()
                self.root.lift()
            if self.pending is not None and self.pending.done():
                future, self.pending = self.pending, None
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

    def destroy(self) -> None:
        self.page = "closed"
        self.root.after_cancel(self.timer)
        self.done = lambda _: None
        self.executor.shutdown(wait=True)
        self.root.destroy()


def main() -> int:
    os.umask(0o077)
    paths = d.Paths()
    fd = desktop.lock(paths.runtime / "app.lock")
    if fd is None:
        d.atomic(paths.runtime / "show-window", "show")
        return 0
    try:
        root = tk.Tk()
        App(root, Service(paths))
        root.mainloop()
    finally:
        os.close(fd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
