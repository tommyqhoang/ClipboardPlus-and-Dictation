"""The macOS menu bar (lib/menubar.py) and the Linux/Windows tray (lib/tray.py) must not drift.

A user-facing capability that exists in one and not the other fails here. Each row below
names the concrete call (usually into a shared module) that implements the capability,
for each side. If you add a capability to one file, add it to the other (or, when the
platform genuinely cannot do it, add an entry to ALLOWED with the reason). The tests read
the source text, so they run identically on Linux, macOS and Windows.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

LIB = Path(__file__).resolve().parents[1] / "lib"
MENUBAR = (LIB / "menubar.py").read_text(encoding="utf-8")
TRAY = (LIB / "tray.py").read_text(encoding="utf-8")
SOURCES = {"menubar": MENUBAR, "tray": TRAY}

# capability -> (regex in menubar.py, regex in tray.py). Prefer a shared helper both call.
CAPABILITIES: dict[str, tuple[str, str]] = {
    "recent copies use one label formatter": (
        r"clipcontrol\.preview_text\(",
        r"clipcontrol\.preview_text\(",
    ),
    "image rows show a thumbnail": (r"\.thumb_path\(", r"\.thumb_path\("),
    "open the clipboard history": (
        r'open_window\("--clipboard"\)',
        r'open_window\("--clipboard"\)',
    ),
    "search the history": (r"NSSearchField", r'"Search Clipboard History'),
    "clear the history": (r"service\.clear_clipboard\(", r'open_window\("--clipboard-clear"\)'),
    "toggle dictation": (r"def pressed\(", r"def pressed\("),
    "cancel a recording": (r'run_engine\("--cancel"\)', r'run_engine\("--cancel"\)'),
    "copy the last transcript": (r'run_engine\("--copy-last"\)', r'run_engine\("--copy-last"\)'),
    "open Settings": (r'open_window\("--settings"\)', r'open_window\("--settings"\)'),
    "offer a newer release": (r"updates\.check\(", r"updates\.check\("),
    "open at login": (r"hotkeys\.set_login_item\(", r"hotkeys\.set_login_item\("),
    "dictation shortcut is registered from the saved preference": (
        r"self\.preferences\.shortcut\(\)",
        r"self\.preferences\.shortcut\(\)",
    ),
    "history shortcut follows the clipboard controller": (
        r"self\.clip\.history_shortcut\(\)",
        r"self\.clip\.history_shortcut\(\)",
    ),
    "shortcut recorder pauses both shortcuts (capture flags)": (
        r"hotkeys\.CAPTURE_FLAGS",
        r"hotkeys\.CAPTURE_FLAGS",
    ),
    "shortcut recorder acknowledges a heard press": (
        r"hotkeys\.record_heard\(",
        r"hotkeys\.record_heard\(",
    ),
    "the clipboard service is supervised by the shared controller": (
        r"self\.clip\.supervise\(\)",
        r"self\.clip\.supervise\(\)",
    ),
    "copying a row goes through the shared service": (
        r"service\.copy_item\(",
        r"service\.copy_item\(",
    ),
    "quit": (r"def quit_?\(", r"def quit\("),
}

# Genuine, deliberate differences. Each entry: (capability, side that has it) -> reason.
# A test below fails if the "missing" side gains the capability, so this list cannot rot.
ALLOWED: dict[str, tuple[str, str, str]] = {
    "install an offered release from the menu": (
        "tray",
        r"updates\.start_updater\(",
        "The macOS menu bar only announces the update and points at Settings, where "
        "app.py installs it; the tray also has an 'Update now' item. Both use "
        "updates.check to learn of it.",
    ),
    "pause and resume clipboard capture": (
        "tray",
        r"self\.clip\.pause\(.*self\.clip\.resume|self\.clip\.resume",
        "KNOWN GAP, not a platform limit: the macOS popover has no pause control yet "
        "(popover is a slim search + clear layout); the tray offers 'Pause Clipboard "
        "Capture'. Remove this entry when the popover gets one.",
    ),
    "pick or record the dictation shortcut from the menu": (
        "tray",
        r'"Record New Shortcut',
        "The macOS popover leaves shortcut choice to the Settings window; the tray "
        "keeps a More submenu. Both record through the same --shortcut window.",
    ),
}


class ParityTests(unittest.TestCase):
    def test_every_capability_exists_on_both_sides(self):
        missing = []
        for name, (mac, tray) in CAPABILITIES.items():
            for side, pattern in (("menubar.py", mac), ("tray.py", tray)):
                source = MENUBAR if side == "menubar.py" else TRAY
                if not re.search(pattern, source):
                    missing.append(f"{name}: missing in {side} (expected /{pattern}/)")
        self.assertEqual(missing, [], "\n".join(missing))

    def test_shared_helpers_are_defined_where_both_look(self):
        # The names both files call must still exist in the shared modules.
        expectations = {
            "clipcontrol.py": ("def preview_text(", "def image_label(", "def supervise("),
            "hotkeys.py": ("CAPTURE_FLAGS =", "def record_heard(", "def set_login_item("),
            "updates.py": ("def check(", "def start_updater("),
            "clipstore.py": ("def thumb_path(",),
        }
        for module, needles in expectations.items():
            text = (LIB / module).read_text(encoding="utf-8")
            for needle in needles:
                self.assertIn(needle, text, f"{module} lost {needle}")

    def test_allowed_differences_are_real_and_explained(self):
        for name, (has, pattern, reason) in ALLOWED.items():
            self.assertGreater(len(reason), 40, f"{name}: say why")
            other = "menubar" if has == "tray" else "tray"
            self.assertTrue(re.search(pattern, SOURCES[has]), f"{name}: {has} lost it")
            self.assertFalse(
                re.search(pattern, SOURCES[other]),
                f"{name}: {other} now has it; drop the ALLOWED entry and add a CAPABILITY",
            )

    def test_menu_rows_share_one_label_formatter(self):
        # Neither file may format an image clip's label by hand.
        for name, source in SOURCES.items():
            self.assertNotRegex(source, r'f"Image ', f"{name}.py formats image labels itself")


if __name__ == "__main__":
    unittest.main()
