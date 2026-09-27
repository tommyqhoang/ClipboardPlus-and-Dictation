"""Shortcut and open-at-login preferences shared by the macOS menu bar and the
Windows/Linux tray. Pure data and files only; GUI toolkits live elsewhere.
"""

from __future__ import annotations

import json
import plistlib
import re
import shlex
import shutil
import string
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import desktop
import dictation as d

# Shown wherever the app names itself. Identifiers, folders and the bundle id keep the
# original "whisper-dictation" spelling so upgrades keep working.
# Formerly "Whisper Dictation & Clipboard+"; identifiers below stay stable for upgrades.
APP_NAME = "Clipboard+ and Dictation"
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


def default_shortcut(platform: str) -> Shortcut:
    """A shortcut no Chrome shortcut uses (checked against Chrome's published list).

    Chrome takes Alt/Ctrl/Ctrl+Shift/⌘/⌘⇧ + D and has no Win/Super or ⌃⌥ combinations,
    so Windows and Linux use Super+Shift+D and macOS, which has no Super key, uses ⌃⌥⇧D.
    """
    if platform == "macos":
        return Shortcut(("ctrl", "alt", "shift"), "D")
    return Shortcut(("shift", "cmd"), "D")


DEFAULT = default_shortcut(desktop.platform_name())
# Deduplicated: on macOS the default is also the Ctrl+Alt+Shift+D preset.
PRESETS = tuple(
    dict.fromkeys(
        (
            DEFAULT,
            # Alt+Space opens the window menu on GNOME and Windows, so only macOS offers it.
            *((Shortcut(("alt",), "Space"),) if desktop.platform_name() == "macos" else ()),
            Shortcut(("ctrl", "alt"), "Space"),
            Shortcut(("ctrl", "shift"), "Space"),
            Shortcut(("ctrl", "alt", "shift"), "D"),
            Shortcut(("ctrl", "alt"), "D"),  # The earlier default, one click away.
        )
    )
)


def default_history_shortcut(platform: str) -> Shortcut:
    """Opens the clipboard history: the dictation default with F (for find) instead of D."""
    return Shortcut(default_shortcut(platform).modifiers, "F")


DEFAULT_HISTORY = default_history_shortcut(desktop.platform_name())
HISTORY_PRESETS = tuple(
    dict.fromkeys(
        (
            DEFAULT_HISTORY,
            Shortcut(("ctrl", "alt", "shift"), "V"),
            Shortcut(("ctrl", "alt"), "H"),
        )
    )
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
        except (d.DictationError, OSError, ValueError):
            return {}

    def stamp(self) -> float:
        try:
            return self.path.stat().st_mtime
        except OSError:
            return 0.0

    @staticmethod
    def _shortcut(raw: Any, default: Shortcut) -> Shortcut:
        if not isinstance(raw, dict):
            return default
        modifiers, key = raw.get("modifiers"), raw.get("key")
        if not isinstance(modifiers, list) or not isinstance(key, str):
            return default
        shortcut = Shortcut(canonical(modifiers), key)
        return default if shortcut.problem() else shortcut

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
        """Anonymous crash reports and usage statistics (telemetry.py): on unless declined."""
        return self.read().get("share_usage", True) is not False

    def save(
        self,
        shortcut: Shortcut | None = None,
        open_at_login: bool | None = None,
        features: Features | None = None,
        clipboard: ClipboardSettings | None = None,
        history_shortcut: Shortcut | Literal[False] | None = None,
        share_usage: bool | None = None,
    ) -> None:
        """Change the given preferences. `history_shortcut=False` switches it off."""
        values = self.read()
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
            f"[Desktop Entry]\nType=Application\nName={APP_NAME}\n"
            f"Exec={shlex.join(command)}\nIcon=whisper-dictation\n"
            "X-GNOME-Autostart-enabled=true\nNoDisplay=true\n",
        )


HISTORY_STATUS = "history-shortcut-status"


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
    except OSError:
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
    except ValueError:
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
    except (OSError, subprocess.SubprocessError):
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
    except (OSError, subprocess.SubprocessError):
        return False
    return not result.returncode


def _gnome_command(command: Path | list[str]) -> str:
    return shlex.join(command) if isinstance(command, list) else shlex.quote(str(command))


def history_command(lib: Path, python: str | None = None) -> list[str]:
    """What the clipboard history shortcut runs: the window, opened on its Clipboard tab."""
    return [python or python_for_gui(), str(lib / "app.py"), "--clipboard"]


def gnome_shortcut(
    shortcut: Shortcut | None,
    command: Path | list[str],
    run: Any = subprocess.run,
    path: str = GNOME_PATH,
    name: str = APP_NAME,
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
        gsettings("set", schema, "binding", shortcut.gnome() if shortcut else "")
    except (OSError, subprocess.SubprocessError):
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
        if gsettings("get", schema, "command") != repr(_gnome_command(command)):
            return
        current = gsettings("get", *GNOME_LIST)
        entries = [entry.strip() for entry in current.strip("[]").split(",") if entry.strip()]
        remaining = [entry for entry in entries if entry.strip("'\"") != path]
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
