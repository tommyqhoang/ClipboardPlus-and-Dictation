"""The Clipboard+ account card and the clear-history choice."""

from __future__ import annotations

import gc
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import app_service
import clipservice
import clipstore
import hotkeys
from test_app import ServiceCase

KEY = "cp_live_" + "a1b2c3d4" * 6
PASSWORD = "correct horse battery staple"
TOKEN = "eyJ.session.token"


class AccountCase(ServiceCase):
    features = hotkeys.Features(False, True)

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
        self.prefs = hotkeys.Preferences(self.paths)
        self.prefs.save(features=self.features)
        self.root = app.tk.Tk()
        self.root.withdraw()
        self.window = app.App(self.root, self.service)
        self.addCleanup(self.close)
        self.open_settings()

    def open_settings(self):
        with (
            patch.object(self.service, "completed", return_value=True),
            patch.object(self.service, "microphones", return_value=[]),
        ):
            self.window.settings()

    def close(self):
        if self.window.pending:
            self.window.pending.result(timeout=5)
        if self.window.page != "closed":
            self.window.destroy()
        self.window = self.root = None
        gc.collect()

    def widgets(self, kind):
        found, stack = [], [self.window.frame]
        while stack:
            widget = stack.pop()
            stack.extend(widget.winfo_children())
            if widget.winfo_class() == kind:
                found.append(widget)
        return found

    def texts(self):
        return [str(w.cget("text")) for w in self.widgets("TLabel")]

    def joined(self):
        return " | ".join(self.texts())

    def button(self, label):
        for widget in self.widgets("TButton"):
            if widget.cget("text") == label:
                return widget
        raise AssertionError(
            f"No {label!r} button; have {[w.cget('text') for w in self.widgets('TButton')]}"
        )

    def has_button(self, label):
        return any(w.cget("text") == label for w in self.widgets("TButton"))

    def finish(self):
        self.window.pending.result(timeout=5)
        self.root.after_cancel(self.window.timer)
        self.window.poll()

    def press(self, label):
        self.button(label).invoke()

    def link(self, email="me@example.com"):
        with patch.object(app_service.clipboardplus, "verify", return_value="ok"):
            self.service.connect_clipboard_plus(KEY)
        if email:
            app_service.clipboardplus.save_email(self.paths.config.parent, email)

    def report(self, sync, synced=0.0):
        clipservice.write_status(self.paths, "capturing", 1, "", sync=sync, synced=synced)

    def refresh(self):
        self.window.account.refresh()


class StateTests(AccountCase):
    def test_not_connected_offers_an_account_or_a_key(self):
        text = self.joined()
        self.assertIn("Not connected", text)
        for label in ("Create account", "Sign in", "Use an API key instead", "Get Clipboard+"):
            self.assertTrue(self.has_button(label), label)
        self.assertEqual(len(self.widgets("TEntry")), 2)  # Email and password.
        self.assertNotIn("Connected as", text)

    def test_the_password_is_hidden_as_it_is_typed(self):
        hidden = [w for w in self.widgets("TEntry") if str(w.cget("show"))]
        self.assertEqual(len(hidden), 1)

    def test_connected_shows_the_account_and_the_actions(self):
        self.link()
        self.window.account.render()
        self.assertIn("Connected as me@example.com", self.joined())
        for label in ("Sync now", "Open history", "Disconnect"):
            self.assertTrue(self.has_button(label), label)
        self.assertFalse(self.has_button("Sign in"))

    def test_connected_by_key_has_no_address_to_show(self):
        self.link(email="")
        self.window.account.render()
        self.assertIn("Connected", self.joined())
        self.assertNotIn("Connected as", self.joined())

    def test_a_refused_key_asks_to_reconnect(self):
        self.link()
        self.report("auth")
        self.window.account.render()
        text = self.joined()
        self.assertIn("Reconnect needed", text)
        self.assertTrue(self.has_button("Sign in"))
        self.assertFalse(self.has_button("Create account"))  # There is an account already.
        self.assertTrue(self.has_button("Disconnect"))
        self.assertFalse(self.has_button("Sync now"))

    def test_the_sync_report_is_shown_in_plain_words(self):
        self.link()
        for sync, expected in (
            ("syncing", "Syncing"),
            ("offline", "Offline"),
            ("error", "trouble"),
            ("off", "clipboard service"),
        ):
            with self.subTest(sync=sync):
                self.report(sync)
                self.window.account.render()
                self.assertIn(expected, self.joined())
        import time

        self.report("ok", time.time() - 300)
        self.window.account.render()
        self.assertIn("Synced 5 min ago", self.joined())

    def test_the_card_follows_the_service_without_being_rebuilt_needlessly(self):
        self.link()
        self.report("syncing")
        self.window.account.render()
        frames = self.window.account.body.winfo_children()
        self.refresh()  # Nothing changed.
        self.assertEqual(self.window.account.body.winfo_children(), frames)
        self.report("offline")
        self.refresh()
        self.assertIn("Offline", self.joined())

    def test_the_polling_window_keeps_the_card_current(self):
        self.link()
        self.report("syncing")
        self.window.account.render()
        self.report("offline")
        for _ in range(12):
            self.root.after_cancel(self.window.timer)
            self.window.poll()
        self.assertIn("Offline", self.joined())


class SignInTests(AccountCase):
    def fill(self, email="me@example.com", password=PASSWORD):
        self.window.account.email.set(email)
        self.window.account.password.set(password)

    def patched(self, **kwargs):
        cp = app_service.clipboardplus
        defaults = {"register": TOKEN, "login": TOKEN, "create_key": KEY}
        defaults.update(kwargs)
        managers = []
        for name, value in defaults.items():
            if isinstance(value, Exception):
                managers.append(patch.object(cp, name, side_effect=value))
            else:
                managers.append(patch.object(cp, name, return_value=value))
        return managers

    def run_with(self, managers, action):
        mocks = [m.start() for m in managers]
        for m in managers:
            self.addCleanup(m.stop)
        action()
        self.finish()
        return mocks

    def test_creating_an_account_links_it_off_the_ui_thread_and_clears_the_password(self):
        threads = []
        cp = app_service.clipboardplus

        def register(email, password, api=cp.API):
            threads.append(threading.current_thread())
            return TOKEN

        managers = self.patched()
        managers[0] = patch.object(cp, "register", side_effect=register)
        self.fill()
        self.run_with(managers, lambda: self.press("Create account"))
        self.assertNotEqual(threads[0], threading.main_thread())
        self.assertTrue(self.service.clipboard_plus_linked())
        self.assertEqual(self.window.account.password.get(), "")
        self.assertIn("Connected as me@example.com", self.joined())
        self.assertIn("Connected to Clipboard+", self.window.status.get())

    def test_signing_in_uses_login_not_register(self):
        self.fill()
        register, login, _ = self.run_with(self.patched(), lambda: self.press("Sign in"))
        login.assert_called_once()
        register.assert_not_called()
        self.assertTrue(self.service.clipboard_plus_linked())

    def test_a_refusal_is_shown_inline_and_the_password_is_still_cleared(self):
        cp = app_service.clipboardplus
        self.fill()
        self.run_with(
            self.patched(login=cp.AuthError("Wrong email or password.")),
            lambda: self.press("Sign in"),
        )
        self.assertIn("Wrong email or password.", self.joined())
        self.assertEqual(self.window.account.password.get(), "")
        self.assertEqual(self.window.account.email.get(), "me@example.com")  # Kept to retry.
        self.assertFalse(self.service.clipboard_plus_linked())
        self.assertTrue(self.has_button("Sign in"))

    def test_the_error_goes_away_on_the_next_attempt(self):
        cp = app_service.clipboardplus
        self.fill()
        self.run_with(
            self.patched(login=cp.AuthError("Wrong email or password.")),
            lambda: self.press("Sign in"),
        )
        self.fill()
        self.run_with(self.patched(), lambda: self.press("Sign in"))
        self.assertNotIn("Wrong email or password.", self.joined())

    def test_missing_fields_are_reported_without_any_request(self):
        self.fill(email="", password="")
        register, login, _ = self.run_with(self.patched(), lambda: self.press("Sign in"))
        login.assert_not_called()
        self.assertIn("Enter your email and password.", self.joined())

    def test_enter_in_the_password_field_signs_in(self):
        self.fill()
        entry = [w for w in self.widgets("TEntry") if str(w.cget("show"))][0]
        for manager in self.patched():
            manager.start()
            self.addCleanup(manager.stop)
        self.root.deiconify()
        self.root.update()
        entry.focus_force()
        self.root.update()
        entry.event_generate("<Return>")
        self.finish()
        self.assertTrue(self.service.clipboard_plus_linked())

    def test_the_password_never_reaches_the_status_line_or_the_disk(self):
        cp = app_service.clipboardplus
        self.fill()
        self.run_with(
            self.patched(login=cp.AuthError("Wrong email or password.")),
            lambda: self.press("Sign in"),
        )
        self.assertNotIn(PASSWORD, self.window.status.get() + self.joined())


class KeyTests(AccountCase):
    def to_key_mode(self):
        self.press("Use an API key instead")

    def test_a_key_can_be_used_instead_and_the_field_is_cleared(self):
        self.to_key_mode()
        self.assertTrue(self.has_button("Connect"))
        self.assertTrue(self.has_button("Use email and password instead"))
        self.assertFalse(self.has_button("Create account"))
        self.window.account.key.set(KEY)
        with patch.object(app_service.clipboardplus, "verify", return_value="ok"):
            self.press("Connect")
            self.finish()
        self.assertTrue(self.service.clipboard_plus_linked())
        self.assertEqual(self.window.account.key.get(), "")
        self.assertIn("Connected", self.joined())

    def test_a_key_that_cannot_read_and_write_is_refused_with_the_reason(self):
        self.to_key_mode()
        self.window.account.key.set(KEY)
        for result, words in (("write-only", "can’t read"), ("read-only", "can’t save")):
            with self.subTest(result=result):
                with patch.object(app_service.clipboardplus, "verify", return_value=result):
                    self.press("Connect")
                    self.finish()
                self.assertIn(words, self.joined())
                self.assertFalse(self.service.clipboard_plus_linked())
                self.window.account.key.set(KEY)

    def test_switching_back_shows_the_account_form_again(self):
        self.to_key_mode()
        self.press("Use email and password instead")
        self.assertTrue(self.has_button("Create account"))


class ActionTests(AccountCase):
    def test_sync_now_leaves_a_request_for_the_service(self):
        self.link()
        self.window.account.render()
        (self.paths.runtime / "clip-sync-now").unlink(missing_ok=True)
        self.press("Sync now")
        self.assertTrue((self.paths.runtime / "clip-sync-now").exists())
        self.assertIn("Syncing", self.window.status.get())

    def test_open_history_opens_the_dashboard(self):
        self.link()
        self.window.account.render()
        with patch("webbrowser.open") as browser:
            self.press("Open history")
        browser.assert_called_once()
        self.assertIn("dashboard", browser.call_args.args[0])

    def store(self):
        store = clipstore.Store(self.paths.clipboard)
        self.addCleanup(store.close)
        return store

    def test_disconnecting_asks_whether_to_keep_the_history(self):
        self.link()
        store = self.store()
        item = store.add_text("mine", now=1.0)
        store.link(item.id, "cloud-1", False)
        self.window.account.render()
        with patch.object(self.gui.messagebox, "askyesnocancel", return_value=True) as ask:
            self.press("Disconnect")
            self.finish()
        ask.assert_called_once()
        self.assertFalse(self.service.clipboard_plus_linked())
        self.assertEqual(store.get(item.id).text, "mine")
        self.assertEqual(store.get(item.id).cloud_id, "")
        self.assertIn("Not connected", self.joined())

    def test_disconnecting_can_delete_the_history_on_this_computer(self):
        self.link()
        store = self.store()
        store.add_text("mine", now=1.0)
        self.window.account.render()
        with patch.object(self.gui.messagebox, "askyesnocancel", return_value=False):
            self.press("Disconnect")
            self.finish()
        self.assertEqual(store.count(), 0)
        self.assertFalse(self.service.clipboard_plus_linked())

    def test_cancelling_the_question_changes_nothing(self):
        self.link()
        self.window.account.render()
        with patch.object(self.gui.messagebox, "askyesnocancel", return_value=None):
            self.press("Disconnect")
        self.assertTrue(self.service.clipboard_plus_linked())
        self.assertIsNone(self.window.pending)


class WhereItAppearsTests(AccountCase):
    def test_a_dictation_only_user_is_not_shown_an_account_card(self):
        self.prefs.save(features=hotkeys.Features(True, False))
        self.open_settings()
        self.assertNotIn("Clipboard+ account", self.joined())
        self.assertFalse(self.has_button("Create account"))

    def test_a_dictation_only_user_with_a_linked_key_is_told_what_it_now_needs(self):
        self.link()
        self.prefs.save(features=hotkeys.Features(True, False))
        self.open_settings()
        self.assertIn("Turn on Clipboard history", self.joined())
        self.assertFalse(self.has_button("Sync now"))
        self.assertTrue(self.has_button("Disconnect"))

    def test_dictation_setup_no_longer_uploads_transcripts_by_itself(self):
        self.prefs.save(features=hotkeys.Features(True, True))
        self.window.tutorial()
        self.assertNotIn("Keep every transcript in Clipboard+", self.joined())
        self.assertFalse(self.has_button("Connect"))

    def test_the_clipboard_setup_finish_offers_the_account(self):
        self.window.tutorial_clipboard()
        self.assertIn("Clipboard+ account", self.joined())
        self.assertTrue(self.has_button("Create account"))

    def test_settings_for_both_modes_show_it_once(self):
        self.prefs.save(features=hotkeys.Features(True, True))
        self.open_settings()
        self.assertEqual(self.texts().count("Clipboard+ account"), 1)


class ClearDialogTests(AccountCase):
    def make(self, linked):
        import clipui

        dialog = clipui.ClearDialog(self.root, linked=linked)
        self.addCleanup(lambda: dialog.window.winfo_exists() and dialog.window.destroy())
        return dialog

    def labels(self, dialog):
        found, stack = [], [dialog.window]
        while stack:
            widget = stack.pop()
            stack.extend(widget.winfo_children())
            if widget.winfo_class() in ("TButton", "TRadiobutton", "TCheckbutton", "TLabel"):
                found.append(str(widget.cget("text")))
        return found

    def test_without_an_account_there_is_only_the_favorites_choice(self):
        dialog = self.make(linked=False)
        labels = " | ".join(self.labels(dialog))
        self.assertIn("Keep favorites", labels)
        self.assertNotIn("Everywhere", labels)
        dialog.confirm()
        self.assertEqual(dialog.result, (False, True))

    def test_with_an_account_the_scope_is_asked_and_defaults_to_this_device(self):
        dialog = self.make(linked=True)
        labels = " | ".join(self.labels(dialog))
        self.assertIn("This device only", labels)
        self.assertIn("Everywhere", labels)
        dialog.confirm()
        self.assertEqual(dialog.result, (False, True))

    def test_everywhere_without_favorites_is_returned_as_chosen(self):
        dialog = self.make(linked=True)
        dialog.scope.set("everywhere")
        dialog.keep_favorites.set(False)
        dialog.confirm()
        self.assertEqual(dialog.result, (True, False))

    def test_cancel_returns_nothing(self):
        dialog = self.make(linked=True)
        dialog.cancel()
        self.assertIsNone(dialog.result)


class ClipboardPageClearTests(AccountCase):
    features = hotkeys.Features(False, True)

    def setUp(self):
        super().setUp()
        self.store = clipstore.Store(self.paths.clipboard)
        self.addCleanup(self.store.close)
        with patch.object(self.service, "completed", return_value=True):
            self.window.clipboard()
        self.page = self.window.clipboard_page

    def test_the_choice_decides_what_is_cleared_and_where(self):
        self.link()
        self.store.add_text("old", now=1.0)
        star = self.store.add_text("star", now=2.0)
        self.store.set_favorite(star.id, True)
        with patch.object(self.page, "ask_clear", return_value=(True, True)):
            self.page.clear()
        self.assertEqual([i.text for i in self.store.list()], ["star"])
        self.assertEqual(self.store.meta_get("clear_pending"), "keep")

    def test_this_device_only_leaves_the_account_alone(self):
        self.link()
        item = self.store.add_text("old", now=1.0)
        self.store.link(item.id, "cloud-1", False)
        with patch.object(self.page, "ask_clear", return_value=(False, True)):
            self.page.clear()
        self.assertEqual((self.store.count(), self.store.tombstones()), (0, []))
        self.assertEqual(self.store.meta_get("clear_pending"), "")

    def test_cancelling_clears_nothing(self):
        self.store.add_text("old", now=1.0)
        with patch.object(self.page, "ask_clear", return_value=None):
            self.page.clear()
        self.assertEqual(self.store.count(), 1)

    def test_the_dialog_is_told_whether_an_account_is_linked(self):
        with patch.object(self.page, "ask_clear", return_value=None) as ask:
            self.page.clear()
            ask.assert_called_with(False)
            self.link()
            self.page.clear()
            ask.assert_called_with(True)


if __name__ == "__main__":
    unittest.main()
