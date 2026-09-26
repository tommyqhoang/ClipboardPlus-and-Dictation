"""Shortcut and open-at-login preferences shared by the macOS menu bar and the
Windows/Linux tray. Pure data and files only; GUI toolkits live elsewhere.
"""

from __future__ import annotations

import json
import plistlib
import shlex
import shutil
import string
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import desktop
import dictation as d

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
    "Meta_L": "alt",
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
CLIPBOARD_PLUS = "https://clipboardplus.apercallc.com"
AGENT_LABEL = "org.whisperdictation.menubar"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
GNOME_PATH = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/dictation/"
GNOME_LIST = ("org.gnome.settings-daemon.plugins.media-keys", "custom-keybindings")


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

    def problem(self) -> str:
        """Why this cannot be a global shortcut, or an empty string."""
        if self.key not in KEYS:
            return "Use a letter, number, Space, or F1–F12."
        if self.key in FUNCTION_KEYS:
            return ""
        if not {"ctrl", "alt", "cmd"} & set(self.modifiers):
            return "Include Ctrl, Alt/Option, or ⌘/Win so normal typing isn’t captured."
        if self.modifiers in (("ctrl",), ("cmd",)) and self.key in set("ACFHMNOPQSTVWXYZ"):
            return "Most apps already use that shortcut. Add Alt/Option or Shift."
        return ""

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


# The same physical keys on every platform: Super (Win, ⌘) + Shift + D. Browsers
# already use Ctrl+Alt+D-style combinations, so this avoids them.
DEFAULT = Shortcut(("shift", "cmd"), "D")
PRESETS = (
    DEFAULT,
    Shortcut(("alt",), "Space"),
    Shortcut(("ctrl", "alt"), "Space"),
    Shortcut(("ctrl", "shift"), "Space"),
    Shortcut(("ctrl", "alt", "shift"), "D"),
    Shortcut(("ctrl", "alt"), "D"),  # The earlier default, one click away after upgrading.
)


def from_mac_event(key_code: int, flags: int, characters: str) -> Shortcut:
    key = MAC_KEY_NAMES.get(key_code) or characters.strip().upper()
    return Shortcut(canonical(n for n in EVENT_FLAGS if flags & EVENT_FLAGS[n]), key)


def from_tk(keysym: str, held: set[str]) -> Shortcut | None:
    """A shortcut from a Tk key press, or None while only modifiers are held."""
    if keysym in TK_MODIFIERS:
        return None
    key = TK_NAMES.get(keysym) or (keysym.upper() if len(keysym) == 1 else keysym)
    return Shortcut(canonical(held), key)


class Preferences:
    def __init__(self, paths: d.Paths) -> None:
        self.path = paths.config.parent / "menubar.json"

    def read(self) -> dict[str, Any]:
        try:
            return d.read_json(self.path)
        except (d.DictationError, OSError, ValueError):
            return {}

    def stamp(self) -> float:
        try:
            return self.path.stat().st_mtime
        except OSError:
            return 0.0

    def shortcut(self) -> Shortcut:
        raw = self.read().get("shortcut")
        if not isinstance(raw, dict):
            return DEFAULT
        modifiers, key = raw.get("modifiers"), raw.get("key")
        if not isinstance(modifiers, list) or not isinstance(key, str):
            return DEFAULT
        shortcut = Shortcut(canonical(modifiers), key)
        return DEFAULT if shortcut.problem() else shortcut

    def open_at_login(self) -> bool:
        return self.read().get("open_at_login", True) is not False

    def save(self, shortcut: Shortcut | None = None, open_at_login: bool | None = None) -> None:
        values = self.read()
        if shortcut is not None:
            values["shortcut"] = {"modifiers": list(shortcut.modifiers), "key": shortcut.key}
        if open_at_login is not None:
            values["open_at_login"] = open_at_login
        d.private_dir(self.path.parent)
        d.atomic(self.path, json.dumps(values, indent=2))


def agent_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / f"Library/LaunchAgents/{AGENT_LABEL}.plist"


def autostart_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".config/autostart/whisper-dictation.desktop"


def set_login_item(
    enabled: bool,
    command: list[str],
    home: Path | None = None,
    platform: str | None = None,
    registry: Any = None,
) -> None:
    """Start the tray/menu bar app when the user logs in."""
    platform = platform or desktop.platform_name()
    if platform == "windows":
        winreg = registry or __import__("winreg")
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            if enabled:
                value = subprocess.list2cmdline(command)
                winreg.SetValueEx(key, "WhisperDictation", 0, winreg.REG_SZ, value)
            else:
                try:
                    winreg.DeleteValue(key, "WhisperDictation")
                except FileNotFoundError:
                    pass
        return
    path = agent_path(home) if platform == "macos" else autostart_path(home)
    if not enabled:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if platform == "macos":
        agent = {
            "Label": AGENT_LABEL,
            "ProgramArguments": command,
            "RunAtLoad": True,
            "ProcessType": "Interactive",
        }
        d.atomic(path, plistlib.dumps(agent).decode("utf-8"))
    else:
        d.atomic(
            path,
            "[Desktop Entry]\nType=Application\nName=Whisper Dictation\n"
            f"Exec={shlex.join(command)}\nIcon=whisper-dictation\n"
            "X-GNOME-Autostart-enabled=true\nNoDisplay=true\n",
        )


def record_status(paths: d.Paths, ok: bool) -> None:
    """Share whether the tray or menu bar could register the shortcut with the window."""
    d.private_dir(paths.runtime)
    d.atomic(paths.runtime / "shortcut-status", "ok" if ok else "failed")


def shortcut_working(paths: d.Paths) -> bool:
    try:
        return (paths.runtime / "shortcut-status").read_text(encoding="utf-8") != "failed"
    except OSError:
        return True


def gnome_shortcut(shortcut: Shortcut | None, command: Path, run: Any = subprocess.run) -> bool:
    """Bind the shortcut in GNOME (Wayland apps cannot grab keys themselves).

    None pauses it (no keys) while a new shortcut is being recorded.
    """
    if not shutil.which("gsettings"):
        return False
    schema = f"{GNOME_LIST[0]}.custom-keybinding:{GNOME_PATH}"

    def gsettings(*args: str) -> str:
        result = run(["gsettings", *args], capture_output=True, text=True, timeout=10)
        if result.returncode:
            raise OSError(result.stderr)
        return str(result.stdout).strip()

    try:
        current = gsettings("get", *GNOME_LIST)
        if GNOME_PATH not in current:
            entries = [] if current in ("@as []", "[]") else [current.strip("[]")]
            gsettings("set", *GNOME_LIST, "[" + ", ".join(entries + [repr(GNOME_PATH)]) + "]")
        gsettings("set", schema, "name", "Whisper Dictation")
        gsettings("set", schema, "command", shlex.quote(str(command)))
        gsettings("set", schema, "binding", shortcut.gnome() if shortcut else "")
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def gnome_remove(command: Path, run: Any = subprocess.run) -> None:
    """Remove the GNOME shortcut, but only if it runs this installation's command."""
    if not shutil.which("gsettings"):
        return
    schema = f"{GNOME_LIST[0]}.custom-keybinding:{GNOME_PATH}"

    def gsettings(*args: str) -> str:
        result = run(["gsettings", *args], capture_output=True, text=True, timeout=10)
        if result.returncode:
            raise OSError(result.stderr)
        return str(result.stdout).strip()

    try:
        if gsettings("get", schema, "command") != repr(shlex.quote(str(command))):
            return
        current = gsettings("get", *GNOME_LIST)
        entries = [entry.strip() for entry in current.strip("[]").split(",") if entry.strip()]
        remaining = [entry for entry in entries if entry.strip("'\"") != GNOME_PATH]
        gsettings("set", *GNOME_LIST, "[" + ", ".join(remaining) + "]")
        gsettings("reset-recursively", schema)
    except (OSError, subprocess.SubprocessError):
        pass


def open_link(url: str = CLIPBOARD_PLUS) -> None:
    import webbrowser

    webbrowser.open(url)


def python_for_gui() -> str:
    """pythonw on Windows so no console window flashes."""
    python = Path(sys.executable)
    windowed = python.with_name("pythonw.exe")
    return str(windowed if windowed.exists() else python)
