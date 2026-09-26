"""Windows and Linux tray app: a global shortcut starts and stops dictation.

The same menu and behavior as the macOS menu bar (menubar.py). Recording,
transcription, clipboard delivery and notifications stay in dictation.py.
Requires pystray and Pillow (installed into the app's private environment).
"""

from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import desktop
import dictation as d
import hotkeys
import workflow
from app_service import Service

HERE = Path(__file__).resolve().parent
WM_HOTKEY, WM_APP = 0x0312, 0x8000


class WindowsHotKey:
    """RegisterHotKey on a dedicated message-loop thread (hotkeys are thread-bound)."""

    def __init__(self, callback: Callable[[], None]) -> None:
        self.callback = callback
        self.user32: Any = getattr(ctypes, "windll").user32
        self.kernel32: Any = getattr(ctypes, "windll").kernel32
        self.request: hotkeys.Shortcut | None = None
        self.result = False
        self.done = threading.Event()
        self.ready = threading.Event()
        self.thread_id = 0
        threading.Thread(target=self.loop, daemon=True).start()
        self.ready.wait(5)

    def loop(self) -> None:
        from ctypes import wintypes

        self.thread_id = self.kernel32.GetCurrentThreadId()
        message = wintypes.MSG()
        # Create this thread's message queue before anyone posts to it.
        self.user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 0)
        self.ready.set()
        while self.user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            if message.message == WM_HOTKEY:
                self.callback()
            elif message.message == WM_APP:
                self.user32.UnregisterHotKey(None, 1)
                shortcut = self.request
                self.result = bool(
                    shortcut
                    and self.user32.RegisterHotKey(
                        None, 1, shortcut.windows_modifiers(), shortcut.windows_key()
                    )
                )
                self.done.set()

    def register(self, shortcut: hotkeys.Shortcut | None) -> bool:
        self.request, self.result = shortcut, False
        self.done.clear()
        self.user32.PostThreadMessageW(self.thread_id, WM_APP, 0, 0)
        self.done.wait(5)
        return self.result


class GnomeHotKey:
    """GNOME runs the toggle command itself; Wayland apps cannot grab keys."""

    def __init__(self, _callback: Callable[[], None]) -> None:
        self.command = HERE.parent / "bin/dictate-toggle"

    def register(self, shortcut: hotkeys.Shortcut | None) -> bool:
        return hotkeys.gnome_shortcut(shortcut, self.command) or shortcut is None


class Tray:
    def __init__(self, pystray: Any, image: Any) -> None:
        self.pystray = pystray
        self.paths = d.Paths()
        self.service = Service(self.paths)
        self.preferences = hotkeys.Preferences(self.paths)
        self.shortcut = self.preferences.shortcut()
        self.stamp = self.preferences.stamp()
        self.hotkey_ok = False
        self.suspended = False
        self.running = False
        self.phase = "idle"
        self.state: tuple[str, int] = ("", 0)
        self.images = {
            "idle": image.open(HERE / "whisper-dictation.png"),
            "recording": image.open(HERE / "tray-recording.png"),
        }
        self.place = "system tray" if desktop.platform_name() == "windows" else "top bar"
        item, menu = pystray.MenuItem, pystray.Menu
        presets = [
            item(
                preset.label(),
                self.choose(preset),
                checked=lambda _, preset=preset: self.shortcut == preset,
                radio=True,
            )
            for preset in hotkeys.PRESETS
        ]
        self.icon = pystray.Icon(
            "whisper-dictation",
            self.images["idle"],
            hotkeys.APP_NAME,
            menu(
                item(lambda _: self.status_text(), None, enabled=False),
                menu.SEPARATOR,
                item(lambda _: self.toggle_text(), self.toggle, default=True),
                item("Cancel Recording", self.cancel, enabled=lambda _: self.phase == "recording"),
                item(
                    "Copy Last Transcript",
                    self.copy_last,
                    enabled=lambda _: self.phase == "idle" and self.paths.text.exists(),
                ),
                menu.SEPARATOR,
                item(
                    lambda _: f"Shortcut: {self.shortcut.label()}",
                    menu(*presets, menu.SEPARATOR, item("Record New Shortcut…", self.record)),
                ),
                item(
                    "Open at Login",
                    self.toggle_login,
                    checked=lambda _: self.preferences.open_at_login(),
                ),
                item(
                    "Clipboard History (Clipboard+)…", lambda: self.service.open_clipboard_history()
                ),
                item("Settings…", lambda: self.open_window("--settings")),
                menu.SEPARATOR,
                item(f"Quit {hotkeys.APP_NAME}", self.quit),
            ),
        )

    # -- actions ----------------------------------------------------------
    def pressed(self) -> None:
        if not self.service.ready():
            self.open_window("--setup")
        else:
            self.run_engine()

    def run_engine(self, *flags: str) -> None:
        subprocess.Popen(
            [hotkeys.python_for_gui(), str(HERE / "dictation.py"), *flags],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **desktop.process_options(detached=True),
        )

    def open_window(self, page: str) -> None:
        open_app_window(page)

    def toggle(self) -> None:
        self.pressed()

    def cancel(self) -> None:
        self.run_engine("--cancel")

    def copy_last(self) -> None:
        self.run_engine("--copy-last")

    def record(self) -> None:
        self.open_window("--shortcut")

    def choose(self, preset: hotkeys.Shortcut) -> Callable[[], None]:
        return lambda: self.apply(preset)

    def apply(self, shortcut: hotkeys.Shortcut) -> None:
        previous = self.shortcut
        if self.hotkey.register(shortcut):
            self.shortcut, self.hotkey_ok = shortcut, True
            self.preferences.save(shortcut=shortcut)
        else:
            self.hotkey_ok = self.hotkey.register(previous)
            self.preferences.save(shortcut=previous)
            if desktop.platform_name() == "linux" and not shutil.which("gsettings"):
                self.notify(
                    "This desktop can’t set shortcuts automatically. Assign one to "
                    "~/.local/bin/dictate-toggle in your keyboard settings."
                )
            else:
                self.notify(f"{shortcut.label()} is already in use. Keeping {previous.label()}.")
        hotkeys.record_status(self.paths, self.hotkey_ok)
        self.stamp = self.preferences.stamp()
        self.icon.update_menu()

    def toggle_login(self) -> None:
        self.preferences.save(open_at_login=not self.preferences.open_at_login())
        self.sync_login()
        self.icon.update_menu()

    def sync_login(self) -> None:
        command = [hotkeys.python_for_gui(), str(Path(__file__).resolve())]
        hotkeys.set_login_item(self.preferences.open_at_login(), command)

    def quit(self) -> None:
        self.running = False
        self.icon.stop()

    def notify(self, message: str) -> None:
        try:
            self.icon.notify(message, hotkeys.APP_NAME)
        except (NotImplementedError, OSError):
            pass

    # -- state ------------------------------------------------------------
    def status_text(self) -> str:
        if self.phase == "recording":
            return "Recording…"
        if self.phase != "idle":
            return "Transcribing…"
        if not self.service.ready():
            return "Finish setup to start"
        if not self.hotkey_ok:
            return "Set a shortcut in your desktop’s keyboard settings"
        return f"Press {self.shortcut.label()} anywhere to dictate"

    def toggle_text(self) -> str:
        label = self.shortcut.label()
        if self.phase == "recording":
            return f"Stop and Transcribe ({label})"
        return "Transcribing…" if self.phase != "idle" else f"Start Dictation ({label})"

    def started(self, icon: Any) -> None:
        icon.visible = True
        self.hotkey = (
            WindowsHotKey(self.pressed)
            if desktop.platform_name() == "windows"
            else GnomeHotKey(self.pressed)
        )
        self.hotkey_ok = self.hotkey.register(self.shortcut)
        hotkeys.record_status(self.paths, self.hotkey_ok)
        if not self.hotkey_ok and desktop.platform_name() == "windows":
            self.notify(f"{self.shortcut.label()} is in use by another app. Pick another.")
        self.sync_login()
        if not self.service.completed():
            self.open_window("--setup")
        self.running = True
        while self.running:
            self.tick()
            time.sleep(0.5)

    def tick(self) -> None:
        if (self.paths.runtime / "menubar-quit").exists():
            (self.paths.runtime / "menubar-quit").unlink(missing_ok=True)
            self.quit()
            return
        capture = self.paths.runtime / "shortcut-capture"
        capturing = capture.exists()
        if capturing and not self.suspended:
            # A window that crashed on the shortcut page leaves the flag behind.
            window = desktop.lock(self.paths.runtime / "app.lock")
            if window is not None:
                os.close(window)
                capture.unlink(missing_ok=True)
                capturing = False
        changed = self.preferences.stamp() != self.stamp
        if capturing and not self.suspended:
            # Pressing the old keys while choosing a new shortcut must not record.
            self.hotkey.register(None)
            self.suspended = True
        elif not capturing and (self.suspended or changed):
            self.suspended = False
            if changed:
                self.apply(self.preferences.shortcut())  # Chosen in the shortcut window.
            else:
                self.hotkey_ok = self.hotkey.register(self.shortcut)
                hotkeys.record_status(self.paths, self.hotkey_ok)
        try:
            current = workflow.snapshot(self.paths)
        except (d.DictationError, OSError, ValueError):
            current = {"phase": "idle", "active": False}
        phase = str(current["phase"]) if current["active"] else "idle"
        elapsed = int(current.get("elapsed_seconds", 0))
        # Only the Windows tooltip can show a running clock cheaply. On Linux each
        # icon or menu update rewrites the icon file and rebuilds the GTK menu,
        # which flickers the top bar and closes an open menu.
        clock = phase == "recording" and desktop.platform_name() == "windows"
        state = (phase, elapsed if clock else 0)
        if state == self.state:
            return
        changed = phase != self.phase
        self.state, self.phase = state, phase
        if changed:
            self.icon.icon = self.images["recording" if phase == "recording" else "idle"]
        self.icon.title = (
            f"{hotkeys.APP_NAME} — recording {elapsed // 60}:{elapsed % 60:02d}"
            if clock
            else f"{hotkeys.APP_NAME} — recording"
            if phase == "recording"
            else hotkeys.APP_NAME
        )
        if changed:
            self.icon.update_menu()


def open_app_window(page: str = "") -> None:
    """Start the window; a running window is asked to show itself instead."""
    subprocess.Popen(
        [hotkeys.python_for_gui(), str(HERE / "app.py"), *([page] if page else [])],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **desktop.process_options(detached=True),
    )


def main() -> int:
    os.umask(0o077)
    if desktop.platform_name() == "macos":
        print("On macOS, run menubar.py.")
        return 1
    paths = d.Paths()
    fd = desktop.lock(paths.runtime / "menubar.lock")
    if fd is None:
        # Already running: opening the launcher again should show the window.
        open_app_window()
        return 0
    import pystray  # type: ignore[import-not-found]
    from PIL import Image  # type: ignore[import-not-found, unused-ignore]

    try:
        (paths.runtime / "menubar-quit").unlink(missing_ok=True)
        tray = Tray(pystray, Image)
        tray.icon.run(setup=tray.started)
    finally:
        os.close(fd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
