"""The Clipboard tab and opt-in page, plus the helpers behind copy-back."""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
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
        self.window.quick = False  # Browsing, not picking: copying keeps the window open.

    def close_window(self):
        if self.window.page != "closed":
            self.window.destroy()
        self.window = self.root = self.page = None
        import gc

        gc.collect()

    def texts(self):
        found, stack = [], [self.window.root]
        while stack:
            widget = stack.pop()
            stack.extend(widget.winfo_children())
            if widget.winfo_class() in ("TLabel", "Label"):
                found.append(str(widget.cget("text")))
        return found

    def row_text(self, row):
        """The label showing a row's text (not its time, star or actions)."""
        stack = [row.frame]
        while stack:
            widget = stack.pop(0)
            stack.extend(widget.winfo_children())
            if widget.winfo_class() == "Label" and widget.cget("wraplength"):
                return widget
        raise AssertionError("no text label")

    def buttons(self, label):
        found, stack = [], [self.window.root]
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
        self.assertIn("Nothing copied yet", self.texts())

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
        self.assertTrue(any("Nothing matches" in t for t in self.texts()))

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
        self.assertEqual(self.window.status.get(), "Copied. Paste it anywhere.")

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

    def test_undo_restores_favorite_and_label(self):
        item = self.store.add_text("recover me")
        self.store.set_favorite(item.id, True)
        self.store.set_label(item.id, "Important")
        self.page.reload()
        with patch.object(clipui.messagebox, "askyesno", return_value=True):
            self.page.delete(item.id)
        self.page.undo_delete()
        restored = self.store.list()[0]
        self.assertEqual(
            (restored.text, restored.favorite, restored.label), ("recover me", True, "Important")
        )
        self.assertIsNone(self.page.deleted)

    def test_view_shows_full_text_without_copying(self):
        text = "long text\n" * 200
        item = self.store.add_text(text)
        with (
            patch.object(clipui, "TextPreview") as preview,
            patch.object(self.service, "copy_item") as copy,
        ):
            self.page.view(item.id)
        self.assertEqual(preview.call_args.args[2], text)
        copy.assert_not_called()

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
        # The first rows appear at once; the rest follow a batch per frame.
        self.assertEqual(len(self.page.rows), clipui.ROW_BATCH)
        self.page._flush()
        self.assertEqual(len(self.page.rows), clipui.PAGE_SIZE)
        texts = [row.item.text for row in self.page.rows]
        self.assertEqual(texts, [f"item {n}" for n in range(59, 59 - clipui.PAGE_SIZE, -1)])
        packed = [
            str(w) for w in self.page.card.pack_slaves() if w in {r.frame for r in self.page.rows}
        ]
        self.assertEqual(packed, [str(row.frame) for row in self.page.rows])
        # A new copy on top keeps every drawn row, drawn again in place.
        frames = {row.item.id: row.frame for row in self.page.rows}
        self.store.add_text("newest", now=100.0)
        self.page.reload()
        self.page._flush()
        kept = [row for row in self.page.rows if row.item.id in frames]
        self.assertTrue(all(frames[row.item.id] is row.frame for row in kept))
        self.assertEqual(self.page.rows[0].item.text, "newest")
        self.buttons("Load more")[0].invoke()
        self.assertEqual(len(self.page.rows), clipui.PAGE_SIZE + 11)
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

    def test_starring_and_deleting_change_only_their_own_row(self):
        first = self.store.add_text("one", now=1.0)
        second = self.store.add_text("two", now=2.0)
        self.page.reload()
        before = {row.item.id: row.frame for row in self.page.rows}
        self.page.toggle_favorite(first.id)
        self.assertEqual({row.item.id: row.frame for row in self.page.rows}, before)
        self.assertEqual(self.page._row(first.id).star.cget("text"), "★")
        self.page.refresh()  # Our own change is not mistaken for a new copy.
        self.assertEqual({row.item.id: row.frame for row in self.page.rows}, before)
        self.page.delete(second.id)
        self.assertEqual([row.frame for row in self.page.rows], [before[first.id]])

    def test_a_recopied_item_moves_to_the_top_without_redrawing_the_list(self):
        older = self.store.add_text("older", now=1.0)
        self.store.add_text("newer", now=2.0)
        self.page.reload()
        before = {row.item.id: row.frame for row in self.page.rows}
        self.store.add_text("older", now=3.0)  # Clicking a row copies it back: it moves up.
        self.page.refresh()
        self.assertEqual([row.item.text for row in self.page.rows], ["older", "newer"])
        self.assertEqual({row.item.id: row.frame for row in self.page.rows}, before)
        packed = [w for w in self.page.card.pack_slaves() if w in before.values()]
        self.assertEqual(packed[0], before[older.id])

    def test_favorites_can_be_given_a_label_changed_and_have_it_removed(self):
        item = self.store.add_text("hunter2", now=1.0)
        self.page.reload()
        row = self.page.rows[0]
        self.assertFalse(row.rename.winfo_manager())  # Only favorites take a label.
        self.page.toggle_favorite(item.id)
        self.assertEqual(row.rename.cget("text"), "Add label")
        with patch.object(self.page, "ask_label", return_value="Wifi password") as ask:
            self.page.edit_label(item.id)
        ask.assert_called_once_with("")
        self.assertEqual(self.store.get(item.id).label, "Wifi password")
        self.assertEqual(row.title.cget("text"), "Wifi password")
        self.assertTrue(row.title.winfo_manager())
        self.assertEqual(row.rename.cget("text"), "Edit label")
        self.assertEqual(self.page.rows[0].frame, row.frame)
        with patch.object(self.page, "ask_label", return_value=None):
            self.page.edit_label(item.id)  # Cancelled: nothing changes.
        self.assertEqual(self.store.get(item.id).label, "Wifi password")
        self.page.set_query("wifi")
        self.assertEqual([r.item.id for r in self.page.rows], [item.id])
        with patch.object(self.page, "ask_label", return_value=""):
            self.page.edit_label(item.id)
        self.assertEqual(self.store.get(item.id).label, "")
        self.assertFalse(self.page.rows[0].title.winfo_manager())

    def test_a_label_set_elsewhere_shows_and_unstarring_drops_it(self):
        item = self.store.add_text("address", now=1.0)
        self.store.set_favorite(item.id, True)
        self.store.set_label(item.id, "Home")
        self.page.reload()
        self.assertIn("Home", self.texts())
        self.page.toggle_favorite(item.id)
        row = self.page.rows[0]
        self.assertFalse(row.title.winfo_manager())
        self.assertFalse(row.rename.winfo_manager())
        self.assertEqual(self.store.get(item.id).label, "")

    def test_the_label_dialog_saves_removes_and_cancels(self):
        for action, expected in (("save", "New"), ("remove", ""), ("cancel", None)):
            with self.subTest(action=action):
                dialog = clipui.LabelDialog(self.root, "Old")
                dialog.text.set("  New ")
                getattr(dialog, action)()
                self.assertEqual(dialog.result, expected)

    def test_arrow_keys_choose_the_row_enter_copies(self):
        for number in range(4):
            self.store.add_text(f"item {number}", now=float(number))
        self.page.reload()
        self.assertEqual(self.page.selected, 0)
        self.page.move(1)
        self.page.move(1)
        self.page.move(-1)
        self.assertEqual(self.page.selected, 1)
        selected = self.window.colors["selected"]
        self.assertEqual(self.page.rows[1].frame.cget("background"), selected)
        self.assertNotEqual(self.page.rows[0].frame.cget("background"), selected)
        for _ in range(10):
            self.page.move(1)
        self.assertEqual(self.page.selected, 3)  # Stops at the end.
        with patch.object(self.page, "copy") as copy:
            self.page.copy_selected()
        copy.assert_called_once_with(self.page.rows[3].item.id)
        self.page.set_query("item 2")
        self.assertEqual(self.page.selected, 0)  # A new search starts at the top.
        self.store.add_text("newest", now=10.0)
        self.page.set_query("")
        self.page.move(2)
        self.store.add_text("even newer", now=11.0)
        self.page.refresh()  # A new copy arriving keeps a valid selection.
        self.assertLess(self.page.selected, len(self.page.rows))

    def test_copying_keeps_the_window_open_and_escape_closes_a_picker(self):
        item = self.store.add_text("paste me", now=1.0)
        self.page.reload()
        self.window.quick = True
        with (
            patch.object(self.service, "copy_item"),
            patch.object(self.window, "close") as close,
        ):
            self.page.copy(item.id)
            self.root.update()
        close.assert_not_called()  # Copy several things in a row if you like.
        self.assertEqual(self.window.status.get(), "Copied. Paste it anywhere.")
        with patch.object(self.window, "close") as close:
            self.page.query.set("paste")
            self.page.escape()  # First Escape clears the search.
            close.assert_not_called()
            self.assertEqual(self.page.query.get(), "")
            self.page.escape()
        close.assert_called_once()
        self.window.quick = False
        with patch.object(self.window, "close") as close:
            self.page.escape()
        close.assert_not_called()  # A window opened to browse stays open.
        self.window.tab("settings")
        self.assertFalse(self.window.quick)

    def test_a_broken_capture_is_announced(self):
        broken = {"state": "error", "message": "wl-paste is missing."}
        with patch.object(clipui.clipservice, "read_status", return_value=broken):
            self.page.refresh()
        self.assertTrue(any("isn’t working: wl-paste is missing." in t for t in self.texts()))

    def test_deleting_a_favorite_asks_first(self):
        item = self.store.add_text("precious", now=1.0)
        self.store.set_favorite(item.id, True)
        self.page.reload()
        with patch.object(clipui.messagebox, "askyesno", return_value=False) as ask:
            self.page.delete(item.id)
        ask.assert_called_once()
        self.assertIsNotNone(self.store.get(item.id))
        with patch.object(clipui.messagebox, "askyesno", return_value=True):
            self.page.delete(item.id)
        self.assertIsNone(self.store.get(item.id))

    def test_an_empty_search_offers_a_way_back(self):
        self.store.add_text("alpha", now=1.0)
        self.page.set_query("zzz")
        self.assertIn("Nothing matches “zzz”.", self.texts())
        self.buttons("Show everything")[0].invoke()
        self.assertEqual(len(self.page.rows), 1)
        self.assertEqual(self.page.query.get(), "")

    def test_clearing_defaults_to_this_device_only(self):
        dialog = clipui.ClearDialog(self.root, linked=True)
        self.assertEqual(dialog.scope.get(), "device")
        dialog.cancel()

    def test_unstarring_under_the_favorites_filter_drops_the_row(self):
        item = self.store.add_text("fav", now=1.0)
        self.store.set_favorite(item.id, True)
        self.page.set_filter("favorites")
        self.page.toggle_favorite(item.id)
        self.assertEqual(self.page.rows, [])
        self.assertTrue(any("Nothing matches" in t for t in self.texts()))

    def test_load_more_keeps_the_rows_already_drawn(self):
        for number in range(clipui.PAGE_SIZE + 3):
            self.store.add_text(f"item {number}", now=float(number))
        self.page.reload()
        before = [row.frame for row in self.page.rows]
        self.page.load_more()
        self.assertEqual([row.frame for row in self.page.rows][: len(before)], before)
        self.assertEqual(len(self.page.rows), clipui.PAGE_SIZE + 3)

    def test_images_are_decoded_once(self):
        self.store.add_image(make_png(30, 20, noise=True), now=1.0)
        with patch.object(clipui.tk, "PhotoImage", wraps=clipui.tk.PhotoImage) as decode:
            self.page.reload()
            self.page.reload()
        self.assertEqual(decode.call_count, 1)

    def test_enter_copies_the_top_match_and_clicking_a_row_copies_it(self):
        self.store.add_text("older", now=1.0)
        newest = self.store.add_text("newer", now=2.0)
        self.page.reload()
        with patch.object(self.page, "copy") as copy:
            self.page.query.set("old")
            self.page.copy_selected()  # Runs the waiting search first.
            self.assertEqual(copy.call_args.args[0], self.page.rows[0].item.id)
            self.page.set_query("")
            label = self.row_text(self.page.rows[0])
            self.root.update_idletasks()  # New rows are mapped when Tk is idle.
            label.event_generate("<Button-1>")
        self.assertEqual(copy.call_args.args[0], newest.id)

    def test_clear_history_sits_with_the_filters_not_below_the_list(self):
        button = self.page.clear_button
        self.assertEqual(button.winfo_manager(), "")  # Hidden: nothing to clear yet.
        item = self.store.add_text("one", now=1.0)
        self.page.reload()
        self.assertEqual(button.winfo_manager(), "pack")
        self.assertIs(button.master, self.page._chips["all"].master)
        self.assertEqual(self.buttons("Clear history"), [button])
        self.page.delete(item.id)
        self.assertEqual(button.winfo_manager(), "")

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


class WindowTests(PageCase):
    def test_the_shortcut_opens_the_clipboard_tab_ready_to_search(self):
        with patch.object(self.service, "completed", return_value=True):
            self.window.settings()
            self.window.open_page("clipboard")
        self.assertEqual(self.window.page, "clipboard")
        page = self.window.clipboard_page
        page.query.set("old search")
        with patch.object(page, "focus_search") as focus:
            self.window.open_page("clipboard")  # Already there: just ready to type.
        focus.assert_called_once()

    def test_control_f_searches_from_any_page(self):
        with patch.object(self.service, "completed", return_value=True):
            self.window.tab("dictation")
            self.window.find()
        self.assertEqual(self.window.page, "clipboard")

    def test_labels_rewrap_when_the_window_is_resized(self):
        self.store.add_text("long " * 80, now=1.0)
        self.page.reload()
        label = self.row_text(self.page.rows[0])
        before = int(str(label.cget("wraplength")))
        self.window.rewrap(self.window.wraplength + 2 * self.gui.PAD + 30 + 200)
        self.assertEqual(int(str(label.cget("wraplength"))), before + 200)
        self.window.rewrap(100)  # Never narrower than readable.
        self.assertGreaterEqual(int(str(label.cget("wraplength"))), 120)

    def test_the_window_size_is_remembered(self):
        with (
            patch.object(self.root, "winfo_width", return_value=900),
            patch.object(self.root, "winfo_height", return_value=640),
        ):
            self.window.save_size()
        self.assertEqual(self.window.saved_size(), (900, 640))

    def test_the_history_shortcut_is_chosen_in_settings(self):
        with (
            patch.object(self.service, "completed", return_value=True),
            patch.object(self.service, "microphones", return_value=["Mic"]),
        ):
            self.window.settings()
        self.assertTrue(any("Open clipboard history" in t for t in self.texts()))
        box = next(
            w
            for w in self.all_widgets()
            if w.winfo_class() == "TCombobox" and "Off" in w.cget("values")
        )
        box.set("Off")
        box.event_generate("<<ComboboxSelected>>")
        self.assertIsNone(hotkeys.Preferences(self.paths).history_shortcut())
        self.assertIn("off", self.window.status.get())

    def test_changing_the_dictation_shortcut_returns_to_settings(self):
        with (
            patch.object(self.service, "completed", return_value=True),
            patch.object(self.service, "microphones", return_value=["Mic"]),
        ):
            self.window.settings()
            self.buttons("Change…")[0].invoke()
            self.assertEqual(self.window.page, "shortcut")
            self.assertTrue((self.paths.runtime / "shortcut-capture").exists())
            self.window.captured = hotkeys.Shortcut(("ctrl", "alt"), "K")
            self.window.save_shortcut()
        self.assertEqual(self.window.page, "settings")
        self.assertFalse((self.paths.runtime / "shortcut-capture").exists())
        self.assertEqual(hotkeys.Preferences(self.paths).shortcut().key, "K")
        self.assertIn("Dictation shortcut", self.window.status.get())

    def test_search_stays_in_view_and_the_empty_bottom_bar_is_hidden(self):
        self.assertIs(self.page.entry.master.master, self.window.toolbar)
        self.assertEqual(self.window.toolbar.winfo_manager(), "pack")
        self.root.update_idletasks()
        self.assertEqual(self.window.bottom.winfo_manager(), "")  # Nothing to show.
        self.window.status.set("Copied to the clipboard.")
        self.assertEqual(self.window.bottom.winfo_manager(), "pack")
        with (
            patch.object(self.service, "completed", return_value=True),
            patch.object(self.service, "microphones", return_value=["Mic"]),
        ):
            self.window.settings()
        self.assertEqual(self.window.toolbar.winfo_manager(), "")  # Only the list needs it.

    def test_page_keys_scroll_but_not_while_typing(self):
        seen = []
        self.window.scroll(0.0, 0.5)
        self.window.canvas.yview_scroll = lambda amount, what: seen.append((amount, what))
        event = SimpleNamespace(widget=self.window.frame)
        self.assertEqual(self.window.scroll_key(1, "pages", event), "break")
        self.assertEqual(seen, [(1, "pages")])
        typing = SimpleNamespace(widget=self.page.entry)
        self.assertIsNone(self.window.scroll_key(1, "pages", typing))
        self.assertEqual(len(seen), 1)

    def test_a_narrow_window_keeps_the_tabs_and_drops_the_name(self):
        self.window.rewrap(460)
        self.assertEqual(self.window.brand.cget("text"), "")
        self.assertEqual(self.page.count_label.winfo_manager(), "")
        self.window.rewrap(760)
        self.assertEqual(self.window.brand.cget("text"), hotkeys.APP_NAME)

    def all_widgets(self):
        found, stack = [], [self.window.root]
        while stack:
            widget = stack.pop()
            stack.extend(widget.winfo_children())
            found.append(widget)
        return found


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
