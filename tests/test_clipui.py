"""The Clipboard tab and opt-in page, plus the helpers behind copy-back."""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import clipstore
import clipui
import desktop
import dictation as d
import hotkeys
from support import make_png
from test_app import ServiceCase


class RelativeTimeTests(unittest.TestCase):
    def test_times_read_naturally(self):
        now = 1_000_000.0
        for age, expected in (
            (5, "just now"),
            (60, "1 min ago"),
            (5 * 60, "5 min ago"),
            (3600, "1 h ago"),
            (5 * 3600, "5 h ago"),
            (30 * 3600, "yesterday"),
        ):
            with self.subTest(age=age):
                self.assertEqual(clipui.relative_time(now - age, now), expected)
        self.assertRegex(clipui.relative_time(now - 10 * 86400, now), r"^\w{3} \d{1,2}")
        self.assertEqual(clipui.relative_time(now + 500, now), "just now")  # Clock skew.

    def test_previews_are_short_single_lines(self):
        self.assertEqual(clipui.preview("  hello\n\n  world  "), "hello world")
        self.assertEqual(len(clipui.preview("x" * 500)), clipui.PREVIEW_CHARS + 1)
        self.assertTrue(clipui.preview("x" * 500).endswith("…"))


class CopyImageTests(unittest.TestCase):
    def run_for(self, platform):
        calls = []
        path = Path("/data/images/" + "a" * 64 + ".png")
        with (
            patch.object(desktop, "platform_name", return_value=platform),
            patch.object(Path, "read_bytes", return_value=b"PNGDATA"),
        ):
            desktop.copy_image(
                {"wl_copy": "wl-copy", "powershell": "powershell.exe"},
                path,
                run=lambda *a, **k: calls.append((a, k)),
            )
        return calls[0]

    def test_linux_pipes_the_png_to_wl_copy(self):
        (command,), options = self.run_for("linux")
        self.assertEqual(command[-2:], ["--type", "image/png"])
        self.assertEqual(options["input"], b"PNGDATA")

    def test_macos_uses_osascript_with_the_png_class(self):
        (command,), _ = self.run_for("macos")
        self.assertEqual(command[:2], ["/usr/bin/osascript", "-e"])
        self.assertIn("«class PNGf»", command[2])
        self.assertIn("a" * 64 + ".png", command[2])

    def test_windows_uses_a_single_threaded_powershell_and_escapes_quotes(self):
        (command,), _ = self.run_for("windows")
        self.assertIn("-STA", command)
        self.assertIn("SetImage", command[-1])
        path = Path("C:/Users/O'Brien/images/x.png")
        script = desktop.windows_image_script(path)
        self.assertIn("O''Brien", script)

    def test_a_failing_copy_is_an_error_with_a_message(self):
        def fail(*a, **k):
            raise subprocess.CalledProcessError(1, "x")

        with (
            patch.object(desktop, "platform_name", return_value="linux"),
            patch.object(Path, "read_bytes", return_value=b"x"),
        ):
            with self.assertRaises(OSError):
                desktop.copy_image({"wl_copy": "wl-copy"}, Path("/x.png"), run=fail)


class PageCase(ServiceCase):
    @classmethod
    def setUpClass(cls):
        try:
            import tkinter as tk

            probe = tk.Tk()
            probe.withdraw()
            probe.destroy()
        except Exception as exc:  # noqa: BLE001
            raise unittest.SkipTest(f"Tk/display unavailable: {exc}")

    def setUp(self):
        super().setUp()
        import app

        self.gui = app
        hotkeys.Preferences(self.paths).save(features=hotkeys.Features(True, True))
        self.store = clipstore.Store(self.paths.clipboard)
        self.addCleanup(self.store.close)
        self.root = app.tk.Tk()
        self.root.withdraw()
        with patch.object(self.service, "completed", return_value=True):
            self.window = app.App(self.root, self.service, "clipboard")
        self.addCleanup(self.close_window)
        self.page = self.window.clipboard_page

    def close_window(self):
        if self.window.page != "closed":
            self.window.destroy()
        self.window = self.root = self.page = None
        import gc

        gc.collect()

    def texts(self):
        found, stack = [], [self.window.frame]
        while stack:
            widget = stack.pop()
            stack.extend(widget.winfo_children())
            if widget.winfo_class() in ("TLabel", "Label"):
                found.append(str(widget.cget("text")))
        return found

    def buttons(self, label):
        found, stack = [], [self.window.frame]
        while stack:
            widget = stack.pop()
            stack.extend(widget.winfo_children())
            if widget.winfo_class() == "TButton" and widget.cget("text") == label:
                found.append(widget)
        return found


class PageTests(PageCase):
    def test_the_page_is_reached_with_the_clipboard_flag(self):
        self.assertEqual(self.window.page, "clipboard")

    def test_an_empty_history_explains_what_will_appear(self):
        self.assertTrue(any("Copy something" in t for t in self.texts()))

    def test_rows_show_text_links_and_images(self):
        self.store.add_text("meeting notes", now=1.0)
        self.store.add_text("https://example.com", now=2.0)
        self.store.add_image(make_png(30, 20, noise=True), now=3.0)
        self.page.reload()
        texts = self.texts()
        self.assertIn("meeting notes", texts)
        self.assertIn("https://example.com", texts)
        self.assertTrue(any(t.startswith("Image 30×20") for t in texts))
        self.assertEqual(len(self.page.rows), 3)

    def test_search_and_filters_narrow_the_list(self):
        keep = self.store.add_text("invoice 4711", now=1.0)
        self.store.add_text("holiday plans", now=2.0)
        self.store.add_image(make_png(), now=3.0)
        self.store.set_favorite(keep.id, True)
        self.page.set_query("INVOICE")
        self.assertEqual([r.item.text for r in self.page.rows], ["invoice 4711"])
        self.page.set_query("")
        self.page.set_filter("favorites")
        self.assertEqual(len(self.page.rows), 1)
        self.page.set_filter("images")
        self.assertEqual([r.item.kind for r in self.page.rows], ["image"])
        self.page.set_filter("text")
        self.assertEqual(len(self.page.rows), 2)
        self.page.set_query("nothing matches this")
        self.assertTrue(any("No matches" in t for t in self.texts()))

    def test_typing_in_the_search_box_waits_before_searching(self):
        self.store.add_text("alpha", now=1.0)
        self.store.add_text("beta", now=2.0)
        self.page.reload()
        self.page.query.set("alp")
        self.assertEqual(len(self.page.rows), 2)  # Not yet: the pause has not elapsed.
        self.assertIsNotNone(self.page.pending_search)
        self.page.run_pending_search()
        self.assertEqual([r.item.text for r in self.page.rows], ["alpha"])

    def test_favorite_toggles_in_the_store_and_the_row(self):
        item = self.store.add_text("star me", now=1.0)
        self.page.reload()
        self.page.toggle_favorite(item.id)
        self.assertTrue(self.store.get(item.id).favorite)
        self.assertEqual(self.page.rows[0].item.favorite, True)
        self.page.toggle_favorite(item.id)
        self.assertFalse(self.store.get(item.id).favorite)

    def test_copy_uses_the_clipboard_and_says_so(self):
        item = self.store.add_text("copy me", now=1.0)
        self.page.reload()
        with patch.object(self.service, "copy_item") as copy:
            self.page.copy(item.id)
        copy.assert_called_once()
        self.assertEqual(self.window.status.get(), "Copied to the clipboard.")

    def test_a_failed_copy_reports_instead_of_crashing(self):
        item = self.store.add_text("copy me", now=1.0)
        self.page.reload()
        with patch.object(self.service, "copy_item", side_effect=d.DictationError("no clipboard")):
            self.page.copy(item.id)
        self.assertEqual(self.window.status.get(), "no clipboard")

    def test_delete_removes_the_item(self):
        item = self.store.add_text("bye", now=1.0)
        self.store.add_text("stay", now=2.0)
        self.page.reload()
        self.page.delete(item.id)
        self.assertEqual([i.text for i in self.store.list()], ["stay"])
        self.assertEqual(len(self.page.rows), 1)

    def test_clear_asks_first_and_keeps_favorites(self):
        keep = self.store.add_text("keep", now=1.0)
        self.store.add_text("drop", now=2.0)
        self.store.set_favorite(keep.id, True)
        self.page.reload()
        with patch.object(self.page, "ask_clear", return_value=None) as ask:
            self.page.clear()
        ask.assert_called_once_with(False)
        self.assertEqual(self.store.count(), 2)
        with patch.object(self.page, "ask_clear", return_value=(False, True)):
            self.page.clear()
        self.assertEqual([i.text for i in self.store.list()], ["keep"])

    def test_long_histories_load_in_pages(self):
        for number in range(clipui.PAGE_SIZE + 10):
            self.store.add_text(f"item {number}", now=float(number))
        self.page.reload()
        self.assertEqual(len(self.page.rows), clipui.PAGE_SIZE)
        self.buttons("Load more")[0].invoke()
        self.assertEqual(len(self.page.rows), clipui.PAGE_SIZE + 10)
        self.assertEqual(self.buttons("Load more"), [])

    def test_an_unchanged_history_is_not_redrawn(self):
        self.store.add_text("steady", now=1.0)
        self.page.reload()
        before = [row.frame for row in self.page.rows]
        self.page.refresh()
        self.assertEqual([row.frame for row in self.page.rows], before)
        self.store.add_text("new", now=2.0)
        self.page.refresh()
        self.assertEqual(len(self.page.rows), 2)

    def test_a_paused_capture_is_announced_with_a_way_back(self):
        hotkeys.Preferences(self.paths).save(clipboard=hotkeys.ClipboardSettings(paused_until=-1))
        self.page.reload()
        self.assertTrue(any("paused" in t.lower() for t in self.texts()))
        self.buttons("Resume")[0].invoke()
        self.assertFalse(hotkeys.Preferences(self.paths).clipboard().paused(1e12))


class TabTests(PageCase):
    def test_only_the_chosen_features_have_tabs(self):
        self.assertEqual(self.window.tab_names(), ["clipboard", "dictation", "settings"])
        hotkeys.Preferences(self.paths).save(features=hotkeys.Features(False, True))
        self.assertEqual(self.window.tab_names(), ["clipboard", "settings"])
        hotkeys.Preferences(self.paths).save(features=hotkeys.Features(True, False))
        self.assertEqual(self.window.tab_names(), ["dictation", "settings"])

    def test_choosing_a_tab_switches_the_page(self):
        with patch.object(self.service, "completed", return_value=True):
            self.window.tab("dictation")
            self.assertEqual(self.window.page, "home")
            self.window.tab("clipboard")
            self.assertEqual(self.window.page, "clipboard")


class OptInTests(PageCase):
    def test_turning_it_on_enables_the_feature(self):
        hotkeys.Preferences(self.paths).save(features=hotkeys.Features(True, False))
        self.window.clipboard_optin(lambda enabled: None)
        self.assertTrue(any("password manager" in t for t in self.texts()))
        self.buttons("Turn on")[0].invoke()
        self.assertTrue(hotkeys.Preferences(self.paths).features().clipboard)

    def test_not_now_leaves_it_off_and_continues(self):
        hotkeys.Preferences(self.paths).save(features=hotkeys.Features(True, False))
        went = Mock()
        self.window.clipboard_optin(went)
        self.buttons("Not now")[0].invoke()
        self.assertFalse(hotkeys.Preferences(self.paths).features().clipboard)
        went.assert_called_once()


if __name__ == "__main__":
    unittest.main()
