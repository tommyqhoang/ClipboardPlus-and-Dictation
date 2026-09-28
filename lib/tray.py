"""Windows and Linux tray app: a global shortcut starts and stops dictation.

The same menu and behavior as the macOS menu bar (menubar.py). Recording,
transcription, clipboard delivery and notifications stay in dictation.py.
Requires pystray and Pillow (installed into the app's private environment).
"""

from __future__ import annotations

import ctypes
import os
import sqlite3
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import clipcontrol
import clipstore
import desktop
import dictation as d
import hotkeys
import telemetry
import updates
import workflow
from app_service import Service

HERE = Path(__file__).resolve().parent
WM_HOTKEY, WM_APP = 0x0312, 0x8000
MENU_ROWS = 8  # Recent copies listed in the menu, like the macOS popover.


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
                # None means "turn it off", which this unregister just did: that is
                # success, not failure (matches GnomeHotKey.register(None)).
                self.result = shortcut is None or bool(
                    self.user32.RegisterHotKey(
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
    """GNOME runs the command itself; Wayland apps cannot grab keys."""

    def __init__(
        self,
        _callback: Callable[[], None],
        command: Path | list[str] | None = None,
        path: str = hotkeys.GNOME_PATH,
        name: str = hotkeys.DICTATION_SHORTCUT_NAME,
    ) -> None:
        # bin/dictate-toggle only exists for a source install; a packaged build has
        # no such script, so its default goes through the bundled "dictation" entry
        # instead — persistent_relaunch() so it stays runnable after an AppImage's
        # mount point disappears.
        self.command = command or (
            desktop.persistent_relaunch("dictation")
            if desktop.frozen_root() is not None
            else desktop.install_prefix(HERE / "tray.py") / "bin/dictate-toggle"
        )
        self.path, self.name = path, name
        self.conflict: hotkeys.Conflict | None = None  # Who else has these keys.

    def register(self, shortcut: hotkeys.Shortcut | None) -> bool:
        bound = hotkeys.gnome_shortcut(shortcut, self.command, path=self.path, name=self.name)
        self.conflict = hotkeys.gnome_conflict(shortcut, self.path) if bound and shortcut else None
        return bound or shortcut is None


class Tray:
    def __init__(self, pystray: Any, image: Any) -> None:
        self.pystray = pystray
        self.paths = d.Paths()
        self.service = Service(self.paths)
        self.preferences = hotkeys.Preferences(self.paths)
        self.clip = clipcontrol.ClipboardControl(self.paths, self.preferences)
        self.clip.changed()  # The first look is not a change.
        self.dictation_registered = False
        self.history: hotkeys.Shortcut | None = None  # The history shortcut registered now.
        self.history_synced = False
        self.shortcut = self.preferences.shortcut()
        self.stamp = self.preferences.stamp()
        self.hotkey_ok = False
        self.announced: hotkeys.Conflict | None = None  # Told once per conflict.
        self.suspended = False
        self.running = False
        self.phase = "idle"
        self.state: tuple[str, int] = ("", 0)
        self.update: dict[str, str] | None = None  # A newer release to offer.
        self.update_checked = False
        self.update_checking = False
        self.update_done = False
        self.update_manual = False
        self.store: clipstore.Store | None = None
        self.rows: clipstore.Items = []
        self.rows_stamp: tuple[int, float] | None = None  # What the menu rows show now.
        self.images = {
            "idle": image.open(HERE / "whisper-dictation.png"),
            "recording": image.open(HERE / "tray-recording.png"),
        }
        self.place = "system tray" if desktop.platform_name() == "windows" else "top bar"
        item, menu = pystray.MenuItem, pystray.Menu

        def dictation_on(_: Any) -> bool:
            return self.clip.features().dictation

        def clipboard_on(_: Any) -> bool:
            return self.clip.features().clipboard

        recent = [
            item(
                lambda _, index=index: self.row_text(index),
                lambda index=index: self.copy_row(index),
                visible=lambda _, index=index: clipboard_on(None) and index < len(self.rows),
            )
            for index in range(MENU_ROWS)
        ]

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
            # Kept short: everyday actions on top, rarely changed options under More.
            menu(
                item(lambda _: self.status_text(), None, enabled=False),
                menu.SEPARATOR,
                *recent,
                item(
                    "Nothing copied yet.",
                    None,
                    enabled=False,
                    visible=lambda _: self.clip.features().clipboard and not self.rows,
                ),
                item(
                    "Turn on Clipboard history in Settings to see recent copies here.",
                    lambda: self.open_window("--settings"),
                    visible=lambda _: not self.clip.features().clipboard,
                ),
                menu.SEPARATOR,
                item(
                    lambda _: self.toggle_text(),
                    self.toggle,
                    default=True,
                    visible=dictation_on,
                ),
                item(
                    "Cancel Recording",
                    self.cancel,
                    visible=lambda _: dictation_on(None) and self.phase == "recording",
                ),
                item(
                    "Copy Last Transcript",
                    self.copy_last,
                    enabled=lambda _: self.phase == "idle" and self.paths.text.exists(),
                    visible=dictation_on,
                ),
                item(
                    lambda _: self.history_text(),
                    self.open_history,
                    default=True,
                    visible=lambda _: self.clip.features().clipboard and not dictation_on(None),
                ),
                item(
                    lambda _: self.history_text(),
                    self.open_history,
                    visible=lambda _: self.clip.features().clipboard and dictation_on(None),
                ),
                item(
                    "Search Clipboard History…",
                    self.open_history,
                    visible=clipboard_on,
                ),
                item(
                    "Clear Clipboard History…",
                    self.open_clear_history,
                    visible=clipboard_on,
                ),
                item(
                    "Pause Clipboard Capture",
                    menu(
                        item("For 1 Hour", lambda: self.clip.pause(3600)),
                        item("Until I Resume", lambda: self.clip.pause(None)),
                    ),
                    visible=lambda _: self.clip.features().clipboard and not self.clip.paused(),
                ),
                item(
                    "Resume Clipboard Capture",
                    self.clip.resume,
                    visible=lambda _: self.clip.features().clipboard and self.clip.paused(),
                ),
                menu.SEPARATOR,
                item(
                    lambda _: self.update_text(),
                    self.update_now,
                ),
                item("Settings…", lambda: self.open_window("--settings")),
                item(
                    "More",
                    menu(
                        item(
                            lambda _: f"Shortcut: {self.shortcut.label()}",
                            menu(
                                *presets, menu.SEPARATOR, item("Record New Shortcut…", self.record)
                            ),
                            visible=dictation_on,
                        ),
                        item(
                            "Open at Login",
                            self.toggle_login,
                            checked=lambda _: self.preferences.open_at_login(),
                        ),
                        item("Clipboard+ Website…", lambda: self.service.open_clipboard_website()),
                    ),
                ),
                item(f"Quit {hotkeys.APP_NAME}", self.quit),
            ),
        )
        self.steady_icon_files()

    def steady_icon_files(self) -> None:
        """One fixed file per tray image, never deleted.

        On Linux pystray writes every icon change to a fresh temp file and deletes
        the previous one; GNOME's top bar sometimes reads the deleted path ("Failed
        to recognize image format") and the recording icon never shows.
        """
        icon = self.icon
        if not hasattr(icon, "_update_fs_icon"):
            return  # Windows draws the image directly.
        files = {}
        for name, picture in self.images.items():
            path = self.paths.runtime / f"tray-{name}.png"
            picture.save(path, "PNG")
            files[id(picture)] = str(path)

        def update() -> None:
            icon._icon_path = files.get(id(icon.icon), files[id(self.images["idle"])])
            icon._icon_valid = True

        icon._remove_fs_icon()  # The temp file pystray already wrote.
        icon._update_fs_icon = update
        icon._remove_fs_icon = lambda: None
        icon.icon = self.images["idle"]  # Point the indicator at the steady file.

    # -- actions ----------------------------------------------------------
    def pressed(self) -> None:
        if not self.service.ready():
            self.open_window("--setup")
        else:
            self.run_engine()

    def run_engine(self, *flags: str) -> None:
        subprocess.Popen(
            desktop.relaunch("dictation", *flags),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **desktop.process_options(detached=True),
        )

    def open_window(self, page: str) -> None:
        open_app_window(page)

    def open_history(self) -> None:
        if self.clip.features().clipboard:
            self.open_window("--clipboard")

    def open_clear_history(self) -> None:
        if self.clip.features().clipboard:
            self.open_window("--clipboard-clear")

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
            if desktop.platform_name() == "linux":
                self.notify(
                    "Couldn’t save the shortcut in this desktop’s settings. Assign one to "
                    "~/.local/bin/dictate-toggle in your keyboard settings."
                )
            else:
                self.notify(f"{shortcut.label()} is already in use. Keeping {previous.label()}.")
        self.record_dictation()
        self.stamp = self.preferences.stamp()
        self.icon.update_menu()

    def record_dictation(self) -> None:
        """Tell the window whether the shortcut works, and who else uses its keys."""
        conflict = getattr(self.hotkey, "conflict", None)
        hotkeys.record_status(self.paths, self.hotkey_ok, conflict=conflict)
        announced, self.announced = self.announced, conflict
        if conflict is not None and conflict != announced:
            telemetry.event("shortcut_conflict", kind="custom" if conflict.path else "desktop")
            self.notify(
                f"{self.shortcut.label()} is also used by {conflict.name}, which gets it first. "
                f"Open {hotkeys.APP_NAME} to fix it."
            )

    def toggle_login(self) -> None:
        self.preferences.save(open_at_login=not self.preferences.open_at_login())
        self.sync_login()
        self.icon.update_menu()

    def sync_login(self) -> None:
        hotkeys.set_login_item(
            self.preferences.open_at_login(), desktop.persistent_relaunch("tray")
        )

    # -- updates ----------------------------------------------------------
    def update_text(self) -> str:
        if self.update is not None:
            return f"Update to {self.update['version']}…"
        return "Checking for Updates…" if self.update_checking else "Check for Updates…"

    def start_update_check(self, force: bool = False) -> None:
        """Look for a newer release off the tray's thread; clicks always check."""
        if self.update_checking or (self.update_checked and not force):
            return
        self.update_checked = True
        self.update_checking = True
        self.update_done = False
        self.update_manual = force
        self.update_result: dict[str, str] | updates.UpdateError | None = None
        if force:
            self.icon.update_menu()

        def look() -> None:
            try:
                found: dict[str, str] | updates.UpdateError | None = updates.check(
                    self.paths, force=force
                )
            except updates.UpdateError as exc:
                found = exc
            except Exception:  # noqa: BLE001 - a failed check must never touch the tray.
                found = updates.UpdateError("Couldn’t check for updates. Try again shortly.")
            self.update_result = found  # Picked up by tick(), on the tray's thread.
            self.update_done = True

        threading.Thread(target=look, daemon=True).start()

    def collect_update(self) -> None:
        """Adopt a finished update check (called from tick, never a worker thread)."""
        if not self.update_done:
            return
        found, self.update_result = self.update_result, None
        manual = self.update_manual
        self.update_checking = self.update_done = self.update_manual = False
        if isinstance(found, updates.UpdateError):
            if manual:
                self.notify(str(found))
        elif found is None:
            if manual:
                self.notify(
                    "No published update is available yet."
                    if updates.read_state(self.paths).get("published") is False
                    else f"Clipboard+ {desktop.APP_VERSION} is up to date."
                )
        if found is not None and self.update is None:
            if isinstance(found, dict):
                self.update = found
                self.notify(f"Version {found['version']} is available. See the tray menu.")
        self.icon.update_menu()

    def update_now(self) -> None:
        """Install the offered release; the updater process reports how it went."""
        found = self.update
        if found is None:
            self.start_update_check(force=True)
            return
        self.update = None
        self.icon.update_menu()
        self.notify(f"Updating to {found['version']}… Clipboard+ stays usable while it downloads.")
        try:
            updates.start_updater(found["version"], found["url"])
        except OSError:
            self.update = found
            self.notify("The update could not start. Try again, or re-run the installer.")

    def quit(self) -> None:
        self.clip.stop()
        self.running = False
        self.icon.stop()

    def notify(self, message: str) -> None:
        try:
            self.icon.notify(message, hotkeys.APP_NAME)
        except (NotImplementedError, OSError):
            pass

    # -- recent copies -----------------------------------------------------
    def open_store(self) -> clipstore.Store | None:
        if self.store is None:
            try:
                self.store = clipstore.Store(self.paths.clipboard)
            except (clipstore.StoreError, OSError) as exc:
                telemetry.capture(exc, level="warning", stage="tray_store")
        return self.store

    def refresh_rows(self) -> bool:
        """List the newest clips when the history changed; False when it did not.

        A cheap stamp (row count plus newest update) keeps the GTK menu from being
        rebuilt on every half-second tick, which would flicker the top bar.
        """
        if not self.clip.features().clipboard:
            if self.rows:
                self.rows, self.rows_stamp = [], None
                return True
            return False
        store = self.open_store()
        if store is None:
            return False
        try:
            stamp = store.stamp()
            if stamp == self.rows_stamp:
                return False
            self.rows = store.list(limit=MENU_ROWS)
            self.rows_stamp = stamp
            return True
        except sqlite3.Error:
            return False  # A busy or briefly missing database is not worth a crash.

    def row_text(self, index: int) -> str:
        return clipcontrol.preview_text(self.rows[index]) if index < len(self.rows) else ""

    def copy_row(self, index: int) -> None:
        if not self.clip.features().clipboard or index >= len(self.rows) or self.store is None:
            return
        clip = self.rows[index]
        try:
            self.service.copy_item(clip, self.store)
        except d.DictationError as exc:
            self.notify(str(exc))
            return
        telemetry.event("clipboard_copy", kind=clip.kind, favorite=clip.favorite)
        self.notify("Copied. Paste it anywhere.")

    # -- state ------------------------------------------------------------
    def history_text(self) -> str:
        shortcut = self.clip.history_shortcut()
        return f"Clipboard History…  ({shortcut.label()})" if shortcut else "Clipboard History…"

    def status_text(self) -> str:
        if not self.clip.features().dictation:
            return self.clip.status_line() or "Clipboard history is off"
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
        if self.clip.features().dictation:
            self.hotkey_ok = self.hotkey.register(self.shortcut)
            self.dictation_registered = True
            self.record_dictation()
            if not self.hotkey_ok and desktop.platform_name() == "windows":
                self.notify(f"{self.shortcut.label()} is in use by another app. Pick another.")
        self.history_key = (
            WindowsHotKey(self.open_history)
            if desktop.platform_name() == "windows"
            else GnomeHotKey(
                self.open_history,
                hotkeys.history_command(HERE),
                hotkeys.GNOME_HISTORY_PATH,
                f"{hotkeys.APP_NAME}: clipboard history",
            )
        )
        self.sync_history_shortcut()
        self.sync_login()
        self.start_update_check()
        features = self.clip.features()
        telemetry.event(
            "tray_start",
            dictation=features.dictation,
            clipboard=features.clipboard,
            setup_complete=self.service.completed(),
            open_at_login=self.preferences.open_at_login(),
            default_shortcut=self.shortcut == hotkeys.DEFAULT,
            shortcut_ok=self.hotkey_ok,
        )
        if not self.service.completed():
            self.open_window("--setup")
        self.running = True
        while self.running:
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - one failed tick must not freeze the icon.
                telemetry.capture(exc, stage="tray_tick")
            time.sleep(0.5)

    def sync_dictation_shortcut(self) -> None:
        """Only the dictation feature owns a global shortcut; follow the chosen features."""
        wanted = self.clip.features().dictation
        if wanted and not self.dictation_registered:
            self.hotkey_ok = self.hotkey.register(self.shortcut)
            self.dictation_registered = True
            self.record_dictation()
        elif not wanted and self.dictation_registered:
            self.hotkey.register(None)
            self.dictation_registered = False

    def sync_history_shortcut(self) -> None:
        """Register the clipboard history shortcut the user chose (none while Clipboard is off)."""
        wanted = self.clip.history_shortcut()
        if self.history_synced and wanted == self.history:
            return
        self.history, self.history_synced = wanted, True
        ok = self.history_key.register(wanted)
        conflict = getattr(self.history_key, "conflict", None)
        hotkeys.record_status(self.paths, ok, hotkeys.HISTORY_STATUS, conflict)
        if not ok and wanted is not None and desktop.platform_name() == "windows":
            self.notify(f"{wanted.label()} is in use by another app. Choose another in Settings.")

    def tick(self) -> None:
        self.clip.supervise()
        self.start_update_check()
        self.collect_update()
        if self.clip.changed():
            self.sync_dictation_shortcut()
            self.sync_history_shortcut()
            self.icon.update_menu()
        if self.refresh_rows():
            self.icon.update_menu()
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
            was_suspended, self.suspended = self.suspended, False
            chosen = self.preferences.shortcut()
            if changed and chosen != self.shortcut:
                self.apply(chosen)  # Chosen in the shortcut window.
            elif was_suspended and self.clip.features().dictation:
                # The shortcut window closed without a new choice: use the old one again.
                self.hotkey_ok = self.hotkey.register(self.shortcut)
                self.record_dictation()
            self.stamp = self.preferences.stamp()
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
        desktop.relaunch("app", *([page] if page else [])),
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
    try:
        if not desktop.has_display():
            print("No desktop session to show the tray icon in; it starts at your next login.")
            return 1
        telemetry.install("tray")
        try:
            import pystray  # type: ignore[import-not-found]
        except Exception as exc:  # noqa: BLE001 - pystray connects to the display on import.
            if type(exc).__name__ not in ("DisplayNameError", "DisplayConnectionError"):
                raise
            print("The tray icon can't reach the display; it starts at your next login.")
            return 1
        from PIL import Image  # type: ignore[import-not-found, unused-ignore]

        (paths.runtime / "menubar-quit").unlink(missing_ok=True)
        tray = Tray(pystray, Image)
        tray.icon.run(setup=tray.started)
    finally:
        os.close(fd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
