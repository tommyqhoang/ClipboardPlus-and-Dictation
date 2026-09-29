"""Tray menu thumbnails, against stand-ins for pystray and GTK (no desktop session)."""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import traymenu


class Descriptor:
    def __init__(self, text="Image 8×4", *, submenu=None, checked=None, default=False):
        self.text, self.submenu, self.checked, self.default = text, submenu, checked, default
        self.enabled = True


def make_icon(module: str) -> Any:
    """An icon whose class claims to live in `module`, like pystray's backends do."""
    icon = type("Icon", (), {"__module__": module})()
    icon._create_menu_item = MagicMock(name="text item")
    icon._handler = MagicMock(return_value="handler")
    return icon


def fake_gi() -> tuple[dict[str, object], MagicMock, MagicMock]:
    gtk, pixbuf = MagicMock(name="Gtk"), MagicMock(name="GdkPixbuf")
    repository = types.SimpleNamespace(Gtk=gtk, GdkPixbuf=pixbuf)
    gi = types.SimpleNamespace(require_version=MagicMock(), repository=repository)
    return {"gi": gi, "gi.repository": repository}, gtk, pixbuf


class InstallTests(unittest.TestCase):
    def test_other_backends_are_left_alone(self):
        for module in ("pystray._win32", "pystray._xorg", "pystray._darwin"):
            icon = make_icon(module)
            original = icon._create_menu_item
            self.assertFalse(traymenu.install(icon, lambda _: Path("t.png")), module)
            self.assertIs(icon._create_menu_item, original)

    def test_missing_gtk_keeps_text_rows(self):
        icon = make_icon("pystray._appindicator")
        original = icon._create_menu_item
        with patch.dict(sys.modules, {"gi": None}):
            self.assertFalse(traymenu.install(icon, lambda _: Path("t.png")))
        self.assertIs(icon._create_menu_item, original)

    def test_wrong_gtk_version_keeps_text_rows(self):
        modules, _, _ = fake_gi()
        modules["gi"].require_version.side_effect = ValueError("no such namespace")
        icon = make_icon("pystray._appindicator")
        with patch.dict(sys.modules, modules):
            self.assertFalse(traymenu.install(icon, lambda _: Path("t.png")))

    def test_rows_with_a_thumbnail_get_an_image_item(self):
        modules, gtk, pixbuf = fake_gi()
        icon = make_icon("pystray._appindicator")
        text_item = icon._create_menu_item.return_value
        thumbs = {}
        row, plain = Descriptor(), Descriptor("hello")
        thumbs[row] = Path("t.png")
        with patch.dict(sys.modules, modules):
            self.assertTrue(traymenu.install(icon, thumbs.get))
            built = icon._create_menu_item(row)
            self.assertIs(built, gtk.ImageMenuItem.new_with_label.return_value)
            self.assertIs(icon._create_menu_item(plain), text_item)  # No thumbnail.
        gtk.ImageMenuItem.new_with_label.assert_called_once_with("Image 8×4")
        pixbuf.Pixbuf.new_from_file_at_scale.assert_called_once_with(
            "t.png", traymenu.THUMB_WIDTH, traymenu.THUMB_HEIGHT, True
        )
        built.set_always_show_image.assert_called_once_with(True)
        built.connect.assert_called_once_with("activate", "handler")  # Still click-to-copy.
        built.set_sensitive.assert_called_once_with(True)

    def test_only_plain_rows_are_replaced(self):
        modules, gtk, _ = fake_gi()
        icon = make_icon("pystray._gtk")
        with patch.dict(sys.modules, modules):
            traymenu.install(icon, lambda _: Path("t.png"))
            for descriptor in (
                Descriptor(submenu=object()),
                Descriptor(checked=True),
                Descriptor(default=True),
            ):
                icon._create_menu_item(descriptor)
        gtk.ImageMenuItem.new_with_label.assert_not_called()

    def test_any_failure_falls_back_to_the_text_item(self):
        modules, gtk, pixbuf = fake_gi()
        icon = make_icon("pystray._appindicator")
        text_item = icon._create_menu_item.return_value
        with patch.dict(sys.modules, modules):
            traymenu.install(icon, lambda _: Path("missing.png"))
            pixbuf.Pixbuf.new_from_file_at_scale.side_effect = RuntimeError("bad picture")
            self.assertIs(icon._create_menu_item(Descriptor()), text_item)
            traymenu.install(icon, MagicMock(side_effect=OSError("gone")))
            self.assertIs(icon._create_menu_item(Descriptor()), text_item)


if __name__ == "__main__":
    unittest.main()
