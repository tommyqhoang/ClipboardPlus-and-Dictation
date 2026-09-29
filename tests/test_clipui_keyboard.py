"""Keyboard navigation of the clipboard list, driven with real key events on a real Tk.

Runs under a display (tests/with-xvfb.sh) and is skipped without one; nothing here mocks
Tk itself, only the clipboard copy.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_clipui import PageCase


class KeyboardTests(PageCase):
    def setUp(self):
        super().setUp()
        for number in range(5):
            self.store.add_text(f"item {number}", now=float(number))
        self.page.reload()
        self.root.deiconify()
        self.root.update()
        self.entry = self.page.entry
        self.entry.focus_force()
        self.root.update()

    def press(self, sequence):
        self.entry.event_generate(sequence)
        self.root.update()

    def test_the_search_box_has_the_focus_when_the_page_opens(self):
        self.assertIs(self.root.focus_get(), self.entry)

    def test_the_arrow_keys_walk_the_list_and_stop_at_both_ends(self):
        self.assertEqual(self.page.selected, 0)
        for expected in (1, 2, 3, 4, 4):
            self.press("<Down>")
            self.assertEqual(self.page.selected, expected)
        for expected in (3, 2, 1, 0, 0):
            self.press("<Up>")
            self.assertEqual(self.page.selected, expected)

    def test_the_selected_row_is_the_one_painted(self):
        self.press("<Down>")
        self.press("<Down>")
        selected = self.window.colors["selected"]
        colors = [str(row.frame.cget("background")) for row in self.page.rows]
        self.assertEqual([i for i, color in enumerate(colors) if color == selected], [2])

    def test_arrows_keep_the_typing_focus(self):
        self.press("<Down>")
        self.assertIs(self.root.focus_get(), self.entry)  # No jump onto a row.

    def test_enter_copies_the_chosen_row_from_the_keyboard(self):
        self.press("<Down>")
        self.press("<Down>")
        with patch.object(self.window.service, "copy_item") as copy:
            self.press("<Return>")
        copied = copy.call_args.args[0]
        self.assertEqual(copied.id, self.page.rows[2].item.id)

    def test_typing_narrows_the_list_and_escape_brings_it_back(self):
        self.entry.insert(0, "item 3")
        self.root.update()
        self.page.run_pending_search()
        self.root.update()
        self.assertEqual(len(self.page.rows), 1)
        self.press("<Escape>")
        self.assertEqual(self.page.query.get(), "")
        self.assertEqual(len(self.page.rows), 5)

    def test_star_label_and_delete_have_keys(self):
        chosen = self.page.rows[1]
        self.press("<Down>")
        self.press("<Control-s>")
        self.assertTrue(self.store.get(chosen.item.id).favorite)
        with patch.object(self.page, "ask_label", return_value="Keep") as ask:
            self.press("<F2>")
        ask.assert_called_once()
        self.assertEqual(self.store.get(chosen.item.id).label, "Keep")
        with patch("clipui.messagebox.askyesno", return_value=True) as asked:
            self.press("<Control-Delete>")
        asked.assert_called_once()  # A favorite asks first.
        self.assertIsNone(self.store.get(chosen.item.id))
        self.assertEqual(len(self.page.rows), 4)
        self.assertLess(self.page.selected, len(self.page.rows))

    def test_an_ordinary_row_deletes_without_asking(self):
        first = self.page.rows[0].item.id
        with patch("clipui.messagebox.askyesno") as asked:
            self.press("<Control-Delete>")
        asked.assert_not_called()
        self.assertIsNone(self.store.get(first))

    def test_the_keys_do_nothing_on_an_empty_list(self):
        self.page.set_query("no such thing")
        self.root.update()
        for sequence in ("<Down>", "<Return>", "<Control-s>", "<F2>", "<Control-Delete>"):
            self.press(sequence)  # Must not raise.
        self.assertEqual(self.page.rows, [])

    def test_the_filter_chips_stay_out_of_the_tab_order(self):
        # Tab from the search box must not stop on every filter chip.
        for chip in self.page._chips.values():
            self.assertEqual(str(chip.cget("takefocus")), "0")


if __name__ == "__main__":
    unittest.main()
