"""macOS menu bar app: a global shortcut starts and stops dictation.

Recording, transcription, clipboard delivery and notifications stay in
dictation.py; this process only listens for the shortcut and shows state.
Requires PyObjC (installed into the app's private environment by setup).
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import desktop
import dictation as d
import hotkeys
import objc  # type: ignore[import-not-found]
import workflow
from app_service import Service
from AppKit import (  # type: ignore[import-not-found]
    NSAlert,
    NSAlertFirstButtonReturn,
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSEvent,
    NSEventMaskKeyDown,
    NSFont,
    NSImage,
    NSMakeRect,
    NSMenu,
    NSMenuItem,
    NSStatusBar,
    NSTextField,
    NSVariableStatusItemLength,
)
from Foundation import NSObject, NSTimer  # type: ignore[import-not-found]

HERE = Path(__file__).resolve().parent


def fourcc(code: str) -> int:
    return int.from_bytes(code.encode("ascii"), "big")


class EventTypeSpec(ctypes.Structure):
    _fields_ = [("eventClass", ctypes.c_uint32), ("eventKind", ctypes.c_uint32)]


class EventHotKeyID(ctypes.Structure):
    _fields_ = [("signature", ctypes.c_uint32), ("id", ctypes.c_uint32)]


HANDLER = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)


class GlobalHotKey:
    """Carbon RegisterEventHotKey: works system-wide without Accessibility access."""

    def __init__(self, callback: Any) -> None:
        carbon = ctypes.CDLL("/System/Library/Frameworks/Carbon.framework/Carbon")
        carbon.GetApplicationEventTarget.restype = ctypes.c_void_p
        carbon.InstallEventHandler.argtypes = [
            ctypes.c_void_p,
            HANDLER,
            ctypes.c_uint32,
            ctypes.POINTER(EventTypeSpec),
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        carbon.RegisterEventHotKey.argtypes = [
            ctypes.c_uint32,
            ctypes.c_uint32,
            EventHotKeyID,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        carbon.UnregisterEventHotKey.argtypes = [ctypes.c_void_p]
        self.carbon = carbon
        self.target = carbon.GetApplicationEventTarget()
        self.ref: ctypes.c_void_p | None = None

        def pressed(_call: Any, _event: Any, _data: Any) -> int:
            callback()
            return 0

        # Keep a reference: the C side holds only a raw pointer to this thunk.
        self.handler = HANDLER(pressed)
        spec = EventTypeSpec(fourcc("keyb"), 5)  # kEventHotKeyPressed
        carbon.InstallEventHandler(self.target, self.handler, 1, ctypes.byref(spec), None, None)

    def register(self, shortcut: hotkeys.Shortcut) -> bool:
        self.unregister()
        ref = ctypes.c_void_p()
        status = self.carbon.RegisterEventHotKey(
            shortcut.mac_key_code(),
            shortcut.carbon_modifiers(),
            EventHotKeyID(fourcc("WDct"), 1),
            self.target,
            0,
            ctypes.byref(ref),
        )
        if status != 0:
            return False
        self.ref = ref
        return True

    def unregister(self) -> None:
        if self.ref is not None:
            self.carbon.UnregisterEventHotKey(self.ref)
            self.ref = None


def template(name: str, template_image: bool = True) -> Any:
    image = NSImage.alloc().initWithContentsOfFile_(str(HERE / name))
    if image is not None:
        image.setSize_((18, 18))
        image.setTemplate_(template_image)
    return image


class Controller(NSObject):  # type: ignore[misc]
    def init(self) -> Controller | None:
        self = objc.super(Controller, self).init()
        if self is None:
            return None
        self.paths = d.Paths()
        self.service = Service(self.paths)
        self.preferences = hotkeys.Preferences(self.paths)
        self.shortcut = self.preferences.shortcut()
        self.phase = ""
        return self

    # -- lifecycle --------------------------------------------------------
    def applicationDidFinishLaunching_(self, _notification: Any) -> None:
        self.item = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
        self.idle_image = template("menubar-icon.png")
        self.recording_image = template("menubar-recording.png", template_image=False)
        self.item.button().setImage_(self.idle_image)
        self.item.button().setToolTip_("Whisper Dictation")
        self.build_menu()
        self.hotkey = GlobalHotKey(self.pressed)
        self.hotkey_ok = self.hotkey.register(self.shortcut)
        hotkeys.record_status(self.paths, self.hotkey_ok)
        if not self.hotkey_ok:
            self.warn(
                "Shortcut unavailable",
                f"{self.shortcut.label()} is already used by another app. "
                "Choose a different shortcut from the menu bar icon.",
            )
        self.sync_login_item()
        self.timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.5, self, "refresh:", None, True
        )
        self.refresh_(None)
        if not self.service.completed():
            self.open_window("--setup")

    @objc.python_method
    def build_menu(self) -> None:
        menu = NSMenu.alloc().init()
        menu.setAutoenablesItems_(False)
        self.status_line = self.add(menu, "Ready", None)
        self.status_line.setEnabled_(False)
        menu.addItem_(NSMenuItem.separatorItem())
        self.toggle_item = self.add(menu, "Start Dictation", "toggle:")
        self.cancel_item = self.add(menu, "Cancel Recording", "cancel:")
        self.copy_item = self.add(menu, "Copy Last Transcript", "copyLast:")
        menu.addItem_(NSMenuItem.separatorItem())
        self.shortcut_item = self.add(menu, "", None)
        submenu = NSMenu.alloc().init()
        for index, preset in enumerate(hotkeys.PRESETS):
            entry = self.add(submenu, preset.label(), "choosePreset:")
            entry.setTag_(index)
        submenu.addItem_(NSMenuItem.separatorItem())
        self.add(submenu, "Record New Shortcut…", "recordShortcut:")
        self.shortcut_item.setSubmenu_(submenu)
        self.login_item = self.add(menu, "Open at Login", "toggleLogin:")
        self.add(menu, "Clipboard History (Clipboard+)…", "clipboardPlus:")
        self.add(menu, "Settings…", "openSettings:")
        menu.addItem_(NSMenuItem.separatorItem())
        self.add(menu, "Quit Whisper Dictation", "quit:")
        self.item.setMenu_(menu)
        self.update_shortcut_menu()

    @objc.python_method
    def add(self, menu: Any, title: str, action: str | None) -> Any:
        entry = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, "")
        if action:
            entry.setTarget_(self)
        menu.addItem_(entry)
        return entry

    # -- actions ----------------------------------------------------------
    @objc.python_method
    def pressed(self) -> None:
        if not self.service.ready():
            self.open_window("--setup")
            return
        self.run_engine()

    @objc.python_method
    def run_engine(self, *flags: str) -> None:
        # The engine owns locking, recording, notifications and the clipboard.
        subprocess.Popen(
            [sys.executable, str(HERE / "dictation.py"), *flags],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def toggle_(self, _sender: Any) -> None:
        self.pressed()

    def cancel_(self, _sender: Any) -> None:
        self.run_engine("--cancel")

    def copyLast_(self, _sender: Any) -> None:
        self.run_engine("--copy-last")

    def openSettings_(self, _sender: Any) -> None:
        self.open_window("--settings")

    @objc.python_method
    def open_window(self, page: str) -> None:
        open_app_window(page)

    def applicationShouldHandleReopen_hasVisibleWindows_(
        self, _app: Any, _has_visible_windows: bool
    ) -> bool:
        # Clicking the app in Finder or Launchpad shows the window, not nothing.
        self.open_window("")
        return False

    def choosePreset_(self, sender: Any) -> None:
        self.apply_shortcut(hotkeys.PRESETS[sender.tag()])

    def recordShortcut_(self, _sender: Any) -> None:
        self.hotkey.unregister()  # Otherwise pressing the old shortcut toggles recording.
        captured: list[hotkeys.Shortcut] = []
        alert = NSAlert.alloc().init()
        alert.setMessageText_("Press your new shortcut")
        alert.setInformativeText_(
            "Hold ⌃ Control, ⌥ Option or ⌘ Command and press a letter, number or Space — "
            "or press a function key."
        )
        alert.addButtonWithTitle_("Save")
        alert.addButtonWithTitle_("Cancel")
        field = NSTextField.labelWithString_(self.shortcut.label())
        field.setFont_(NSFont.systemFontOfSize_(20))
        field.setFrame_(NSMakeRect(0, 0, 260, 30))
        alert.setAccessoryView_(field)

        def key_down(event: Any) -> Any:
            shortcut = hotkeys.from_mac_event(
                event.keyCode(),
                int(event.modifierFlags()),
                event.charactersIgnoringModifiers() or "",
            )
            problem = shortcut.problem()
            field.setStringValue_(shortcut.label() + (f"  — {problem}" if problem else ""))
            captured[:] = [] if problem else [shortcut]
            return None  # Swallow the key so the alert does not act on it.

        monitor = NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
            NSEventMaskKeyDown, key_down
        )
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        try:
            saved = alert.runModal() == NSAlertFirstButtonReturn
        finally:
            NSEvent.removeMonitor_(monitor)
        if saved and captured:
            self.apply_shortcut(captured[0])
        else:
            self.hotkey_ok = self.hotkey.register(self.shortcut)
            hotkeys.record_status(self.paths, self.hotkey_ok)

    @objc.python_method
    def apply_shortcut(self, shortcut: hotkeys.Shortcut) -> None:
        if self.hotkey.register(shortcut):
            self.shortcut, self.hotkey_ok = shortcut, True
            self.preferences.save(shortcut=shortcut)
        else:
            self.hotkey_ok = self.hotkey.register(self.shortcut)
            self.warn(
                "Shortcut unavailable",
                f"{shortcut.label()} is already used by another app. "
                f"Keeping {self.shortcut.label()}.",
            )
        hotkeys.record_status(self.paths, self.hotkey_ok)
        self.update_shortcut_menu()

    def toggleLogin_(self, _sender: Any) -> None:
        self.preferences.save(open_at_login=not self.preferences.open_at_login())
        self.sync_login_item()

    @objc.python_method
    def sync_login_item(self) -> None:
        enabled = self.preferences.open_at_login()
        # Set by the bundle launcher; the Python interpreter is a different bundle.
        bundle = os.environ.get("WHISPER_DICTATION_BUNDLE", "")
        if bundle.endswith(".app"):
            hotkeys.set_login_item(enabled, ["/usr/bin/open", bundle])
        self.login_item.setState_(1 if enabled else 0)

    def clipboardPlus_(self, _sender: Any) -> None:
        self.service.open_clipboard_history()

    def quit_(self, _sender: Any) -> None:
        NSApplication.sharedApplication().terminate_(self)

    @objc.python_method
    def warn(self, title: str, message: str) -> None:
        alert = NSAlert.alloc().init()
        alert.setMessageText_(title)
        alert.setInformativeText_(message)
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        alert.runModal()

    # -- state ------------------------------------------------------------
    @objc.python_method
    def update_shortcut_menu(self) -> None:
        self.shortcut_item.setTitle_(f"Shortcut: {self.shortcut.label()}")
        for entry in self.shortcut_item.submenu().itemArray():
            if entry.action() == "choosePreset:":
                entry.setState_(1 if hotkeys.PRESETS[entry.tag()] == self.shortcut else 0)
        self.refresh_(None)

    @objc.python_method
    def ready(self) -> bool:
        """Setup state, re-checked every few seconds rather than on every tick."""
        now = time.monotonic()
        if now - getattr(self, "ready_at", -10.0) >= 5:
            self.ready_value, self.ready_at = self.service.ready(), now
        return bool(self.ready_value)

    def refresh_(self, _timer: Any) -> None:
        quit_request = self.paths.runtime / "menubar-quit"
        if quit_request.exists():
            quit_request.unlink(missing_ok=True)
            self.quit_(None)
            return
        try:
            current = workflow.snapshot(self.paths)
        except (d.DictationError, OSError, ValueError):
            current = {"phase": "idle", "active": False, "retained_audio": False}
        active = bool(current["active"])
        phase = str(current["phase"]) if active else "idle"
        elapsed = int(current.get("elapsed_seconds", 0))
        ready = self.ready()
        # Redraw only when something visible changed; this runs twice a second.
        view = (
            phase,
            elapsed if phase == "recording" else 0,
            self.shortcut,
            self.hotkey_ok,
            ready,
            self.paths.text.exists(),
        )
        if view == getattr(self, "view", None):
            return
        self.view = view
        label = self.shortcut.label()
        button = self.item.button()
        if phase == "recording":
            button.setImage_(self.recording_image)
            button.setTitle_(f" {elapsed // 60}:{elapsed % 60:02d}")
            self.status_line.setTitle_("Recording…")
            self.toggle_item.setTitle_(f"Stop and Transcribe    {label}")
        elif active:
            button.setImage_(self.idle_image)
            button.setTitle_(" …")
            self.status_line.setTitle_("Transcribing…")
            self.toggle_item.setTitle_("Transcribing…")
        else:
            button.setImage_(self.idle_image)
            button.setTitle_("")
            self.status_line.setTitle_(
                "Finish setup to start"
                if not ready
                else f"Press {label} anywhere to dictate"
                if self.hotkey_ok
                else f"{label} is taken — choose another shortcut"
            )
            self.toggle_item.setTitle_(f"Start Dictation    {label}")
        self.toggle_item.setEnabled_(not active or phase == "recording")
        self.cancel_item.setEnabled_(phase == "recording")
        self.copy_item.setEnabled_(not active and self.paths.text.exists())


def open_app_window(page: str = "") -> None:
    """Start the window; a running window is asked to show itself instead."""
    subprocess.Popen(
        [sys.executable, str(HERE / "app.py"), *([page] if page else [])],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def main() -> int:
    os.umask(0o077)
    if desktop.platform_name() != "macos":
        print("The menu bar app is macOS only; use your desktop's shortcut settings.")
        return 1
    paths = d.Paths()
    fd = desktop.lock(paths.runtime / "menubar.lock")
    if fd is None:
        open_app_window()  # Already running: opening it again shows the window.
        return 0
    try:
        (paths.runtime / "menubar-quit").unlink(missing_ok=True)
        app = NSApplication.sharedApplication()
        app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
        controller = Controller.alloc().init()
        app.setDelegate_(controller)
        app.run()
    finally:
        os.close(fd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
