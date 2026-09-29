"""Thumbnails in the Linux tray menu.

pystray builds plain text menu items. On Linux (the GTK and AppIndicator backends)
an item that carries a picture is exported over DBus as `icon-data`, so the top bar
can draw it. `install` wraps the icon's item builder and swaps only the rows that
have a thumbnail for a Gtk.ImageMenuItem; every other row, and every failure, keeps
pystray's own text item. Windows menus cannot hold pictures (pystray's win32
backend has no image items), so there it does nothing and the rows stay text.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

try:
    import logsetup

    log = logsetup.get_logger("tray")
except ImportError:
    log = logging.getLogger(__name__)

THUMB_HEIGHT = 32  # Pixels; the shell may draw it smaller.
THUMB_WIDTH = 64
SUPPORTED_BACKENDS = ("pystray._appindicator", "pystray._gtk")


def supported(icon: Any) -> bool:
    return type(icon).__module__ in SUPPORTED_BACKENDS and hasattr(icon, "_create_menu_item")


def load_pixbuf(path: Path, gdk_pixbuf: Any) -> Any:
    """The thumbnail scaled to fit the row, keeping its proportions."""
    return gdk_pixbuf.Pixbuf.new_from_file_at_scale(str(path), THUMB_WIDTH, THUMB_HEIGHT, True)


def image_item(icon: Any, descriptor: Any, path: Path, gtk: Any, gdk_pixbuf: Any) -> Any:
    """A Gtk.ImageMenuItem for a plain, clickable row; the same look as the text item."""
    item = gtk.ImageMenuItem.new_with_label(descriptor.text)
    item.set_image(gtk.Image.new_from_pixbuf(load_pixbuf(path, gdk_pixbuf)))
    item.set_always_show_image(True)
    item.connect("activate", icon._handler(descriptor))
    item.set_sensitive(descriptor.enabled)
    return item


def install(icon: Any, thumb_of: Callable[[Any], Path | None]) -> bool:
    """Show `thumb_of(descriptor)` beside a row, when this backend can; True if wired up.

    Never raises: with no GTK, another backend or any error, the menu stays text-only.
    """
    if not supported(icon):
        return False
    try:
        import gi  # type: ignore[import-untyped, import-not-found, unused-ignore]

        gi.require_version("Gtk", "3.0")
        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import (  # type: ignore[import-untyped, import-not-found, unused-ignore]
            GdkPixbuf,
            Gtk,
        )
    except (ImportError, ValueError) as exc:
        log.info("tray thumbnails unavailable (%s)", exc)
        return False
    original = icon._create_menu_item

    def create(descriptor: Any) -> Any:
        widget = original(descriptor)
        try:
            if descriptor.submenu or descriptor.checked is not None or descriptor.default:
                return widget
            path = thumb_of(descriptor)
            if path is None:
                return widget
            return image_item(icon, descriptor, path, Gtk, GdkPixbuf)
        except Exception as exc:  # A bad picture must not break the menu.
            log.warning("tray thumbnail not shown (%s)", exc)
            return widget

    icon._create_menu_item = create
    return True
