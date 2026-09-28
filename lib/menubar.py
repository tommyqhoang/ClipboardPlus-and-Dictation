"""macOS menu bar app: a global shortcut starts and stops dictation.

Recording, transcription, clipboard delivery and notifications stay in
dictation.py; this process only listens for the shortcut and shows state.
Requires PyObjC (installed into the app's private environment by setup).
"""

from __future__ import annotations

import ctypes
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import clipcontrol
import clipstore
import desktop
import dictation as d
import hotkeys
import objc  # type: ignore[import-not-found]
import telemetry
import workflow
from app_service import Service
from AppKit import (  # type: ignore[import-not-found]
    NSAlert,
    NSAlertFirstButtonReturn,
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSBezelBorder,
    NSButton,
    NSColor,
    NSImage,
    NSImageScaleProportionallyUpOrDown,
    NSImageView,
    NSMakeRect,
    NSMakeSize,
    NSMaxYEdge,
    NSPopover,
    NSPopoverBehaviorTransient,
    NSScrollView,
    NSSearchField,
    NSStatusBar,
    NSTableColumn,
    NSTableView,
    NSTableViewUniformColumnAutoresizingStyle,
    NSTextAlignmentCenter,
    NSTextField,
    NSVariableStatusItemLength,
    NSView,
    NSViewController,
    NSViewHeightSizable,
    NSViewWidthSizable,
)
from Foundation import NSIndexSet, NSObject, NSTimer  # type: ignore[import-not-found]

HERE = Path(__file__).resolve().parent
POPOVER_WIDTH = 380.0
POPOVER_HEIGHT = 420.0
POPOVER_ROWS = 8
POPOVER_PREVIEW_CHARS = 60
ROW_HEIGHT = 40.0
THUMB_SIZE = 28.0


def fourcc(code: str) -> int:
    return int.from_bytes(code.encode("ascii"), "big")


class EventTypeSpec(ctypes.Structure):
    _fields_ = [("eventClass", ctypes.c_uint32), ("eventKind", ctypes.c_uint32)]


class EventHotKeyID(ctypes.Structure):
    _fields_ = [("signature", ctypes.c_uint32), ("id", ctypes.c_uint32)]


DICTATION_ID, HISTORY_ID = 1, 2
HANDLER = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)


class GlobalHotKey:
    """Carbon RegisterEventHotKey: works system-wide without Accessibility access.

    One event handler serves every shortcut; each is told apart by its id.
    """

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
        carbon.GetEventParameter.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        self.carbon = carbon
        self.target = carbon.GetApplicationEventTarget()
        self.callbacks: dict[int, Any] = {DICTATION_ID: callback}
        self.refs: dict[int, ctypes.c_void_p] = {}

        def pressed(_call: Any, event: Any, _data: Any) -> int:
            which = EventHotKeyID()
            status = carbon.GetEventParameter(
                event,
                fourcc("----"),  # kEventParamDirectObject
                fourcc("hkid"),  # typeEventHotKeyID
                None,
                ctypes.sizeof(which),
                None,
                ctypes.byref(which),
            )
            action = self.callbacks.get(which.id if status == 0 else DICTATION_ID)
            if action is not None:
                action()
            return 0

        # Keep a reference: the C side holds only a raw pointer to this thunk.
        self.handler = HANDLER(pressed)
        spec = EventTypeSpec(fourcc("keyb"), 5)  # kEventHotKeyPressed
        carbon.InstallEventHandler(self.target, self.handler, 1, ctypes.byref(spec), None, None)

    def on(self, hotkey_id: int, callback: Any) -> None:
        self.callbacks[hotkey_id] = callback

    def register(self, shortcut: hotkeys.Shortcut, hotkey_id: int = DICTATION_ID) -> bool:
        self.unregister(hotkey_id)
        ref = ctypes.c_void_p()
        status = self.carbon.RegisterEventHotKey(
            shortcut.mac_key_code(),
            shortcut.carbon_modifiers(),
            EventHotKeyID(fourcc("WDct"), hotkey_id),
            self.target,
            0,
            ctypes.byref(ref),
        )
        if status != 0:
            return False
        self.refs[hotkey_id] = ref
        return True

    def unregister(self, hotkey_id: int = DICTATION_ID) -> None:
        ref = self.refs.pop(hotkey_id, None)
        if ref is not None:
            self.carbon.UnregisterEventHotKey(ref)


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
        self.clip = clipcontrol.ClipboardControl(
            self.paths, self.preferences, python=sys.executable
        )
        self.dictation_registered = False
        self.history: hotkeys.Shortcut | None = None  # The history shortcut registered now.
        self.capturing = False  # The window is recording a new dictation shortcut.
        self.view: tuple[Any, ...] | None = None
        self.shortcut = self.preferences.shortcut()
        self.phase = ""
        self.store: clipstore.Store | None = None
        self.rows: clipstore.Items = []
        self.selected = 0
        return self

    # -- lifecycle --------------------------------------------------------
    def applicationDidFinishLaunching_(self, _notification: Any) -> None:
        self.item = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
        self.idle_image = template("menubar-icon.png")
        self.recording_image = template("menubar-recording.png", template_image=False)
        self.item.button().setImage_(self.idle_image)
        self.item.button().setToolTip_(hotkeys.APP_NAME)
        self.item.button().setTarget_(self)
        self.item.button().setAction_("togglePopover:")
        self.build_popover()
        self.hotkey = GlobalHotKey(self.pressed)
        self.hotkey.on(HISTORY_ID, lambda: self.open_window("--clipboard"))
        self.hotkey_ok = True
        if self.clip.features().dictation:
            self.hotkey_ok = self.hotkey.register(self.shortcut)
            self.dictation_registered = True
            hotkeys.record_status(self.paths, self.hotkey_ok)
        if not self.hotkey_ok:
            self.warn(
                "Shortcut unavailable",
                f"{self.shortcut.label()} is already used by another app. "
                "Choose a different shortcut from the menu bar icon.",
            )
        self.sync_login_item()
        self.clip.changed()  # The first look is not a change.
        self.apply_features()
        self.timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.5, self, "refresh:", None, True
        )
        self.refresh_(None)
        if not self.service.completed():
            self.open_window("--setup")

    @objc.python_method
    def framed(self, view: Any, rect: Any) -> Any:
        """Pin a factory-made control (button/label) to a fixed frame.

        `buttonWithTitle:target:action:` and `labelWithString:` return views set up for
        Auto Layout (translatesAutoresizingMaskIntoConstraints=NO) with no constraints of
        their own; anywhere Auto Layout later re-lays them out (a table view's cell views
        do this on every reload) that collapses the view instead of honoring setFrame_.
        """
        view.setTranslatesAutoresizingMaskIntoConstraints_(True)
        view.setFrame_(rect)
        return view

    @objc.python_method
    def build_popover(self) -> None:
        """A Maccy-style quick view: clicking the icon shows recent clips, a search
        field and Clear History, plus a slim dictation header/footer. Everything else
        (shortcut presets, Open at Login, pausing capture) lives in Settings now."""
        self.popover = NSPopover.alloc().init()
        self.popover.setBehavior_(NSPopoverBehaviorTransient)
        self.popover.setContentSize_(NSMakeSize(POPOVER_WIDTH, POPOVER_HEIGHT))
        root = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, POPOVER_WIDTH, POPOVER_HEIGHT))

        self.header_status = self.framed(
            NSTextField.labelWithString_(""), NSMakeRect(12, 392, 210, 20)
        )
        root.addSubview_(self.header_status)
        self.header_button = self.framed(
            NSButton.buttonWithTitle_target_action_("Start", self, "toggle:"),
            NSMakeRect(298, 386, 70, 28),
        )
        root.addSubview_(self.header_button)
        self.cancel_button = self.framed(
            NSButton.buttonWithTitle_target_action_("Cancel", self, "cancel:"),
            NSMakeRect(230, 386, 60, 28),
        )
        self.cancel_button.setHidden_(True)
        root.addSubview_(self.cancel_button)

        self.search_field = NSSearchField.alloc().initWithFrame_(
            NSMakeRect(8, 344, POPOVER_WIDTH - 16, 28)
        )
        self.search_field.setPlaceholderString_("Search clipboard history")
        self.search_field.setDelegate_(self)
        root.addSubview_(self.search_field)

        self.scroll = NSScrollView.alloc().initWithFrame_(
            NSMakeRect(8, 44, POPOVER_WIDTH - 16, 292)
        )
        self.scroll.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        self.scroll.setHasVerticalScroller_(True)
        self.scroll.setBorderType_(NSBezelBorder)
        self.table = NSTableView.alloc().initWithFrame_(self.scroll.bounds())
        self.table.setHeaderView_(None)
        self.table.setRowHeight_(ROW_HEIGHT)
        self.table.setColumnAutoresizingStyle_(NSTableViewUniformColumnAutoresizingStyle)
        column = NSTableColumn.alloc().initWithIdentifier_("item")
        # A fresh column defaults to 100pt; nothing forces it to the table's real width
        # until the popover is actually on screen, which is after the first reloadData().
        column.setWidth_(POPOVER_WIDTH - 16 - 16)
        self.table.addTableColumn_(column)
        self.table.setDataSource_(self)
        self.table.setDelegate_(self)
        self.table.setTarget_(self)
        self.table.setAction_("rowActivated:")
        self.scroll.setDocumentView_(self.table)
        root.addSubview_(self.scroll)

        self.empty_label = self.framed(
            NSTextField.wrappingLabelWithString_(""), NSMakeRect(24, 160, POPOVER_WIDTH - 48, 60)
        )
        self.empty_label.setAlignment_(NSTextAlignmentCenter)
        self.empty_label.setTextColor_(NSColor.secondaryLabelColor())
        root.addSubview_(self.empty_label)

        self.clear_button = self.framed(
            NSButton.buttonWithTitle_target_action_("Clear", self, "clearHistory:"),
            NSMakeRect(8, 8, 70, 28),
        )
        root.addSubview_(self.clear_button)
        self.full_button = self.framed(
            NSButton.buttonWithTitle_target_action_("Full History…", self, "openFullHistory:"),
            NSMakeRect(86, 8, 120, 28),
        )
        root.addSubview_(self.full_button)
        self.copy_last_button = self.framed(
            NSButton.buttonWithTitle_target_action_("Copy Last Transcript", self, "copyLast:"),
            NSMakeRect(8, 8, 190, 28),
        )
        self.copy_last_button.setHidden_(True)
        root.addSubview_(self.copy_last_button)
        settings_button = self.framed(
            NSButton.buttonWithTitle_target_action_("Settings…", self, "openSettings:"),
            NSMakeRect(210, 8, 90, 28),
        )
        root.addSubview_(settings_button)
        quit_button = self.framed(
            NSButton.buttonWithTitle_target_action_("Quit", self, "quit:"),
            NSMakeRect(308, 8, 60, 28),
        )
        quit_button.setToolTip_(f"Quit {hotkeys.APP_NAME}")
        root.addSubview_(quit_button)

        content = NSViewController.alloc().init()
        content.setView_(root)
        self.popover.setContentViewController_(content)

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
        self.popover.close()
        self.pressed()

    def cancel_(self, _sender: Any) -> None:
        self.popover.close()
        self.run_engine("--cancel")

    def copyLast_(self, _sender: Any) -> None:
        self.popover.close()
        self.run_engine("--copy-last")

    def openSettings_(self, _sender: Any) -> None:
        self.popover.close()
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
        self.view = None
        if self.popover.isShown():
            self.refresh_popover_header()

    @objc.python_method
    def sync_login_item(self) -> None:
        enabled = self.preferences.open_at_login()
        # Set by the bundle launcher; the Python interpreter is a different bundle.
        bundle = os.environ.get("WHISPER_DICTATION_BUNDLE", "")
        if bundle.endswith(".app"):
            hotkeys.set_login_item(enabled, hotkeys.bundle_login_command(bundle))

    def quit_(self, _sender: Any) -> None:
        self.clip.stop()
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
    def apply_features(self) -> None:
        """Own the global shortcuts for the chosen features and refresh the popover to match."""
        features = self.clip.features()
        if features.dictation and not self.dictation_registered:
            self.hotkey_ok = self.hotkey.register(self.shortcut)
            self.dictation_registered = True
            hotkeys.record_status(self.paths, self.hotkey_ok)
        elif not features.dictation and self.dictation_registered:
            self.hotkey.unregister()
            self.dictation_registered = False
        history = self.clip.history_shortcut()
        if history != self.history:
            self.history = history
            self.hotkey.unregister(HISTORY_ID)
            ok = history is None or self.hotkey.register(history, HISTORY_ID)
            hotkeys.record_status(self.paths, ok, hotkeys.HISTORY_STATUS)
        if self.popover.isShown():
            self.refresh_popover_layout()
        self.view = None  # Redraw the status icon.

    @objc.python_method
    def follow_window_shortcut(self) -> None:
        """Pause the dictation shortcut while the window records a new one, then use it."""
        flag = self.paths.runtime / "shortcut-capture"
        capturing = flag.exists()
        if capturing and not self.capturing:
            window = desktop.lock(self.paths.runtime / "app.lock")
            if window is not None:  # A crashed window left the flag behind.
                os.close(window)
                flag.unlink(missing_ok=True)
                capturing = False
        if capturing == self.capturing:
            return
        self.capturing = capturing
        if capturing:
            self.hotkey.unregister()
            return
        if not self.clip.features().dictation:
            return
        chosen = self.preferences.shortcut()
        if chosen != self.shortcut:
            self.apply_shortcut(chosen)
        else:
            self.hotkey_ok = self.hotkey.register(self.shortcut)
            hotkeys.record_status(self.paths, self.hotkey_ok)

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
        self.clip.supervise()
        if self.clip.changed():
            self.apply_features()
        self.follow_window_shortcut()
        try:
            current = workflow.snapshot(self.paths)
        except (d.DictationError, OSError, ValueError):
            current = {"phase": "idle", "active": False, "retained_audio": False}
        active = bool(current["active"])
        phase = str(current["phase"]) if active else "idle"
        elapsed = int(current.get("elapsed_seconds", 0))
        ready = self.ready()
        # Redraw only when something visible changed; this runs twice a second.
        view = (phase, elapsed if phase == "recording" else 0, self.shortcut, self.hotkey_ok, ready)
        if view != self.view:
            self.view = view
            button = self.item.button()
            if phase == "recording":
                button.setImage_(self.recording_image)
                button.setTitle_(f" {elapsed // 60}:{elapsed % 60:02d}")
            elif active:
                button.setImage_(self.idle_image)
                button.setTitle_(" …")
            else:
                button.setImage_(self.idle_image)
                button.setTitle_("")
        if self.popover.isShown():
            self.refresh_popover_header()

    # -- popover ------------------------------------------------------------
    def togglePopover_(self, _sender: Any) -> None:
        if self.popover.isShown():
            self.popover.close()
            return
        self.search_field.setStringValue_("")
        self.refresh_popover_layout()
        self.popover.showRelativeToRect_ofView_preferredEdge_(
            self.item.button().bounds(), self.item.button(), NSMaxYEdge
        )
        window = self.popover.contentViewController().view().window()
        if window is not None:
            window.makeFirstResponder_(self.search_field)

    @objc.python_method
    def refresh_popover_layout(self) -> None:
        """Show only the sections the chosen features need."""
        features = self.clip.features()
        for view in (self.header_status, self.header_button, self.cancel_button):
            view.setHidden_(not features.dictation)
        self.search_field.setHidden_(not features.clipboard)
        self.clear_button.setHidden_(not features.clipboard)
        self.full_button.setHidden_(not features.clipboard)
        self.copy_last_button.setHidden_(features.clipboard or not features.dictation)
        if features.clipboard:
            self.scroll.setHidden_(False)
            self.run_query(self.search_field.stringValue())
        else:
            self.scroll.setHidden_(True)
            self.empty_label.setStringValue_(
                "Turn on Clipboard history in Settings to see recent copies here."
            )
            self.empty_label.setHidden_(False)
        self.refresh_popover_header()

    @objc.python_method
    def refresh_popover_header(self) -> None:
        if not self.clip.features().dictation:
            return
        try:
            current = workflow.snapshot(self.paths)
        except (d.DictationError, OSError, ValueError):
            current = {"phase": "idle", "active": False, "elapsed_seconds": 0}
        active = bool(current["active"])
        phase = str(current["phase"]) if active else "idle"
        elapsed = int(current.get("elapsed_seconds", 0))
        label = self.shortcut.label()
        self.cancel_button.setHidden_(phase != "recording")
        if phase == "recording":
            self.header_status.setStringValue_(f"Recording… {elapsed // 60}:{elapsed % 60:02d}")
            self.header_button.setTitle_("Stop")
            self.header_button.setEnabled_(True)
        elif active:
            self.header_status.setStringValue_("Transcribing…")
            self.header_button.setTitle_("…")
            self.header_button.setEnabled_(False)
        else:
            self.header_status.setStringValue_(
                "Finish setup to start"
                if not self.ready()
                else f"Press {label} anywhere to dictate"
                if self.hotkey_ok
                else f"{label} is taken — choose another shortcut"
            )
            self.header_button.setTitle_("Start")
            self.header_button.setEnabled_(True)

    @objc.python_method
    def open_store(self) -> clipstore.Store | None:
        if self.store is None:
            try:
                self.store = clipstore.Store(self.paths.clipboard)
            except (clipstore.StoreError, OSError) as exc:
                telemetry.capture(exc, level="warning", stage="popover_store")
        return self.store

    @objc.python_method
    def run_query(self, query: str) -> None:
        store = self.open_store()
        try:
            self.rows = store.list(query=query, limit=POPOVER_ROWS) if store is not None else []
        except (sqlite3.Error, OSError):
            self.rows = []
            store = None
        self.table.reloadData()
        self.selected = 0
        if self.rows:
            self.empty_label.setHidden_(True)
            self.select_row(0)
        else:
            self.empty_label.setStringValue_(
                "Clipboard history isn’t available."
                if store is None
                else "No matches."
                if query
                else "Nothing copied yet."
            )
            self.empty_label.setHidden_(False)

    @objc.python_method
    def row_text(self, item: clipstore.Item) -> str:
        return clipcontrol.preview_text(item, POPOVER_PREVIEW_CHARS)

    @objc.python_method
    def select_row(self, row: int) -> None:
        if not self.rows:
            return
        row = max(0, min(row, len(self.rows) - 1))
        self.selected = row
        self.table.selectRowIndexes_byExtendingSelection_(NSIndexSet.indexSetWithIndex_(row), False)
        self.table.scrollRowToVisible_(row)

    @objc.python_method
    def move_selection(self, delta: int) -> None:
        self.select_row(self.selected + delta)

    @objc.python_method
    def activate_selected(self) -> None:
        if 0 <= self.selected < len(self.rows):
            if self.copy_item(self.rows[self.selected]):
                self.popover.close()

    @objc.python_method
    def copy_item(self, item: clipstore.Item) -> bool:
        if self.store is None:
            return False
        try:
            self.service.copy_item(item, self.store)
        except d.DictationError as exc:
            self.warn("Couldn’t copy that item", str(exc))
            return False
        return True

    def numberOfRowsInTableView_(self, _table_view: Any) -> int:
        return len(self.rows)

    def tableView_viewForTableColumn_row_(self, _table_view: Any, column: Any, row: int) -> Any:
        item = self.rows[row]
        width = column.width()
        view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, width, ROW_HEIGHT))
        view.setAutoresizingMask_(NSViewWidthSizable)
        text_x = 8.0
        if item.kind == "image":
            thumb_path = self.store.thumb_path(item) if self.store is not None else None
            image = (
                NSImage.alloc().initWithContentsOfFile_(str(thumb_path))
                if thumb_path is not None
                else None
            )
            if image is not None:
                thumb = NSImageView.alloc().initWithFrame_(
                    NSMakeRect(8, (ROW_HEIGHT - THUMB_SIZE) / 2, THUMB_SIZE, THUMB_SIZE)
                )
                thumb.setImage_(image)
                thumb.setImageScaling_(NSImageScaleProportionallyUpOrDown)
                view.addSubview_(thumb)
                text_x = 8.0 + THUMB_SIZE + 8.0
        label = self.framed(
            NSTextField.labelWithString_(self.row_text(item)),
            NSMakeRect(text_x, 0, width - text_x - 8, ROW_HEIGHT),
        )
        label.setAutoresizingMask_(NSViewWidthSizable)
        view.addSubview_(label)
        return view

    def rowActivated_(self, sender: Any) -> None:
        row = sender.clickedRow()
        if row >= 0:
            self.select_row(row)
            self.activate_selected()

    def controlTextDidChange_(self, _notification: Any) -> None:
        self.run_query(self.search_field.stringValue())

    def control_textView_doCommandBySelector_(
        self, _control: Any, _text_view: Any, selector: str
    ) -> bool:
        if selector == "moveDown:":
            self.move_selection(1)
        elif selector == "moveUp:":
            self.move_selection(-1)
        elif selector == "insertNewline:":
            self.activate_selected()
        elif selector == "cancelOperation:":
            self.popover.close()
        else:
            return False
        return True

    def clearHistory_(self, _sender: Any) -> None:
        store = self.open_store()
        if store is None:
            return
        alert = NSAlert.alloc().init()
        alert.setMessageText_("Clear clipboard history?")
        alert.setInformativeText_("Removes everything except favorites. This can’t be undone.")
        alert.addButtonWithTitle_("Clear History")
        alert.addButtonWithTitle_("Cancel")
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        if alert.runModal() == NSAlertFirstButtonReturn:
            try:
                self.service.clear_clipboard(store, everywhere=False, keep_favorites=True)
                self.run_query(self.search_field.stringValue())
            except (sqlite3.Error, OSError) as exc:
                telemetry.capture(exc, level="warning", stage="popover_clear")
                self.warn("Couldn’t clear history", "The clipboard history is busy. Try again.")

    def openFullHistory_(self, _sender: Any) -> None:
        self.popover.close()
        self.open_window("--clipboard")


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
        # Already running: opening it again shows the window. A start by launchd (the
        # login item loaded while the app runs) is not someone opening it.
        if os.environ.get("XPC_SERVICE_NAME") != hotkeys.AGENT_LABEL:
            open_app_window()
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
