"""Shortcut and open-at-login preferences shared by the macOS menu bar and the
Windows/Linux tray. Pure data and files only; GUI toolkits live elsewhere.
"""

from __future__ import annotations

import json
import logging
import os
import plistlib
import re
import shlex
import shutil
import string
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import desktop
import dictation as d

try:
    import logsetup

    log = logsetup.get_logger("hotkeys")
except ImportError:
    log = logging.getLogger(__name__)

# Shown wherever the app names itself. Identifiers, folders and the bundle id keep the
# original "whisper-dictation" spelling so upgrades keep working.
# Formerly "Whisper Dictation & Clipboard+"; identifiers below stay stable for upgrades.
APP_NAME = "Clipboard+"
# Earlier names of the app, newest first: launchers under these are ours to replace.
FORMER_NAMES = (
    "Clipboard+ and Dictation",
    "Clipboard+ Desktop",
    "Whisper Dictation & Clipboard+",
    "Whisper Dictation",
)
# The dictation shortcut's name in the desktop's keyboard settings.
DICTATION_SHORTCUT_NAME = f"{APP_NAME}: dictation"
MODIFIER_ORDER = ("ctrl", "alt", "shift", "cmd")
MAC_SYMBOLS = {"ctrl": "⌃", "alt": "⌥", "shift": "⇧", "cmd": "⌘"}
# Carbon (macOS) modifier masks from Events.h.
CARBON = {"ctrl": 0x1000, "alt": 0x800, "shift": 0x200, "cmd": 0x100}
# NSEvent modifierFlags bits.
EVENT_FLAGS = {"shift": 1 << 17, "ctrl": 1 << 18, "alt": 1 << 19, "cmd": 1 << 20}
# RegisterHotKey (Windows) modifiers; MOD_NOREPEAT stops auto-repeat toggling.
WINDOWS = {"alt": 0x1, "ctrl": 0x2, "shift": 0x4, "cmd": 0x8}
MOD_NOREPEAT = 0x4000
GNOME = {"ctrl": "<Control>", "alt": "<Alt>", "shift": "<Shift>", "cmd": "<Super>"}

FUNCTION_KEYS = tuple(f"F{number}" for number in range(1, 13))
SPECIAL_KEYS = ("Space", "Return", "Tab", "Esc")
KEYS = tuple(string.ascii_uppercase) + tuple(string.digits) + SPECIAL_KEYS + FUNCTION_KEYS
# macOS virtual key codes (ANSI layout).
MAC_KEY_CODES = {
    **dict(zip("ASDFHGZXCV", range(10))),
    **dict(zip("BQWERYT", range(11, 18))),
    **dict(zip("1234659", (18, 19, 20, 21, 22, 23, 25))),
    "7": 26,
    "8": 28,
    **dict(zip("0OUIPLJKNM", (29, 31, 32, 34, 35, 37, 38, 40, 45, 46))),
    "Space": 49,
    "Return": 36,
    "Tab": 48,
    "Esc": 53,
    "F1": 122,
    "F2": 120,
    "F3": 99,
    "F4": 118,
    "F5": 96,
    "F6": 97,
    "F7": 98,
    "F8": 100,
    "F9": 101,
    "F10": 109,
    "F11": 103,
    "F12": 111,
}
MAC_KEY_NAMES = {code: name for name, code in MAC_KEY_CODES.items()}
TK_NAMES = {"space": "Space", "Return": "Return", "Tab": "Tab", "Escape": "Esc"}
TK_MODIFIERS = {
    "Control_L": "ctrl",
    "Control_R": "ctrl",
    "Alt_L": "alt",
    "Alt_R": "alt",
    "Option_L": "alt",
    "Option_R": "alt",
    "Meta_L": "alt",  # X11's Meta is Alt; macOS differs, see tk_modifier.
    "Meta_R": "alt",
    "Shift_L": "shift",
    "Shift_R": "shift",
    "Super_L": "cmd",
    "Super_R": "cmd",
    "Win_L": "cmd",
    "Win_R": "cmd",
    "Command_L": "cmd",
    "Command_R": "cmd",
}
# Tk's Aqua port reports the Command key as Meta_L/Meta_R and Option as Alt_L/Alt_R.
TK_MODIFIERS_MACOS = {**TK_MODIFIERS, "Meta_L": "cmd", "Meta_R": "cmd"}
# Bits of a Tk key event's `state`, per platform. Tk Aqua: Command is Mod1, Option is Mod2.
# X11: Alt is Mod1, Super is Mod4 (Mod2 is usually NumLock). Windows: only these three
# (Mod1 is NumLock there); the Windows key is never in `state`, so it is tracked by keysym.
TK_STATE_MASKS = {
    "macos": {"shift": 0x1, "ctrl": 0x4, "cmd": 0x8, "alt": 0x10},
    "linux": {"shift": 0x1, "ctrl": 0x4, "alt": 0x8, "cmd": 0x40},
    "windows": {"shift": 0x1, "ctrl": 0x4, "alt": 0x20000},
}
KINDS = ("dictation", "history")
KIND_NAMES = {"dictation": "dictation", "history": "clipboard history"}
# Files in the runtime folder that the window and the tray or menu bar share.
CAPTURE_FLAGS = {"dictation": "shortcut-capture", "history": "history-shortcut-capture"}
HEARD_SECONDS = 30.0  # How long "heard it" stays true after a press.

CLIPBOARD_PLUS = "https://clipboardplus.apercallc.com"
# The macOS bundle identity; setup-desktop.py's Info.plist CFBundleIdentifier stays
# the same value (kept here so both files migrate it together).
BUNDLE_ID = "com.apercallc.clipboardplusdesktop"
FORMER_BUNDLE_IDS = ("org.whisperdictation.desktop",)
AGENT_LABEL = f"{BUNDLE_ID}.menubar"
FORMER_AGENT_LABELS = ("org.whisperdictation.menubar",)
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE_NAME = "ClipboardPlus"
FORMER_RUN_VALUE_NAMES = ("WhisperDictation",)
AUTOSTART_DIR = ".config/autostart"
GNOME_LIST_PREFIX = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/"
GNOME_PATH = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/dictation/"
GNOME_HISTORY_PATH = (
    "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/clipboard-history/"
)
GNOME_LIST = ("org.gnome.settings-daemon.plugins.media-keys", "custom-keybindings")
# Where GNOME keeps its own shortcuts; a key used there never reaches a custom binding.
GNOME_BUILT_IN = (
    "org.gnome.desktop.wm.keybindings",
    "org.gnome.shell.keybindings",
    "org.gnome.settings-daemon.plugins.media-keys",
    "org.gnome.mutter.keybindings",
    "org.gnome.mutter.wayland.keybindings",
)


SYSTEM_NAMES = {"macos": "macOS", "windows": "Windows", "linux": "your desktop"}
# Combinations the system owns and never hands to an app, per platform: (modifiers in
# MODIFIER_ORDER, keys). Not exhaustive on purpose: the desktop's own list can be looked
# up live on GNOME (gnome_conflict), and a registration the system refuses is reported.
RESERVED: dict[str, tuple[tuple[tuple[str, ...], frozenset[str]], ...]] = {
    "macos": (
        (("cmd",), frozenset({"Space", "Tab", "Q", "H", "M", "W"})),
        (("shift", "cmd"), frozenset({"3", "4", "5", "Q", "Tab"})),
        # ⌥⌘D shows and hides the Dock; ⌥⌘Esc is Force Quit; ⌥⌘Space is Finder search.
        (("alt", "cmd"), frozenset({"D", "Esc", "Space", "H", "M"})),
        (("ctrl",), frozenset({"Space"})),  # Input source.
        (("ctrl", "cmd"), frozenset({"Space", "Q"})),  # Emoji and lock screen.
    ),
    "windows": (
        (("cmd",), frozenset("DELMIRXASPKGHNTUBCWZOV") | {"Space", "Tab", *string.digits}),
        (("shift", "cmd"), frozenset({"S", "M", "V", "Tab"})),
        (("alt", "cmd"), frozenset({"D", "B", "K", "H"})),
        (("alt",), frozenset({"Tab", "Space", "F4", "Esc"})),
        (("ctrl",), frozenset({"Esc"})),
        (("ctrl", "shift"), frozenset({"Esc"})),  # Task Manager.
        (("ctrl", "alt"), frozenset({"Tab"})),
    ),
    "linux": (
        (("cmd",), frozenset("LDASVMNP") | {"Space", "Tab", *string.digits}),
        (("shift", "cmd"), frozenset({"Tab"})),
        (("alt",), frozenset({"Tab", "Space", "F4", "Esc"})),
        (("ctrl", "alt"), frozenset({"Tab", "T"})),  # Switch windows; terminal on GNOME.
    ),
}


def canonical(modifiers: Any) -> tuple[str, ...]:
    present = set(modifiers)  # A generator would be consumed by the first lookup.
    return tuple(name for name in MODIFIER_ORDER if name in present)


@dataclass(frozen=True)
class Shortcut:
    modifiers: tuple[str, ...]
    key: str

    def label(self, platform: str | None = None) -> str:
        platform = platform or desktop.platform_name()
        if platform == "macos":
            return "".join(MAC_SYMBOLS[name] for name in self.modifiers) + self.key
        names = {"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift"}
        names["cmd"] = "Win" if platform == "windows" else "Super"
        # The Win/Super key reads first: "Super+Shift+D".
        ordered = sorted(self.modifiers, key=lambda name: name != "cmd")
        return "+".join([names[name] for name in ordered] + [self.key])

    def basic_problem(self) -> str:
        """Why this cannot be a global shortcut on any desktop, or an empty string."""
        if self.key not in KEYS:
            return "Use a letter, number, Space, or F1–F12."
        if self.key in FUNCTION_KEYS:
            return ""
        if not {"ctrl", "alt", "cmd"} & set(self.modifiers):
            return "Include Ctrl, Alt/Option, or ⌘/Win so normal typing isn’t captured."
        if self.modifiers in (("ctrl",), ("cmd",)) and self.key in set("ACFHMNOPQSTVWXYZ"):
            return "Most apps already use that shortcut. Add Alt/Option or Shift."
        return ""

    def reserved(self, platform: str | None = None) -> str:
        """A sentence saying which desktop owns these keys, or an empty string.

        Only combinations the system never lets an app have are listed; GNOME's own and
        custom shortcuts are looked up live instead (gnome_conflict).
        """
        platform = platform or desktop.platform_name()
        for modifiers, keys in RESERVED.get(platform, ()):
            if self.modifiers == modifiers and self.key in keys:
                return (
                    f"{self.label(platform)} is used by {SYSTEM_NAMES.get(platform, 'your system')} "
                    "itself, so it can’t be a global shortcut. Choose another."
                )
        return ""

    def problem(self, platform: str | None = None) -> str:
        """Why this cannot be a global shortcut here, or an empty string."""
        return self.basic_problem() or self.reserved(platform)

    def mac_key_code(self) -> int:
        return MAC_KEY_CODES[self.key]

    def carbon_modifiers(self) -> int:
        return sum(CARBON[name] for name in self.modifiers)

    def windows_key(self) -> int:
        if self.key in FUNCTION_KEYS:
            return 0x6F + int(self.key[1:])
        return {"Space": 0x20, "Return": 0x0D, "Tab": 0x09, "Esc": 0x1B}.get(
            self.key, ord(self.key[0])
        )

    def windows_modifiers(self) -> int:
        return sum(WINDOWS[name] for name in self.modifiers) | MOD_NOREPEAT

    def gnome(self) -> str:
        key = {"Space": "space", "Esc": "Escape"}.get(self.key, self.key)
        return "".join(GNOME[name] for name in self.modifiers) + (
            key.lower() if len(key) == 1 else key
        )

    def qt(self) -> str:
        """KDE's spelling: Ctrl+Shift+D (the Super key is Meta)."""
        names = {"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "cmd": "Meta"}
        return "+".join([names[name] for name in self.modifiers] + [self._xkb_key(False)])

    def sway(self) -> str:
        """Sway's spelling: Ctrl+Shift+d (Mod4 is the Super key)."""
        names = {"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "cmd": "Mod4"}
        return "+".join([names[name] for name in self.modifiers] + [self._xkb_key(True)])

    def hyprland(self) -> tuple[str, str]:
        """Hyprland's (modifiers, key): ("CTRL SHIFT", "D")."""
        names = {"ctrl": "CTRL", "alt": "ALT", "shift": "SHIFT", "cmd": "SUPER"}
        return " ".join(names[name] for name in self.modifiers), self._xkb_key(False)

    def _xkb_key(self, lower: bool) -> str:
        special = {"Space": "space", "Esc": "Escape"}
        if self.key in special:
            return special[self.key]
        return self.key.lower() if lower and len(self.key) == 1 else self.key


def default_shortcut(platform: str) -> Shortcut:
    """The same shortcut on every desktop: ⌘⇧D on macOS, Win+Shift+D on Windows and
    Super+Shift+D on Linux (the `cmd` modifier is each system's own key).

    Ctrl+Shift+D was the default before; it did not work everywhere (the Windows and
    Linux tray could not hear it, and browsers and terminals take it first). It stays
    one click away as a preset, and anyone who saved a shortcut keeps theirs.
    """
    return Shortcut(("shift", "cmd"), "D")


DEFAULT = default_shortcut(desktop.platform_name())


def presets(platform: str) -> tuple[Shortcut, ...]:
    """The one-click choices in the recorder: the default first, then earlier defaults and
    alternatives that no system reserves on `platform`."""
    options = (
        default_shortcut(platform),
        Shortcut(("ctrl", "shift"), "D"),  # The earlier default.
        Shortcut(("ctrl", "alt"), "D"),  # The one before that.
        # Alt+Space opens the window menu on GNOME and Windows, so only macOS offers it.
        *((Shortcut(("alt",), "Space"),) if platform == "macos" else ()),
        Shortcut(("ctrl", "alt"), "Space"),
        Shortcut(("ctrl", "alt", "shift"), "D"),
    )
    return tuple(dict.fromkeys(item for item in options if not item.problem(platform)))


PRESETS = presets(desktop.platform_name())


def default_history_shortcut(platform: str) -> Shortcut:
    """Opens the clipboard history: the dictation default with F (for find) instead of D."""
    return Shortcut(default_shortcut(platform).modifiers, "F")


DEFAULT_HISTORY = default_history_shortcut(desktop.platform_name())


def history_presets(platform: str) -> tuple[Shortcut, ...]:
    options = (
        default_history_shortcut(platform),
        Shortcut(("ctrl", "shift"), "F"),  # The earlier default.
        Shortcut(("ctrl", "alt", "shift"), "V"),
        Shortcut(("ctrl", "alt"), "H"),
    )
    return tuple(dict.fromkeys(item for item in options if not item.problem(platform)))


HISTORY_PRESETS = history_presets(desktop.platform_name())


def from_mac_event(key_code: int, flags: int, characters: str) -> Shortcut:
    key = MAC_KEY_NAMES.get(key_code) or characters.strip().upper()
    return Shortcut(canonical(n for n in EVENT_FLAGS if flags & EVENT_FLAGS[n]), key)


def tk_modifier(keysym: str, platform: str | None = None) -> str:
    """The modifier a Tk keysym stands for on this platform, or an empty string."""
    platform = platform or desktop.platform_name()
    table = TK_MODIFIERS_MACOS if platform == "macos" else TK_MODIFIERS
    return table.get(keysym, "")


def modifiers_from_state(state: int, platform: str | None = None) -> set[str]:
    """The modifiers a Tk key event's `state` bit mask says are down."""
    masks = TK_STATE_MASKS.get(platform or desktop.platform_name(), TK_STATE_MASKS["linux"])
    return {name for name, mask in masks.items() if state & mask}


def from_tk(
    keysym: str,
    held: set[str],
    state: int = 0,
    keycode: int = 0,
    platform: str | None = None,
) -> Shortcut | None:
    """A shortcut from a Tk key press, or None while only modifiers are held.

    Modifiers are the keys tracked as held plus the event's `state` mask (Tk's Aqua port
    misses modifier releases, so on macOS the mask alone decides when it has anything).
    The key comes from the keysym; when that is not a usable key (Option+D types "∂" on
    a Mac) the hardware key code decides.
    """
    platform = platform or desktop.platform_name()
    if tk_modifier(keysym, platform):
        return None
    from_state = modifiers_from_state(state, platform)
    modifiers = (from_state or held) if platform == "macos" else (from_state | held)
    key = TK_NAMES.get(keysym) or (keysym.upper() if len(keysym) == 1 else keysym)
    if key not in KEYS and platform == "macos":
        key = MAC_KEY_NAMES.get(keycode & 0xFFFF, key)  # Aqua: virtual key code, low 16 bits.
    return Shortcut(canonical(modifiers), key)


def choice_problem(shortcut: Shortcut, other: Shortcut | None, other_kind: str) -> str:
    """Why this cannot be chosen (unusable, or already the other feature's), else empty."""
    problem = shortcut.problem()
    if problem or other is None or other != shortcut:
        return problem
    return (
        f"{shortcut.label()} already belongs to {KIND_NAMES[other_kind]}. Choose a different one."
    )


@dataclass(frozen=True)
class Features:
    """Which halves of the app the user chose in setup."""

    dictation: bool = True
    # Clipboard capture remains off until setup explicitly confirms it. The setup UI
    # preselects Both, but the background tray must never begin recording clipboard data.
    clipboard: bool = False


def _bounded(raw: Any, default: int, low: int, high: int) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        return default
    return max(low, min(high, raw))


@dataclass(frozen=True)
class ClipboardSettings:
    """Clipboard history options. `paused_until` is an epoch time, or -1 for until resumed."""

    keep_items: int = 1000
    keep_days: int = 30
    images: bool = True
    paused_until: float = 0.0

    def paused(self, now: float) -> bool:
        return self.paused_until == -1 or self.paused_until > now

    def clamped(self) -> ClipboardSettings:
        return ClipboardSettings(
            keep_items=_bounded(self.keep_items, 1000, 50, 10_000),
            keep_days=_bounded(self.keep_days, 30, 1, 3650),
            images=self.images,
            paused_until=self.paused_until,
        )


class Preferences:
    def __init__(self, paths: d.Paths) -> None:
        self.path = paths.config.parent / "menubar.json"

    def read(self) -> dict[str, Any]:
        try:
            return d.read_json(self.path)
        except (d.DictationError, OSError, ValueError) as exc:
            log.warning("could not read the preferences: %s", exc)
            return {}

    def stamp(self) -> tuple[int, int]:
        """(mtime ns, size): either alone can miss a fast rewrite on a coarse clock."""
        try:
            info = self.path.stat()
        except OSError:
            return (0, 0)
        return (info.st_mtime_ns, info.st_size)

    @staticmethod
    def _shortcut(raw: Any, default: Shortcut) -> Shortcut:
        if not isinstance(raw, dict):
            return default
        modifiers, key = raw.get("modifiers"), raw.get("key")
        if not isinstance(modifiers, list) or not isinstance(key, str):
            return default
        shortcut = Shortcut(canonical(modifiers), key)
        # A saved shortcut stays even if a later release reserves its keys.
        return default if shortcut.basic_problem() else shortcut

    def shortcut(self) -> Shortcut:
        return self._shortcut(self.read().get("shortcut"), DEFAULT)

    def history_shortcut(self) -> Shortcut | None:
        """The shortcut that opens the clipboard history; None when switched off."""
        raw = self.read().get("history_shortcut")
        return None if raw is False else self._shortcut(raw, DEFAULT_HISTORY)

    def features(self) -> Features:
        """Dictation is safe before setup; at least one feature is always on."""
        raw = self.read().get("features")
        if not isinstance(raw, dict):
            return Features()
        dictation, clipboard = raw.get("dictation"), raw.get("clipboard")
        if not isinstance(dictation, bool) or not isinstance(clipboard, bool):
            return Features()
        if not (dictation or clipboard):
            return Features()
        return Features(dictation, clipboard)

    def clipboard(self) -> ClipboardSettings:
        raw = self.read().get("clipboard")
        if not isinstance(raw, dict):
            return ClipboardSettings()
        defaults = ClipboardSettings()
        images, paused = raw.get("images"), raw.get("paused_until")
        return ClipboardSettings(
            keep_items=raw.get("keep_items", defaults.keep_items),
            keep_days=raw.get("keep_days", defaults.keep_days),
            images=images if isinstance(images, bool) else defaults.images,
            paused_until=float(paused)
            if isinstance(paused, (int, float)) and not isinstance(paused, bool)
            else defaults.paused_until,
        ).clamped()

    def open_at_login(self) -> bool:
        return self.read().get("open_at_login", True) is not False

    def share_usage(self) -> bool:
        """Anonymous crash reports and usage statistics (telemetry.py): off until the user
        agrees, in setup or Settings (the same value telemetry.has_consent reads)."""
        return self.read().get("share_usage") is True

    def auto_updates(self) -> bool:
        """Checking for app updates: on unless turned off in Settings."""
        return self.read().get("auto_updates", True) is not False

    def save(
        self,
        shortcut: Shortcut | None = None,
        open_at_login: bool | None = None,
        features: Features | None = None,
        clipboard: ClipboardSettings | None = None,
        history_shortcut: Shortcut | Literal[False] | None = None,
        share_usage: bool | None = None,
        auto_updates: bool | None = None,
    ) -> None:
        """Change the given preferences. `history_shortcut=False` switches it off."""
        values = self.read()
        if auto_updates is not None:
            values["auto_updates"] = auto_updates
        if share_usage is not None:
            values["share_usage"] = share_usage
        if history_shortcut is not None:
            values["history_shortcut"] = history_shortcut and {
                "modifiers": list(history_shortcut.modifiers),
                "key": history_shortcut.key,
            }
        if features is not None:
            values["features"] = {"dictation": features.dictation, "clipboard": features.clipboard}
        if clipboard is not None:
            saved = clipboard.clamped()
            values["clipboard"] = {
                "keep_items": saved.keep_items,
                "keep_days": saved.keep_days,
                "images": saved.images,
                "paused_until": saved.paused_until,
            }
        if shortcut is not None:
            values["shortcut"] = {"modifiers": list(shortcut.modifiers), "key": shortcut.key}
        if open_at_login is not None:
            values["open_at_login"] = open_at_login
        d.private_dir(self.path.parent)
        d.atomic(self.path, json.dumps(values, indent=2))


def agent_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / f"Library/LaunchAgents/{AGENT_LABEL}.plist"


def autostart_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / f"{AUTOSTART_DIR}/{desktop.DESKTOP_ENTRY_ID}.desktop"


def former_autostart_paths(home: Path | None = None) -> tuple[Path, ...]:
    return tuple(
        (home or Path.home()) / f"{AUTOSTART_DIR}/{former}.desktop"
        for former in desktop.FORMER_DESKTOP_ENTRY_IDS
    )


def bundle_login_command(bundle: str) -> list[str]:
    """What the macOS login item runs for the app bundle: its executable itself (not
    `open`, which returns at once) so launchd supervises the app and KeepAlive can
    restart it after a crash. The executable name comes from the bundle's own
    Info.plist (a packaged build names it "menubar"; the source-install wrapper
    names it "Clipboard+") rather than a single hardcoded guess."""
    name = "WhisperDictation"
    try:
        info = plistlib.loads((Path(bundle) / "Contents/Info.plist").read_bytes())
        name = info.get("CFBundleExecutable", name)
    except (OSError, plistlib.InvalidFileException, ValueError) as exc:
        log.warning("could not read the login bundle: %s", exc)
    executable = Path(bundle) / "Contents/MacOS" / name
    return [str(executable)] if executable.is_file() else ["/usr/bin/open", bundle]


def login_bundle_id(command: list[str]) -> str:
    """Source installs and packaged releases have distinct registered bundle IDs."""
    if command and Path(command[0]).parent.name == "MacOS":
        try:
            info = plistlib.loads((Path(command[0]).parent.parent / "Info.plist").read_bytes())
            identifier = info.get("CFBundleIdentifier")
            if identifier in (BUNDLE_ID, "com.apercallc.clipboardplus"):
                return str(identifier)
        except (OSError, ValueError, plistlib.InvalidFileException) as exc:
            log.warning("could not read the login item: %s", exc)
    return BUNDLE_ID


def set_login_item(
    enabled: bool,
    command: list[str],
    home: Path | None = None,
    platform: str | None = None,
    registry: Any = None,
    run: Any = subprocess.run,
) -> None:
    """Start the tray/menu bar app when the user logs in."""
    platform = platform or desktop.platform_name()
    if platform == "windows":
        winreg = registry or __import__("winreg")
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            # A name from a former release is never left behind alongside the new one.
            for former in FORMER_RUN_VALUE_NAMES:
                try:
                    winreg.DeleteValue(key, former)
                except FileNotFoundError:
                    pass
            if enabled:
                value = subprocess.list2cmdline(command)
                winreg.SetValueEx(key, RUN_VALUE_NAME, 0, winreg.REG_SZ, value)
            else:
                try:
                    winreg.DeleteValue(key, RUN_VALUE_NAME)
                except FileNotFoundError:
                    pass
        return
    if platform == "macos":
        _migrate_former_agents(home, run)
    path = agent_path(home) if platform == "macos" else autostart_path(home)
    if not enabled:
        if platform == "macos" and _menubar_is_idle():
            _launchctl("bootout", AGENT_LABEL, run)
        path.unlink(missing_ok=True)
        if platform != "macos":
            for former_path in former_autostart_paths(home):
                former_path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if platform == "macos":
        agent = {
            "Label": AGENT_LABEL,
            "AssociatedBundleIdentifiers": [login_bundle_id(command)],
            "ProgramArguments": command,
            "RunAtLoad": True,
            "ProcessType": "Interactive",
            # Come back after a crash or a force quit; a clean Quit stays quit.
            "KeepAlive": {"SuccessfulExit": False},
        }
        try:
            loaded = plistlib.loads(path.read_bytes())
        except (OSError, plistlib.InvalidFileException, ValueError) as exc:
            log.warning("could not read the login agent: %s", exc)
            loaded = None
        d.atomic(path, plistlib.dumps(agent).decode("utf-8"))
        # launchd keeps a loaded agent's old definition (bootstrap of it is a no-op),
        # so a changed one is unloaded first: always when it ran `open`, which never
        # owned the app, otherwise only while the app is not running (it would quit).
        # The file can already be current while launchd still holds an older
        # definition (an earlier upgrade may have failed to unload it). While
        # idle, always replace the loaded job so renamed launchers recover too.
        if _menubar_is_idle() or (
            isinstance(loaded, dict)
            and loaded != agent
            and loaded.get("ProgramArguments", [None])[:1] == ["/usr/bin/open"]
        ):
            _launchctl("bootout", AGENT_LABEL, run)
        _launchctl("bootstrap", str(path), run)
    else:
        for former_path in former_autostart_paths(home):
            former_path.unlink(missing_ok=True)
        d.atomic(
            path,
            f"[Desktop Entry]\nType=Application\nName={APP_NAME}\n"
            f"Exec={shlex.join(command)}\nIcon={desktop.DESKTOP_ENTRY_ID}\n"
            "X-GNOME-Autostart-enabled=true\nNoDisplay=true\n",
        )


def start_login_item(run: Any = subprocess.run, sleep: Any = time.sleep) -> bool:
    """Start the stopped menu bar app through its login item, so launchd supervises it
    from now on (a plain `open` would not), reloading the agent so launchd runs the
    definition on disk. False when there is none, or launchd would not take it."""
    path = agent_path()
    if not path.is_file():
        return False
    _launchctl("bootout", AGENT_LABEL, run)
    for _ in range(10):  # Booting out finishes in the background.
        try:
            result = run(
                ["launchctl", "bootstrap", f"gui/{desktop.user_id()}", str(path)],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("launchctl failed: %s", exc)
            return False
        if result.returncode == 0:
            return True
        sleep(0.2)
    return False


def _launchctl(action: str, argument: str, run: Any) -> None:
    """Apply an agent change now instead of at the next login.

    Bootstrap of an already-loaded agent fails harmlessly, as does booting one
    out that is not loaded; neither is worth reporting.
    """
    try:
        domain = f"gui/{desktop.user_id()}"
        # bootout accepts a service target OR a domain plus a plist path. A
        # bare label after the domain is treated as a file name and does not
        # unload the job, leaving the previous executable registered forever.
        target = [f"{domain}/{argument}"] if action == "bootout" else [domain, argument]
        run(
            ["launchctl", action, *target],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("launchctl failed: %s", exc)


def _menubar_is_idle() -> bool:
    """Whether the menu bar app is running; unloading its agent would quit it."""
    try:
        fd = desktop.lock(d.Paths().runtime / "menubar.lock")
    except OSError as exc:
        log.warning("could not take the login lock: %s", exc)
        return False
    if fd is None:
        return False
    os.close(fd)
    return True


def _migrate_former_agents(home: Path | None, run: Any) -> None:
    """Retire a LaunchAgent left by a former release under its old label.

    Only while the menu bar is not currently running under it: unloading a running
    agent would quit the app out from under the user with no warning. If it is
    running, this simply runs again next time (the next tick's set_login_item call,
    or the next login) once it is not.
    """
    if not _menubar_is_idle():
        return
    for label in FORMER_AGENT_LABELS:
        _launchctl("bootout", label, run)
        ((home or Path.home()) / f"Library/LaunchAgents/{label}.plist").unlink(missing_ok=True)


HISTORY_STATUS = "history-shortcut-status"
STATUS_NAMES = {"dictation": "shortcut-status", "history": HISTORY_STATUS}


@dataclass(frozen=True)
class Conflict:
    """Another shortcut on the same keys, which gets the key press instead of us."""

    name: str  # As the desktop's keyboard settings show it.
    path: str = ""  # A custom GNOME shortcut we may unbind; "" for the desktop's own.


def record_status(
    paths: d.Paths, ok: bool, name: str = "shortcut-status", conflict: Conflict | None = None
) -> None:
    """Share whether the tray or menu bar could register a shortcut with the window."""
    d.private_dir(paths.runtime)
    text = "ok" if ok else "failed"
    if ok and conflict is not None:
        text = json.dumps({"conflict": conflict.name, "path": conflict.path})
    d.atomic(paths.runtime / name, text)


def _status(paths: d.Paths, name: str) -> str:
    try:
        return (paths.runtime / name).read_text(encoding="utf-8")
    except OSError as exc:
        log.debug("could not read shortcut status: %s", exc)
        return "ok"


def shortcut_working(paths: d.Paths, name: str = "shortcut-status") -> bool:
    """Registered, and nothing else on the desktop takes the same keys."""
    status = _status(paths, name)
    return status != "failed" and shortcut_conflict(paths, name) is None


def shortcut_conflict(paths: d.Paths, name: str = "shortcut-status") -> Conflict | None:
    status = _status(paths, name)
    if not status.startswith("{"):
        return None
    try:
        data = json.loads(status)
    except ValueError as exc:
        log.debug("unreadable shortcut status: %s", exc)
        return None
    return Conflict(str(data.get("conflict", "")), str(data.get("path", "")))


def _accelerator(text: str) -> tuple[frozenset[str], str] | None:
    """A GNOME accelerator ("<Shift><Super>d") as comparable (modifiers, key)."""
    names = {"primary": "ctrl", "control": "ctrl", "mod4": "super", "mod1": "alt"}
    modifiers = frozenset(
        names.get(part.lower(), part.lower()) for part in re.findall(r"<([^>]+)>", text)
    )
    key = re.sub(r"<[^>]+>", "", text).strip().lower()
    return (modifiers, key) if key else None


def gnome_conflict(shortcut: Shortcut, path: str, run: Any = subprocess.run) -> Conflict | None:
    """The first other GNOME shortcut (custom or built in) on the same keys, if any."""
    if not shutil.which("gsettings"):
        return None
    wanted = _accelerator(shortcut.gnome())

    def gsettings(*args: str) -> str:
        result = run(["gsettings", *args], capture_output=True, text=True, timeout=10)
        return str(result.stdout).strip() if not result.returncode else ""

    try:
        listed = gsettings("get", *GNOME_LIST)
        for other in re.findall(r"'([^']+)'", listed):
            if other == path:
                continue
            schema = f"{GNOME_LIST[0]}.custom-keybinding:{other}"
            binding = gsettings("get", schema, "binding").strip("'")
            if _accelerator(binding) == wanted:
                name = gsettings("get", schema, "name").strip("'") or "another shortcut"
                return Conflict(name, other)
        for schema in GNOME_BUILT_IN:
            for line in gsettings("list-recursively", schema).splitlines():
                parts = line.split(" ", 2)
                if len(parts) < 3:
                    continue
                for value in re.findall(r"'([^']*)'", parts[2]):
                    if _accelerator(value) == wanted:
                        action = parts[1].replace("-", " ")
                        return Conflict(f"the desktop’s “{action}” shortcut")
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("gsettings conflict lookup failed: %s", exc)
        return None
    return None


def gnome_release(path: str, run: Any = subprocess.run) -> bool:
    """Unbind another custom GNOME shortcut (its name and command stay, for the user
    to bind again in the keyboard settings)."""
    if not path.startswith(GNOME_LIST_PREFIX) or not shutil.which("gsettings"):
        return False
    schema = f"{GNOME_LIST[0]}.custom-keybinding:{path}"
    try:
        result = run(
            ["gsettings", "set", schema, "binding", ""], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("gsettings release failed: %s", exc)
        return False
    return not result.returncode


def _gnome_command(command: Path | list[str]) -> str:
    return shlex.join(command) if isinstance(command, list) else shlex.quote(str(command))


# Appended to the command a desktop runs on a shortcut press (GNOME, KDE, Sway, Hyprland
# run it themselves, so the app never sees the key). The command then records that it was
# heard, first thing, and Settings can say "it works". A binding without the marker still
# runs the same command; it just can't be acknowledged.
VIA_SHORTCUT = "--via-shortcut"


def via_shortcut(command: Path | list[str]) -> list[str]:
    """`command` with the acknowledgement marker (once) as an argument list."""
    parts = list(command) if isinstance(command, list) else [str(command)]
    return parts if VIA_SHORTCUT in parts else [*parts, VIA_SHORTCUT]


def acknowledge(paths: d.Paths, kind: str) -> bool:
    """Record that this kind's shortcut was just pressed. Called first by the command the
    desktop runs (see VIA_SHORTCUT); never raises, since a press must go on to work."""
    try:
        prefs = Preferences(paths)
        shortcut = prefs.shortcut() if kind == "dictation" else prefs.history_shortcut()
        if shortcut is None:
            return False
        record_heard(paths, kind, shortcut)
    except (OSError, ValueError, d.DictationError) as exc:
        log.warning("could not record the %s shortcut press: %s", kind, exc)
        return False
    return True


def history_command(lib: Path, python: str | None = None) -> list[str]:
    """What the clipboard history shortcut runs: the window, opened on its Clipboard tab.

    `python` is an explicit override for a *different* install prefix than the one
    currently running (setup-desktop.py installing/updating another location) — when
    given, it always wins and this process's own frozen state is irrelevant.
    """
    if python is None:
        root = desktop.frozen_root()
        if root is not None:
            # Persisted as a GNOME custom keybinding's Exec= — must stay runnable
            # after an AppImage's mount point disappears, unlike a same-session relaunch().
            return desktop.persistent_relaunch("app", "--clipboard")
    return [python or desktop.overlay_python(), str(lib / "app.py"), "--clipboard"]


def gnome_shortcut(
    shortcut: Shortcut | None,
    command: Path | list[str],
    run: Any = subprocess.run,
    path: str = GNOME_PATH,
    name: str = DICTATION_SHORTCUT_NAME,
) -> bool:
    """Bind the shortcut in GNOME (Wayland apps cannot grab keys themselves).

    None pauses it (no keys) while a new shortcut is being recorded.
    """
    if not shutil.which("gsettings"):
        return False
    schema = f"{GNOME_LIST[0]}.custom-keybinding:{path}"

    def gsettings(*args: str) -> str:
        result = run(["gsettings", *args], capture_output=True, text=True, timeout=10)
        if result.returncode:
            raise OSError(result.stderr)
        return str(result.stdout).strip()

    try:
        current = gsettings("get", *GNOME_LIST)
        if path not in current:
            entries = [] if current in ("@as []", "[]") else [current.strip("[]")]
            gsettings("set", *GNOME_LIST, "[" + ", ".join(entries + [repr(path)]) + "]")
        gsettings("set", schema, "name", name)
        gsettings("set", schema, "command", _gnome_command(command))
        # Cleared first so GNOME grabs the keys again: a grab lost to another binding
        # (since released) is not retried until the binding actually changes.
        gsettings("set", schema, "binding", "")
        if shortcut:
            gsettings("set", schema, "binding", shortcut.gnome())
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("gsettings binding failed: %s", exc)
        return False
    return True


def gnome_regrab(path: str, run: Any = subprocess.run) -> bool:
    """Make GNOME grab a binding's keys again, e.g. once another binding let go of them."""
    if not path.startswith(GNOME_LIST_PREFIX) or not shutil.which("gsettings"):
        return False
    schema = f"{GNOME_LIST[0]}.custom-keybinding:{path}"
    try:
        found = run(
            ["gsettings", "get", schema, "binding"], capture_output=True, text=True, timeout=10
        )
        binding = str(found.stdout).strip().strip("'")
        if found.returncode or not binding:
            return False
        for value in ("", binding):
            run(
                ["gsettings", "set", schema, "binding", value],
                capture_output=True,
                text=True,
                timeout=10,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("gsettings regrab failed: %s", exc)
        return False
    return True


def gnome_remove(
    command: Path | list[str], run: Any = subprocess.run, path: str = GNOME_PATH
) -> None:
    """Remove the GNOME shortcut, but only if it runs this installation's command."""
    if not shutil.which("gsettings"):
        return
    schema = f"{GNOME_LIST[0]}.custom-keybinding:{path}"

    def gsettings(*args: str) -> str:
        result = run(["gsettings", *args], capture_output=True, text=True, timeout=10)
        if result.returncode:
            raise OSError(result.stderr)
        return str(result.stdout).strip()

    try:
        ours = (repr(_gnome_command(command)), repr(_gnome_command(via_shortcut(command))))
        if gsettings("get", schema, "command") not in ours:
            return
        current = gsettings("get", *GNOME_LIST)
        entries = [entry.strip() for entry in current.strip("[]").split(",") if entry.strip()]
        remaining = [entry for entry in entries if entry.strip("'\"") != path]
        gsettings("set", *GNOME_LIST, "[" + ", ".join(remaining) + "]")
        gsettings("reset-recursively", schema)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("gsettings removal failed: %s", exc)


# -- registering a shortcut on each Linux desktop -----------------------------------

BACKENDS = ("gnome", "kde", "sway", "hyprland", "wayland", "x11")


@dataclass
class RegisterResult:
    """How registering a global shortcut went, in words a user can act on.

    `ok` is whether the desktop will run our command on the keys (`bool(result)` is
    the same). `message` says what happened or what to do; `manual` is the exact line
    or settings entry for a shortcut that has to be added by hand.
    """

    ok: bool
    backend: str = ""
    message: str = ""
    manual: str = ""

    def __bool__(self) -> bool:
        return self.ok


def detect_backend(environ: dict[str, str] | None = None) -> str:
    """Which Linux desktop's mechanism can bind a global shortcut for us.

    One of BACKENDS. "wayland" is a Wayland compositor with no way for an app to
    register a shortcut (no portal use here), "x11" the same for X11 desktops that
    are not GNOME or KDE.
    """
    env = os.environ if environ is None else environ
    current = env.get("XDG_CURRENT_DESKTOP", "").lower()
    wayland = bool(env.get("WAYLAND_DISPLAY")) or env.get("XDG_SESSION_TYPE") == "wayland"
    if env.get("HYPRLAND_INSTANCE_SIGNATURE") or "hyprland" in current:
        return "hyprland"
    if env.get("SWAYSOCK") or "sway" in current:
        return "sway"
    if "kde" in current or env.get("KDE_FULL_SESSION"):
        return "kde"
    if "gnome" in current or "unity" in current or "budgie" in current or "pop" in current:
        return "gnome"
    if shutil.which("gsettings") and not current:
        return "gnome"  # No desktop named, but a GNOME settings daemon to ask.
    return "wayland" if wayland else "x11"


def manual_instructions(shortcut: Shortcut | None, command: Path | list[str], backend: str) -> str:
    """The exact text to put in this desktop's configuration by hand."""
    run = _gnome_command(command)
    if shortcut is None:
        return f"Run: {run}"
    if backend == "sway":
        return f"Add to ~/.config/sway/config:  bindsym {shortcut.sway()} exec {run}"
    if backend == "hyprland":
        modifiers, key = shortcut.hyprland()
        return f"Add to ~/.config/hypr/hyprland.conf:  bind = {modifiers}, {key}, exec, {run}"
    if backend == "kde":
        return (
            "System Settings, Keyboard, Shortcuts, Add New, Command or Script: "
            f"command {run}, shortcut {shortcut.qt()}"
        )
    if backend == "gnome":
        return f"Settings, Keyboard, Custom Shortcuts: command {run}, shortcut {shortcut.label()}"
    return f"Bind {shortcut.label()} to this command in your desktop or window manager: {run}" + (
        " (Wayland apps cannot register global shortcuts themselves.)"
        if backend == "wayland"
        else ""
    )


# The shortcut most recently bound at runtime for each name, so it can be unbound.
_bound: dict[str, Shortcut] = {}


def _tool(name: str) -> str | None:
    return shutil.which(name)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "shortcut"


def _run(run: Any, args: list[str]) -> tuple[bool, str]:
    """(worked, output or error text) for one helper command; never raises."""
    try:
        result = run(args, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("%s failed: %s", args[0], exc)
        return False, str(exc)
    text = (str(result.stdout) + " " + str(result.stderr)).strip()
    if result.returncode or "error" in text.lower():
        log.warning("%s reported: %s", args[0], text)
        return False, text
    return True, text


def _register_sway(
    shortcut: Shortcut | None, command: Path | list[str], key: str, run: Any
) -> RegisterResult:
    manual = manual_instructions(shortcut, command, "sway")
    tool = _tool("swaymsg")
    if tool is None:
        return RegisterResult(False, "sway", "swaymsg was not found. " + manual, manual)
    if key in _bound:
        _run(run, [tool, "unbindsym", _bound.pop(key).sway()])
    if shortcut is None:
        return RegisterResult(True, "sway")
    worked, text = _run(run, [tool, "bindsym", shortcut.sway(), "exec", _gnome_command(command)])
    if not worked:
        return RegisterResult(False, "sway", f"swaymsg refused it ({text}). {manual}", manual)
    _bound[key] = shortcut
    return RegisterResult(
        True,
        "sway",
        f"{shortcut.label()} is set until Sway restarts. To keep it, {manual}",
        manual,
    )


def _register_hyprland(
    shortcut: Shortcut | None, command: Path | list[str], key: str, run: Any
) -> RegisterResult:
    manual = manual_instructions(shortcut, command, "hyprland")
    tool = _tool("hyprctl")
    if tool is None:
        return RegisterResult(False, "hyprland", "hyprctl was not found. " + manual, manual)
    if key in _bound:
        modifiers, old = _bound.pop(key).hyprland()
        _run(run, [tool, "keyword", "unbind", f"{modifiers}, {old}"])
    if shortcut is None:
        return RegisterResult(True, "hyprland")
    modifiers, name = shortcut.hyprland()
    worked, text = _run(
        run, [tool, "keyword", "bind", f"{modifiers}, {name}, exec, {_gnome_command(command)}"]
    )
    # hyprctl prints "ok" on success and an error sentence otherwise.
    if not worked or text.strip().lower() not in ("ok", ""):
        return RegisterResult(False, "hyprland", f"hyprctl refused it ({text}). {manual}", manual)
    _bound[key] = shortcut
    return RegisterResult(
        True,
        "hyprland",
        f"{shortcut.label()} is set until Hyprland restarts. To keep it, {manual}",
        manual,
    )


def _register_kde(
    shortcut: Shortcut | None, command: Path | list[str], name: str, run: Any
) -> RegisterResult:
    """Write KDE's own global-shortcut entry (a launcher plus a kglobalshortcutsrc key)."""
    manual = manual_instructions(shortcut, command, "kde")
    writer = _tool("kwriteconfig6") or _tool("kwriteconfig5")
    if writer is None:
        return RegisterResult(False, "kde", "kwriteconfig was not found. " + manual, manual)
    service = f"clipboardplus-{_slug(name)}.desktop"
    folder = Path.home() / ".local/share/applications"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / service).write_text(
            "[Desktop Entry]\nType=Application\n"
            f"Name={name}\nExec={_gnome_command(command)}\nNoDisplay=true\n"
            "X-KDE-GlobalAccel-CommandShortcut=true\n",
            encoding="utf-8",
        )
    except OSError as exc:
        log.warning("could not write %s: %s", service, exc)
        return RegisterResult(
            False, "kde", f"Could not save the launcher ({exc}). {manual}", manual
        )
    value = shortcut.qt() if shortcut else "none"
    worked, text = _run(
        run,
        [writer, "--file", "kglobalshortcutsrc", "--group", "services", "--group", service]
        + ["--key", "_launch", value],
    )
    if not worked:
        return RegisterResult(False, "kde", f"kwriteconfig failed ({text}). {manual}", manual)
    if shortcut is None:
        return RegisterResult(True, "kde")
    return RegisterResult(
        True,
        "kde",
        f"{shortcut.label()} is saved for KDE. It starts working after you log out and back "
        f"in; if it doesn’t, {manual}",
        manual,
    )


def register_shortcut(
    shortcut: Shortcut | None,
    command: Path | list[str],
    *,
    backend: str | None = None,
    run: Any = subprocess.run,
    path: str = GNOME_PATH,
    name: str = DICTATION_SHORTCUT_NAME,
) -> RegisterResult:
    """Bind `shortcut` to `command` in whichever way this desktop allows.

    None pauses it. A failure never raises: the result says why, and gives the exact
    line or setting to add by hand, so the app can show it instead of failing silently.
    """
    backend = backend or detect_backend()
    command = via_shortcut(command)
    if backend == "sway":
        return _register_sway(shortcut, command, path, run)
    if backend == "hyprland":
        return _register_hyprland(shortcut, command, path, run)
    if backend == "kde":
        return _register_kde(shortcut, command, name, run)
    manual = manual_instructions(shortcut, command, backend)
    if backend == "gnome" or shutil.which("gsettings"):
        if gnome_shortcut(shortcut, command, run, path=path, name=name):
            return RegisterResult(True, "gnome")
        return RegisterResult(
            False,
            "gnome",
            "GNOME’s settings could not be changed (is gsettings working?). " + manual,
            manual,
        )
    if backend == "wayland":
        message = "This Wayland desktop doesn’t let apps register a global shortcut. " + manual
    else:
        message = "This desktop has no shortcut service Clipboard+ can use. " + manual
    return RegisterResult(False, backend, message, manual)


def record_message(paths: d.Paths, message: str, name: str = "shortcut-status") -> None:
    """Keep why a shortcut isn't working, next to its status, for the window to show."""
    d.private_dir(paths.runtime)
    target = paths.runtime / (name + "-message")
    if message:
        d.atomic(target, message)
    else:
        target.unlink(missing_ok=True)


def shortcut_message(paths: d.Paths, name: str = "shortcut-status") -> str:
    try:
        return (paths.runtime / (name + "-message")).read_text(encoding="utf-8")
    except OSError as exc:
        log.debug("could not read the shortcut message: %s", exc)
        return ""


def record_heard(paths: d.Paths, kind: str, shortcut: Shortcut, now: float | None = None) -> None:
    """Note that the registered shortcut was just pressed, for the window to show."""
    d.private_dir(paths.runtime)
    payload = {"at": time.time() if now is None else now, "shortcut": shortcut.label()}
    d.atomic(paths.runtime / f"shortcut-heard-{kind}", json.dumps(payload))


def heard_at(paths: d.Paths, kind: str, shortcut: Shortcut) -> float | None:
    """When this very shortcut was last pressed and acknowledged, or None."""
    try:
        data = json.loads((paths.runtime / f"shortcut-heard-{kind}").read_text(encoding="utf-8"))
        when, label = float(data["at"]), str(data["shortcut"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        log.debug("no shortcut ack: %s", exc)
        return None
    return when if label == shortcut.label() else None


def heard_recently(
    paths: d.Paths,
    kind: str,
    shortcut: Shortcut,
    now: float | None = None,
    within: float = HEARD_SECONDS,
) -> bool:
    """Whether this very shortcut was pressed in the last `within` seconds."""
    when = heard_at(paths, kind, shortcut)
    if when is None:
        return False
    age = (time.time() if now is None else now) - when
    return -2.0 <= age <= within


def record_outcome(
    paths: d.Paths, kind: str, shortcut: Shortcut, heard: bool, now: float | None = None
) -> None:
    """Keep how a "test your shortcut" ended, so Home and Settings stay truthful later."""
    d.private_dir(paths.runtime)
    payload = {
        "at": time.time() if now is None else now,
        "shortcut": shortcut.label(),
        "result": "heard" if heard else "silent",
    }
    d.atomic(paths.runtime / f"shortcut-test-{kind}", json.dumps(payload))


def shortcut_outcome(paths: d.Paths, kind: str, shortcut: Shortcut) -> str:
    """ "heard" (this shortcut's press reached us), "silent" (a test ended without hearing
    it, and no press since), or "untested"."""
    pressed = heard_at(paths, kind, shortcut)
    try:
        data = json.loads((paths.runtime / f"shortcut-test-{kind}").read_text(encoding="utf-8"))
        when, label, result = float(data["at"]), str(data["shortcut"]), str(data["result"])
    except (OSError, ValueError, KeyError, TypeError):
        when, label, result = 0.0, "", ""
    tested = label == shortcut.label()
    if pressed is not None and (not tested or pressed >= when):
        return "heard"
    if tested and result == "heard":
        return "heard"
    return "silent" if tested and result == "silent" else "untested"


def log_location(name: str) -> str:
    """Where a component's log file is, for messages that send the user there."""
    try:
        import logsetup

        return str(logsetup.log_dir() / f"{name}.log")
    except (ImportError, OSError):
        return f"the {name}.log file in the app's logs folder"


def open_link(url: str = CLIPBOARD_PLUS) -> None:
    import webbrowser

    webbrowser.open(url)


python_for_gui = desktop.python_for_gui
