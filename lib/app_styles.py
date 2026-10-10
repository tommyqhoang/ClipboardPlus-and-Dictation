"""The Clipboard+ window's look: one palette, its fonts, and its ttk styles.

Split from app.py so the visual design lives in one place. Nothing here creates a
window; each function only configures the ttk.Style (or returns plain data) it is given.
"""

from __future__ import annotations

import logging
import tkinter
from pathlib import Path
from tkinter import font, ttk

log = logging.getLogger(__name__)

# Rounded, modern widgets: the Sun Valley theme recolored to the logo's amber (built by
# tools/build_theme.py). Without the file the classic styles below still work.
THEME_FILE = Path(__file__).with_name("clipboardplus-theme.tcl")
THEME_NAME = "clipboardplus"
load_error = ""  # Why the modern theme could not load, for diagnostics and tests.

# One palette for every surface; the generated Clipboard+ icon is used everywhere.
BACKGROUND = "#f5f7fa"
SURFACE = "#ffffff"
BORDER = "#e2e8f0"
TEXT = "#1f2937"
MUTED = "#5b6472"
ACCENT = "#b45309"  # The logo's amber, darkened to read on white.
ACCENT_ACTIVE = "#8f3f0b"
ACCENT_SOFT = "#fff5e8"
DANGER = "#b42318"
DANGER_ACTIVE = "#912018"
WARNING = "#a15c07"
IDLE = "#98a2b3"
HOVER = "#f8fafc"  # A list row under the pointer.

Fonts = dict[str, tuple[str, int, str]]


def make_fonts() -> Fonts:
    """The text sizes, following the desktop's own default (and its display scaling)."""
    system = font.nametofont("TkDefaultFont").actual()
    family = str(system["family"])
    # Follow the desktop's own text size (and its display scaling) instead of fixed,
    # oversized points: 13 on a Mac, about 10 on Linux and 9 on Windows.
    base = max(9, min(13, abs(int(system["size"])) or 10))
    return {
        "title": (family, base + 7, "bold"),
        "heading": (family, base + 2, "bold"),
        "body": (family, base, "normal"),
        "small": (family, max(8, base - 1), "normal"),
        "brand": (family, base + 2, "bold"),
        "badge": (family, base, "bold"),
        "record": (family, base + 2, "bold"),
        "icon": (family, base + 3, "normal"),
    }


def make_colors() -> dict[str, str]:
    """For widgets drawn outside ttk (the clipboard list), whose rows change on hover."""
    return {
        "surface": SURFACE,
        "hover": HOVER,
        "selected": ACCENT_SOFT,  # The row Enter copies.
        "border": BORDER,
        "text": TEXT,
        "muted": MUTED,
        "accent": ACCENT,
        "danger": DANGER,
        "star": WARNING,
    }


def configure_labels(style: ttk.Style, fonts: Fonts) -> None:
    style.configure(".", background=BACKGROUND, foreground=TEXT, font=fonts["body"])
    style.configure("TFrame", background=BACKGROUND)
    style.configure("Card.TFrame", background=SURFACE)
    style.configure("Header.TFrame", background=SURFACE)
    style.configure("TLabel", background=BACKGROUND, foreground=TEXT)
    style.configure("Title.TLabel", font=fonts["title"])
    style.configure("Hint.TLabel", foreground=MUTED, font=fonts["small"])
    style.configure(
        "Toast.TLabel",
        background=TEXT,
        foreground=SURFACE,
        font=fonts["small"],
        padding=(12, 8),
    )
    style.configure("Error.TLabel", foreground=DANGER, font=fonts["small"])
    style.configure("Subtitle.TLabel", foreground=MUTED, font=fonts["body"])
    style.configure("Card.TLabel", background=SURFACE)
    style.configure("CardHeading.TLabel", background=SURFACE, font=fonts["heading"])
    style.configure("CardHint.TLabel", background=SURFACE, foreground=MUTED, font=fonts["small"])
    style.configure("CardError.TLabel", background=SURFACE, foreground=DANGER, font=fonts["body"])
    style.configure("Brand.TLabel", background=SURFACE, font=fonts["brand"])
    style.configure("Step.TLabel", background=SURFACE, foreground=MUTED, font=fonts["small"])


def configure_inputs(style: ttk.Style, fonts: Fonts) -> None:
    style.configure(
        "Card.TRadiobutton",
        background=SURFACE,
        foreground=TEXT,
        font=fonts["body"],
        indicatorcolor=SURFACE,
        indicatorbackground=SURFACE,
    )
    style.map(
        "Card.TRadiobutton",
        background=[("active", SURFACE)],
        indicatorcolor=[("selected", ACCENT)],
    )
    style.configure(
        "TCombobox",
        fieldbackground=SURFACE,
        background=SURFACE,
        bordercolor=BORDER,
        lightcolor=SURFACE,
        darkcolor=SURFACE,
        arrowcolor=MUTED,
        padding=4,
    )
    style.map("TCombobox", fieldbackground=[("readonly", SURFACE)])
    style.configure(
        "TEntry",
        fieldbackground=SURFACE,
        bordercolor=BORDER,
        lightcolor=SURFACE,
        darkcolor=SURFACE,
        padding=5,
    )
    style.map("TEntry", bordercolor=[("focus", ACCENT)], lightcolor=[("focus", ACCENT)])


def configure_buttons(style: ttk.Style, fonts: Fonts) -> None:
    buttons = {
        "TButton": (SURFACE, TEXT, BORDER, "#edf1f6", SURFACE),
        "Primary.TButton": (ACCENT, "white", ACCENT, ACCENT_ACTIVE, "#e7c5a7"),
        "Danger.TButton": (DANGER, "white", DANGER, DANGER_ACTIVE, "#e8b4af"),
    }
    for name, (fill, ink, edge, active, muted) in buttons.items():
        style.configure(
            name,
            background=fill,
            foreground=ink,
            bordercolor=edge,
            lightcolor=fill,
            darkcolor=fill,
            focuscolor=ACCENT,
            relief="solid",
            borderwidth=1,
            padding=(14, 7),
            font=fonts["body"],
        )
        style.map(
            name,
            background=[("disabled", muted), ("pressed", active), ("active", active)],
            lightcolor=[("disabled", muted), ("pressed", active), ("active", active)],
            darkcolor=[("disabled", muted), ("pressed", active), ("active", active)],
            bordercolor=[
                ("disabled", BORDER if fill == SURFACE else muted),
                ("focus", ACCENT),
            ],
            foreground=[("disabled", "#98a2b3" if fill == SURFACE else "white")],
        )
    # Compact buttons for dense lists (clipboard rows, filters).
    # The theme's buttons are at least 11 characters wide; small ones fit their words.
    for name in ("Small.TButton", "Small.Primary.TButton", "Small.Danger.TButton"):
        style.configure(name, padding=(10, 4), font=fonts["small"], width=-6)
    # Idle Record is neutral: the shortcut, not this button, is the main way in.
    for name in ("", "Primary.", "Danger."):
        style.configure(f"Record.{name}TButton", padding=(18, 10), font=fonts["record"])


def configure_tabs(style: ttk.Style, fonts: Fonts) -> None:
    # Header tabs: quiet text, the current one filled.
    style.configure(
        "Tab.TButton",
        background=SURFACE,
        foreground=MUTED,
        bordercolor=SURFACE,
        lightcolor=SURFACE,
        darkcolor=SURFACE,
        focuscolor=SURFACE,
        relief="flat",
        padding=(13, 7),
        font=fonts["body"],
    )
    style.map(
        "Tab.TButton",
        background=[("active", ACCENT_SOFT)],
        lightcolor=[("active", ACCENT_SOFT)],
        darkcolor=[("active", ACCENT_SOFT)],
        bordercolor=[("active", ACCENT_SOFT)],
        foreground=[("active", ACCENT)],
    )
    style.configure(
        "Tab.Current.TButton",
        background=ACCENT_SOFT,
        foreground=ACCENT,
        bordercolor=ACCENT_SOFT,
        lightcolor=ACCENT_SOFT,
        darkcolor=ACCENT_SOFT,
        focuscolor=ACCENT_SOFT,
        relief="flat",
        padding=(13, 7),
        font=fonts["body"],
    )
    style.map(
        "Tab.Current.TButton",
        background=[("active", ACCENT_SOFT)],
        lightcolor=[("active", ACCENT_SOFT)],
        darkcolor=[("active", ACCENT_SOFT)],
        bordercolor=[("active", ACCENT_SOFT)],
        foreground=[("active", ACCENT_ACTIVE)],
    )


def configure_misc(style: ttk.Style, fonts: Fonts, modern: bool = False) -> None:
    style.configure("Toolbar.TFrame", background=BACKGROUND)
    style.configure("Placeholder.TLabel", background=SURFACE, foreground=IDLE, font=fonts["body"])
    if modern:
        return
    style.configure(
        "Card.TCheckbutton",
        background=SURFACE,
        foreground=TEXT,
        font=fonts["body"],
        indicatorbackground=SURFACE,
    )
    style.map(
        "Card.TCheckbutton",
        background=[("active", SURFACE)],
        indicatorcolor=[("selected", ACCENT)],
    )
    style.configure(
        "Link.TButton",
        background=BACKGROUND,
        foreground=ACCENT,
        bordercolor=BACKGROUND,
        lightcolor=BACKGROUND,
        darkcolor=BACKGROUND,
        focuscolor=BACKGROUND,
        relief="flat",
        padding=(4, 6),
    )
    style.map(
        "Link.TButton",
        foreground=[("disabled", IDLE), ("active", ACCENT_ACTIVE)],
        background=[("active", BACKGROUND)],
    )
    style.configure(
        "Vertical.TScrollbar",
        background="#cbd5e1",
        troughcolor=BACKGROUND,
        bordercolor=BACKGROUND,
        lightcolor="#cbd5e1",
        darkcolor="#cbd5e1",
        arrowcolor=MUTED,
        relief="flat",
    )
    style.configure(
        "TProgressbar",
        background=ACCENT,
        troughcolor="#e8edf3",
        bordercolor="#e8edf3",
        lightcolor=ACCENT,
        darkcolor=ACCENT,
        thickness=6,
    )


def load_theme(style: ttk.Style) -> bool:
    """Switch to the modern theme; False (and the clam theme) when it cannot load."""
    global load_error
    try:
        style.tk.call("source", str(THEME_FILE))
        style.theme_use(THEME_NAME)
        # The theme resets "." when it is applied; let that finish before ours.
        style.master.update_idletasks()
    except (tkinter.TclError, OSError) as exc:
        load_error = str(exc)
        log.warning("modern theme unavailable, using the classic one: %s", exc)
        style.theme_use("clam")
        return False
    return True


def configure_modern(style: ttk.Style, fonts: Fonts) -> None:
    """Fit our palette and fonts onto the modern theme, whose widgets are drawn from
    images: only text, spacing and which image a named style borrows are set here."""
    style.configure("TEntry", padding=(8, 5), foreground=TEXT, insertcolor=TEXT, font=fonts["body"])
    style.configure("TCombobox", padding=(8, 5), foreground=TEXT, font=fonts["body"])
    style.configure("Panel.TFrame", background=BACKGROUND)
    for name in ("Card.TRadiobutton", "Card.TCheckbutton"):
        style.configure(name, background=SURFACE, foreground=TEXT, font=fonts["body"])
        style.map(name, background=[("active", SURFACE)])
    # Buttons borrow the theme's accent (amber) and danger (red) images.
    for name, borrowed, ink in (
        ("Primary.TButton", "Accent.TButton", "white"),
        ("Danger.TButton", "Danger.TButton", "white"),
        ("Tab.Current.TButton", "Accent.TButton", "white"),
        ("Tab.TButton", "Toolbutton", MUTED),
        ("Link.TButton", "Toolbutton", ACCENT),
    ):
        style.layout(name, style.layout(borrowed))
        style.configure(name, foreground=ink, font=fonts["body"], padding=(14, 7))
    style.map("Primary.TButton", foreground=[("disabled", "#ffffff"), ("pressed", "#f6dcc0")])
    style.map("Tab.Current.TButton", foreground=[("disabled", "#ffffff"), ("pressed", "#f6dcc0")])
    style.map("Danger.TButton", foreground=[("disabled", "#ffffff"), ("pressed", "#f6d0cb")])
    style.map("Tab.TButton", foreground=[("active", TEXT)])
    style.map("Link.TButton", foreground=[("disabled", IDLE), ("active", ACCENT_ACTIVE)])
    style.configure("TButton", padding=(14, 7), font=fonts["body"])
    style.configure("Tab.TButton", padding=(13, 6))
    style.configure("Tab.Current.TButton", padding=(13, 6))
    style.configure("Link.TButton", padding=(6, 4))
    for name in ("Small.TButton", "Small.Primary.TButton", "Small.Danger.TButton"):
        style.configure(name, padding=(10, 4), font=fonts["small"], width=-6)
    for name in ("", "Primary.", "Danger."):
        style.configure(f"Record.{name}TButton", padding=(18, 10), font=fonts["record"])


def apply(root: object, fonts: Fonts) -> bool:
    """Choose the theme and configure every style the window uses; True when modern."""
    style = ttk.Style(root)  # type: ignore[arg-type]
    modern = load_theme(style)
    configure_labels(style, fonts)
    if modern:
        configure_modern(style, fonts)
    else:
        configure_inputs(style, fonts)
        configure_buttons(style, fonts)
        configure_tabs(style, fonts)
    configure_misc(style, fonts, modern)
    return modern
