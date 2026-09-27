from __future__ import annotations

import array
import gc
import io
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import app_service
import clipstore
import desktop
import dictation as d
import hotkeys


class ServiceCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        environment = patch.dict(
            os.environ,
            {
                "XDG_CONFIG_HOME": str(self.folder / "config"),
                "XDG_CACHE_HOME": str(self.folder / "cache"),
                "XDG_RUNTIME_DIR": str(self.folder / "runtime"),
                "DICTATION_NOTIFY": str(self.folder / "missing-notifier"),
                # A speech model installed on this computer must not count as set up.
                "HOME": str(self.folder / "home"),
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.paths = d.Paths()
        self.service = app_service.Service(self.paths)


class ServiceTests(ServiceCase):
    def test_setup_model_save_and_completion(self):
        self.assertFalse(self.service.completed())
        model = self.folder / "model.bin"
        model.write_bytes(b"lmgg-fixture")
        with (
            patch.object(d.Config, "check"),
            patch.object(app_service.onboarding, "download_model", return_value=model) as download,
        ):
            self.service.prepare("auto", "USB Mic", "")
            self.assertTrue(self.service.ready())
            self.service.complete()
            self.assertTrue(self.service.completed())
            self.assertEqual(d.read_json(self.paths.config)["language"], "auto")
            self.service.prepare("en", "USB Mic", str(model))
            self.assertEqual(download.call_count, 1)
        with patch.dict(os.environ, {"DICTATION_PROMPT": "temporary environment prompt"}):
            with patch.object(d.Config, "check"):
                self.service.prepare("en", "USB Mic", str(model))
        self.assertNotEqual(
            d.read_json(self.paths.config)["prompt"], "temporary environment prompt"
        )
        for language, device in (("bad", "default"), ("en", "")):
            with self.assertRaises(d.DictationError):
                self.service.prepare(language, device, "")
        with patch.object(desktop, "platform_name", return_value="windows"):
            with self.assertRaisesRegex(d.DictationError, "pick the microphone"):
                self.service.prepare("en", "default", "")
        with patch.object(d, "busy", return_value=True):
            with self.assertRaises(d.DictationError):
                self.service.prepare("en", "default", "")

    def test_prepare_with_own_ai_service_saves_private_key(self):
        remote = app_service.Remote("https://api.example.com/v1/audio", "fast-model", "sk-test")
        with patch.object(d.Config, "check"):
            with self.assertRaisesRegex(d.DictationError, "API key"):
                self.service.prepare(
                    "en", "USB Mic", "", remote=app_service.Remote(remote.endpoint, "m", "")
                )
            self.service.prepare("en", "USB Mic", "", remote=remote)
            saved = d.read_json(self.paths.config)
            self.assertEqual(saved["backend"], "http")
            self.assertEqual(saved["endpoint"], "https://api.example.com/v1/audio")
            self.assertEqual(saved["api_model"], "fast-model")
            self.assertTrue(saved["allow_remote"])
            key = d.key_file(self.paths)
            self.assertEqual(key.read_text(), "sk-test")
            if sys.platform != "win32":
                self.assertEqual(key.stat().st_mode & 0o777, 0o600)
            # Blank key keeps the saved one; a local server needs none.
            self.service.prepare(
                "en", "USB Mic", "", remote=app_service.Remote(remote.endpoint, "m", "")
            )
            self.assertEqual(key.read_text(), "sk-test")
            # The key is never sent to a different service: a new host needs its own key.
            other = app_service.Remote("https://api.other.example/v1/audio", "m", "")
            with self.assertRaisesRegex(d.DictationError, "api.other.example"):
                self.service.prepare("en", "USB Mic", "", remote=other)
            self.assertEqual(d.read_json(self.paths.config)["endpoint"], remote.endpoint)
            local = app_service.Remote("http://127.0.0.1:8080/inference", "", "")
            self.service.prepare("en", "USB Mic", "", remote=local)
            self.assertFalse(d.read_json(self.paths.config)["allow_remote"])
            self.assertFalse(key.exists())  # Not handed to a local server either.
            model = self.folder / "model.bin"
            model.write_bytes(b"lmgg-fixture")
            self.service.prepare("en", "USB Mic", str(model))
            self.assertFalse(key.exists())  # Switching back to on-device forgets the key.
            self.assertEqual(d.read_json(self.paths.config)["backend"], "local")

    def test_microphone_parsing_without_capturing(self):
        samples = {
            "alsa": (
                "null\n    Discard all samples\ndefault\n    Default device\n"
                "hw:CARD=USB,DEV=0\n    USB Mic, USB Audio\n    Direct hardware device\n"
                "plughw:CARD=USB,DEV=0\n    USB Mic, USB Audio\n    With conversions\n"
                "dsnoop:CARD=USB,DEV=0\n    USB Mic, USB Audio\n",
                ["default", "plughw:CARD=USB,DEV=0"],
            ),
            "dshow": (
                '[dshow] "Webcam" (video)\n[dshow] "USB Mic" (audio)\n[dshow] "USB Mic" (audio)',
                ["USB Mic"],
            ),
            "avfoundation": (
                "AVFoundation video devices:\n[0] Camera\nAVFoundation audio devices:\n[avf @ 1] [0] Mac microphone\n[avf @ 1] [1] USB",
                ["Mac microphone", "USB"],
            ),
        }
        for backend, (output, expected) in samples.items():
            with (
                patch.object(desktop, "audio_backend", return_value=backend),
                patch.object(
                    app_service.subprocess,
                    "run",
                    return_value=Mock(stdout=b"", stderr=output.encode(), returncode=0),
                ) as command,
            ):
                self.assertEqual(self.service.microphones(), expected)
                self.assertTrue(
                    "-L" in command.call_args.args[0]
                    or "-list_devices" in command.call_args.args[0]
                )
        with (
            patch.object(desktop, "audio_backend", return_value="alsa"),
            patch.object(
                app_service.subprocess,
                "run",
                return_value=Mock(stdout=b"", stderr=b"denied", returncode=1),
            ),
        ):
            with self.assertRaisesRegex(d.DictationError, "Microphone"):
                self.service.microphones()

    def test_microphones_have_friendly_names(self):
        listing = (
            b"default\n    Default\nplughw:CARD=C920,DEV=0\n    HD Pro Webcam C920, USB Audio\n"
        )
        with (
            patch.object(desktop, "audio_backend", return_value="alsa"),
            patch.object(
                app_service.subprocess,
                "run",
                return_value=Mock(stdout=listing, stderr=b"", returncode=0),
            ),
        ):
            self.service.microphones()
        self.assertEqual(
            self.service.microphone_names["plughw:CARD=C920,DEV=0"], "HD Pro Webcam C920, USB Audio"
        )
        self.assertIn("recommended", self.service.microphone_names["default"])

    def test_a_microphone_test_reports_what_it_heard(self):
        def fake(pcm):
            process = Mock(stdout=io.BytesIO(pcm))
            process.poll.return_value = None
            return process

        speech = array.array("h", [6000, -6000] * 8000).tobytes()
        for pcm, expected in (
            (speech, "Sounds good"),
            (b"\0\0" * 16000, "silent or very quiet"),
            (b"", "No sound came"),
        ):
            with self.subTest(expected=expected):
                with patch.object(app_service.subprocess, "Popen", return_value=fake(pcm)):
                    test = app_service.MicrophoneTest(self.paths, "default", seconds=0.2)
                    while not test.done():
                        time.sleep(0.02)
                    test.stop()
                self.assertIn(expected, test.verdict())
        with (
            patch.object(d, "busy", return_value=True),
            self.assertRaisesRegex(d.DictationError, "Finish"),
        ):
            app_service.MicrophoneTest(self.paths, "default")

    def test_actions_route_to_existing_engine(self):
        with patch.object(d, "copy_text") as copy, patch.object(d, "dispatch") as dispatch:
            self.service.action("copy")
            copy.assert_called_once()
            self.service.action("toggle")
            self.assertEqual(dispatch.call_args.args[-1], "toggle")


class ModeServiceTests(ServiceCase):
    def test_setup_is_not_required_for_dictation_when_only_the_clipboard_is_used(self):
        self.assertFalse(self.service.ready())  # No model yet: dictation is not usable.
        hotkeys.Preferences(self.paths).save(features=hotkeys.Features(False, True))
        self.assertTrue(self.service.ready())
        hotkeys.Preferences(self.paths).save(features=hotkeys.Features(True, True))
        self.assertFalse(self.service.ready())


class ClipboardPlusServiceTests(ServiceCase):
    KEY = "cp_live_" + "a1b2c3d4" * 6
    PASSWORD = "correct horse battery staple"
    TOKEN = "eyJ.session.token"

    @property
    def config_dir(self):
        return self.paths.config.parent

    def test_connect_saves_the_key_only_after_the_service_accepts_it(self):
        self.assertFalse(self.service.clipboard_plus_linked())
        for result, message in (
            ("read-only", "read and write access"),
            ("write-only", "can’t read"),
            ("no-access", "no clipboard access"),
            ("invalid", "didn’t accept"),
            ("offline", "internet"),
            ("error", "trouble"),
        ):
            with patch.object(app_service.clipboardplus, "verify", return_value=result):
                with self.assertRaisesRegex(d.DictationError, message):
                    self.service.connect_clipboard_plus(self.KEY)
            self.assertFalse(self.service.clipboard_plus_linked())
        with self.assertRaisesRegex(d.DictationError, "cp_live_"):
            self.service.connect_clipboard_plus("not-a-key")
        with patch.object(app_service.clipboardplus, "verify", return_value="ok"):
            self.service.connect_clipboard_plus(f"  {self.KEY}  ")
        self.assertTrue(self.service.clipboard_plus_linked())
        self.service.disconnect_clipboard_plus()
        self.assertFalse(self.service.clipboard_plus_linked())

    def sign_in(self, create, **outcome):
        cp = app_service.clipboardplus
        with (
            patch.object(cp, "register", return_value=self.TOKEN) as register,
            patch.object(cp, "login", return_value=self.TOKEN) as login,
            patch.object(cp, "create_key", return_value=self.KEY, **outcome) as make_key,
        ):
            self.service.sign_in_clipboard_plus(" Me@Example.com ", self.PASSWORD, create=create)
        return register, login, make_key

    def test_signing_in_makes_a_scoped_key_and_keeps_neither_password_nor_token(self):
        register, login, make_key = self.sign_in(create=False)
        login.assert_called_once_with("Me@Example.com", self.PASSWORD)
        register.assert_not_called()
        self.assertEqual(make_key.call_args.args[0], self.TOKEN)
        self.assertIn(hotkeys.APP_NAME, make_key.call_args.args[1])
        self.assertTrue(self.service.clipboard_plus_linked())
        self.assertEqual(self.service.clipboard_plus_email(), "Me@Example.com")
        for path in self.config_dir.rglob("*"):
            if path.is_file():
                text = path.read_text(errors="ignore")
                self.assertNotIn(self.PASSWORD, text)
                self.assertNotIn(self.TOKEN, text)

    def test_creating_an_account_registers_first(self):
        register, login, _ = self.sign_in(create=True)
        register.assert_called_once_with("Me@Example.com", self.PASSWORD)
        login.assert_not_called()
        self.assertTrue(self.service.clipboard_plus_linked())

    def test_a_refused_sign_in_shows_the_message_and_links_nothing(self):
        cp = app_service.clipboardplus
        with patch.object(cp, "login", side_effect=cp.AuthError("Wrong email or password.")):
            with self.assertRaisesRegex(d.DictationError, "Wrong email or password"):
                self.service.sign_in_clipboard_plus("a@b.co", self.PASSWORD, create=False)
        self.assertFalse(self.service.clipboard_plus_linked())
        # A key that cannot be made after signing in leaves nothing behind either.
        with (
            patch.object(cp, "login", return_value=self.TOKEN),
            patch.object(cp, "create_key", side_effect=cp.AuthError("You already have 10 keys.")),
        ):
            with self.assertRaisesRegex(d.DictationError, "10 keys"):
                self.service.sign_in_clipboard_plus("a@b.co", self.PASSWORD, create=False)
        self.assertFalse(self.service.clipboard_plus_linked())
        self.assertEqual(self.service.clipboard_plus_email(), "")

    def test_email_and_password_are_required(self):
        for email, password in (("", "x"), ("a@b.co", ""), ("   ", "   ")):
            with self.subTest(email=email):
                with self.assertRaisesRegex(d.DictationError, "email and password"):
                    self.service.sign_in_clipboard_plus(email, password, create=False)

    def test_a_pasted_key_has_no_email(self):
        with patch.object(app_service.clipboardplus, "verify", return_value="ok"):
            self.service.connect_clipboard_plus(self.KEY)
        self.assertEqual(self.service.clipboard_plus_email(), "")

    def state(self, status=None):
        import clipservice

        if status is not None:
            clipservice.write_status(self.paths, "capturing", 3, "", sync=status, synced=1234.0)
        return self.service.clipboard_plus_state()

    def test_the_account_state_follows_the_key_and_the_services_report(self):
        self.assertEqual(self.state().kind, "disconnected")
        with patch.object(app_service.clipboardplus, "verify", return_value="ok"):
            self.service.connect_clipboard_plus(self.KEY)
        self.assertEqual(self.state().kind, "connected")  # The service has not reported yet.
        self.assertEqual(self.state("ok").kind, "connected")
        self.assertEqual(self.state("ok").synced, 1234.0)
        self.assertEqual(self.state("syncing").sync, "syncing")
        self.assertEqual(self.state("offline").kind, "connected")
        self.assertEqual(self.state("auth").kind, "reconnect")
        self.service.disconnect_clipboard_plus()
        self.assertEqual(self.state().kind, "disconnected")

    def test_sync_now_leaves_a_request_for_the_service(self):
        self.service.sync_clipboard_now()
        self.assertTrue((self.paths.runtime / "clip-sync-now").exists())

    def linked_history(self):
        with patch.object(app_service.clipboardplus, "verify", return_value="ok"):
            self.service.connect_clipboard_plus(self.KEY)
        store = clipstore.Store(self.paths.clipboard)
        self.addCleanup(store.close)
        item = store.add_text("kept", now=1.0)
        store.mark_pushed(item.id, "text|k|kept")
        store.link(item.id, "cloud-1", False)
        store.meta_set("sync_cursor", "5.0")
        return store, item

    def test_linking_another_account_starts_from_scratch(self):
        store, item = self.linked_history()
        other = "cp_live_" + "e5f6a7b8" * 6
        with patch.object(app_service.clipboardplus, "verify", return_value="ok"):
            self.service.connect_clipboard_plus(other)
        again = store.get(item.id)
        self.assertEqual((again.cloud_id, again.cloud_key, again.dirty), ("", "", True))
        self.assertEqual(store.meta_get("sync_cursor"), "")

    def test_disconnecting_can_keep_the_history_and_forgets_the_account(self):
        store, item = self.linked_history()
        self.service.disconnect_clipboard_plus(keep_history=True)
        kept = store.get(item.id)
        self.assertEqual(
            (kept.text, kept.cloud_id, kept.cloud_key, kept.dirty), ("kept", "", "", True)
        )
        self.assertEqual(store.meta_get("sync_cursor"), "")
        self.assertFalse(self.service.clipboard_plus_linked())

    def test_disconnecting_can_also_delete_the_history_here_but_never_the_account_copy(self):
        store, _ = self.linked_history()
        self.service.disconnect_clipboard_plus(keep_history=False)
        self.assertEqual((store.count(), store.tombstones()), (0, []))

    def test_disconnecting_never_creates_the_history_folder(self):
        self.service.disconnect_clipboard_plus()
        self.assertFalse(self.paths.clipboard.exists())

    def test_clearing_this_device_never_reaches_the_account(self):
        store, item = self.linked_history()
        count = self.service.clear_clipboard(store, everywhere=False, keep_favorites=True)
        self.assertEqual((count, store.count(), store.tombstones()), (1, 0, []))
        self.assertEqual(store.meta_get("clear_pending"), "")

    def test_clearing_everywhere_asks_the_account_to_clear_too(self):
        for keep, expected in ((True, "keep"), (False, "all")):
            with self.subTest(keep=keep):
                store, _ = self.linked_history()
                self.service.clear_clipboard(store, everywhere=True, keep_favorites=keep)
                self.assertEqual(store.meta_get("clear_pending"), expected)
                self.assertTrue((self.paths.runtime / "clip-sync-now").exists())
                (self.paths.runtime / "clip-sync-now").unlink()
                store.meta_set("clear_pending", "")
                store.wipe()

    def test_clearing_everywhere_without_an_account_is_just_a_local_clear(self):
        store = clipstore.Store(self.paths.clipboard)
        self.addCleanup(store.close)
        store.add_text("x", now=1.0)
        self.service.clear_clipboard(store, everywhere=True, keep_favorites=True)
        self.assertEqual(store.meta_get("clear_pending"), "")


class WindowTests(ServiceCase):
    @classmethod
    def setUpClass(cls):
        try:
            import tkinter as tk

            probe = tk.Tk()
            probe.withdraw()
            probe.destroy()
        except (ImportError, Exception) as exc:
            raise unittest.SkipTest(f"Tk/display unavailable: {exc}")

    def setUp(self):
        super().setUp()
        import app

        self.gui = app
        self.root = app.tk.Tk()
        self.root.withdraw()
        self.window = app.App(self.root, self.service)
        self.addCleanup(self.cleanup_window)

    def cleanup_window(self):
        if self.window.pending:
            self.window.pending.result(timeout=5)
        if self.window.page != "closed":
            self.window.destroy()
        self.window = None
        self.root = None
        gc.collect()  # Tcl objects must be finalized on their owning UI thread.

    def tick(self):
        self.root.after_cancel(self.window.timer)
        self.window.poll()

    def finish(self):
        self.window.pending.result(timeout=5)
        self.tick()

    def settle(self):
        """Wait for background lookups (microphones), then let the window use them."""
        for future, _ in list(self.window.lookups):
            future.result(timeout=5)
        self.tick()

    def test_wide_window_centers_one_column_and_tabs_hold_still(self):
        with patch.object(self.service, "completed", return_value=True):
            self.window.settings()
            self.root.deiconify()
            self.root.geometry("1800x900")
            self.root.update()
            canvas = self.window.canvas
            left = int(canvas.coords(self.window.frame_window)[0])
            column = int(canvas.itemcget(self.window.frame_window, "width"))
            self.assertEqual(column, self.gui.CONTENT_MAX)
            self.assertGreater(left, 0)
            # The header lines up with the column instead of the far edge of the screen.
            self.assertEqual(
                int(self.window.header_bar.cget("padding")[0]), self.gui.PAD - 4 + left
            )
            places = []
            for page in ("clipboard", "dictation", "settings"):
                self.window.tab(page)
                self.root.update()
                places.append(
                    [
                        (tab.winfo_x(), tab.winfo_width(), tab.winfo_height())
                        for tab in self.window.nav.winfo_children()
                    ]
                )
            self.assertEqual(places[0], places[1])
            self.assertEqual(places[1], places[2])

    def test_help_goes_back_to_the_tab_instead_of_closing(self):
        with patch.object(self.service, "completed", return_value=True):
            self.window.home()
            self.window.tutorial()
            [back] = [
                button
                for button in self.window.bar_actions.winfo_children()
                if button.cget("text") == "Back"
            ]
            back.invoke()
        self.assertEqual(self.window.page, "home")

    def test_enter_presses_the_pages_main_action(self):
        with (
            patch.object(self.service, "completed", return_value=True),
            patch.object(self.gui.App, "prepare") as prepare,
        ):
            self.window.settings()
            self.assertEqual(
                self.window.on_return(SimpleNamespace(widget=self.window.frame)), "break"
            )
            prepare.assert_called_once()

    def test_shortcut_capture_lets_escape_cancel_and_tab_move(self):
        self.window.shortcut_page(back=self.window.home)
        self.assertIsNone(self.window.shortcut_key(SimpleNamespace(keysym="Tab")))
        self.window.shortcut_key(SimpleNamespace(keysym="Escape"))
        self.assertEqual(self.window.page, "home")

    def test_closing_during_download_pauses_it_first(self):
        release = __import__("threading").Event()
        self.window.pause_download = self.gui.ttk.Button(self.window.bar_actions)
        self.window.pause_download.pack()
        self.window.submit(release.wait, lambda _: None, "Downloading")
        with patch.object(self.gui.messagebox, "askokcancel", return_value=True):
            self.window.close()
        self.assertTrue(self.window.download_pause.is_set())
        self.assertNotEqual(self.window.page, "closed")
        release.set()
        self.finish()
        self.assertEqual(self.window.page, "closed")

    def test_leaving_settings_with_unsaved_changes_asks_first(self):
        with patch.object(self.service, "completed", return_value=True):
            self.window.settings()
            with patch.object(self.gui.messagebox, "askyesnocancel") as ask:
                self.window.tab("settings")  # Nothing changed: no question.
                ask.assert_not_called()
            self.window.model_source.set("service")
            with patch.object(self.gui.messagebox, "askyesnocancel", return_value=None):
                self.window.tab("dictation")
            self.assertEqual(self.window.page, "settings")  # Cancel stays.
            with patch.object(self.gui.messagebox, "askyesnocancel", return_value=False):
                self.window.tab("dictation")
            self.assertEqual(self.window.page, "home")  # Don't save leaves.

    def test_unreadable_settings_offer_a_reset_instead_of_crashing(self):
        d.private_dir(self.paths.config.parent)
        self.paths.config.write_text('{"language": 5}', encoding="utf-8")
        with patch.object(self.service, "completed", return_value=True):
            self.window.settings()
            self.assertEqual(self.window.page, "settings")
            [reset] = [
                button
                for button in self.window.bar_actions.winfo_children()
                if button.cget("text") == "Reset dictation settings"
            ]
            with patch.object(self.gui.App, "find_microphones"):
                reset.invoke()
        self.assertTrue(self.paths.config.with_name("config.json.bak").exists())
        self.assertIn("Settings reset", self.window.status.get())

    def test_maximized_size_is_not_remembered(self):
        with patch.object(self.window, "maximized", return_value=True):
            self.root.deiconify()
            self.root.geometry("1500x900")
            self.root.update()
            self.window.save_size()
        self.assertFalse(self.window.size_file().exists())

    def test_both_after_clipboard_only_still_sets_up_dictation(self):
        self.window.after_features("clipboard")
        self.window.after_optin(True)  # Clipboard only: dictation saved as off.
        self.window.choose_features()
        with patch.object(self.gui.App, "find_microphones"):
            self.window.after_features("both")
        self.assertIn("dictation", self.window.setup_steps)
        self.assertEqual(self.window.page, "settings")
        self.assertTrue(hotkeys.Preferences(self.paths).features().dictation)

    def test_first_launch_walkthrough_no_recording(self):
        self.window.tray = False
        self.assertEqual(self.window.page, "welcome")
        with (
            patch.object(self.service, "action") as record,
            patch.object(self.service, "ready", return_value=False),
        ):
            self.window.after_features("dictation")
            self.assertEqual(self.window.page, "settings")
            with patch.object(
                self.gui.filedialog, "askopenfilename", return_value="/selected/model.bin"
            ):
                self.window.choose_model()
            self.assertEqual(self.window.model.get(), "/selected/model.bin")
            with patch.object(self.gui.filedialog, "askopenfilename", return_value=""):
                self.window.choose_model()
            with patch.object(self.service, "microphones", return_value=["USB Mic"]):
                self.window.find_microphones()
                self.settle()
            self.assertEqual(self.window.device.get(), "USB Mic")
            with patch.object(self.service, "prepare") as prepare:
                self.window.prepare()
                self.finish()
                prepare.assert_called_once()
            self.assertEqual(self.window.page, "tutorial")
            self.window.finish_setup()
            self.finish()
            self.assertEqual(self.window.page, "home")
            self.assertTrue(d.read_json(self.service.marker)["complete"])
            record.assert_not_called()

    def test_settings_shows_friendly_microphones_and_saves_the_device(self):
        def found():
            self.service.microphone_names = {"default": "System default", "plughw:X": "USB Mic"}
            return ["default", "plughw:X"]

        with patch.object(self.service, "microphones", side_effect=found):
            self.window.settings()
            self.root.update_idletasks()
            self.settle()
        self.assertEqual(self.window.device.get(), "System default")  # Not the first found.
        self.assertEqual(
            list(self.window.device_picker.cget("values")), ["System default", "USB Mic"]
        )
        self.window.device.set("USB Mic")
        with patch.object(self.service, "prepare") as prepare:
            self.window.prepare()
            self.finish()
        self.assertEqual(prepare.call_args.args[1], "plughw:X")

    def test_testing_the_microphone_shows_a_meter_then_a_verdict(self):
        with patch.object(self.service, "microphones", return_value=["default"]):
            self.window.settings()
            self.settle()
        test = Mock(level=0.6, peak=0.7)
        test.done.return_value = False
        test.verdict.return_value = "Sounds good. This microphone is ready."
        with patch.object(self.gui, "MicrophoneTest", return_value=test) as start:
            self.window.test_microphone()
        start.assert_called_once_with(self.paths, "default")
        self.assertTrue(self.window.meter.winfo_manager())
        self.assertEqual(self.window.status.get(), "Say something…")
        test.done.return_value = True
        self.window.meter_tick()
        test.stop.assert_called_once()
        self.assertEqual(self.window.status.get(), "Sounds good. This microphone is ready.")
        with patch.object(self.gui, "MicrophoneTest", side_effect=d.DictationError("Busy")):
            self.window.test_microphone()
        self.assertEqual(self.window.status.get(), "Busy")

    def test_settings_discovers_microphones_and_reports_download(self):
        with patch.object(self.service, "microphones", return_value=["Built-in", "USB Mic"]):
            self.window.settings()
            self.root.update_idletasks()  # Runs the scheduled discovery.
            self.settle()
        self.assertEqual(self.window.device.get(), "Built-in")
        self.assertIn("2 microphones found", self.window.status.get())
        self.window.model_source.set("file")
        self.window.model.set("")
        self.window.prepare()
        self.assertIsNone(self.window.pending)
        self.assertIn("Choose a model file", self.window.status.get())
        self.window.model_source.set("download")
        release = __import__("threading").Event()

        def download(language, device, model, progress, remote=None, pause=None):
            self.assertEqual(model, "")
            progress(50_000_000, 150_000_000)
            release.wait(5)

        with patch.object(self.service, "prepare", side_effect=download):
            self.window.prepare()
            for _ in range(100):
                if self.window.download[0]:
                    break
                time.sleep(0.01)
            self.tick()
            self.assertIn("50 MB of 150 MB (33%)", self.window.status.get())
            self.assertEqual(str(self.window.progress.cget("mode")), "determinate")
            release.set()
            self.finish()
        self.assertEqual(self.window.page, "tutorial")
        self.window.download = (1_000_000, 0)
        self.window.show_download()
        self.assertIn("1 MB", self.window.status.get())
        self.window.find_microphones()  # Ignored outside the settings page.
        self.assertIsNone(self.window.pending)

    def test_macos_setup_shows_shortcut_and_closes(self):
        self.window.tray = True
        with patch.object(desktop, "platform_name", return_value="macos"):
            self.window.tutorial()
        labels = []
        stack = [self.window.frame]
        while stack:
            widget = stack.pop()
            stack.extend(widget.winfo_children())
            if widget.winfo_class() == "TLabel":
                labels.append(str(widget.cget("text")))
        self.assertTrue(
            any(f"Press {hotkeys.DEFAULT.label('macos')} in any app" in text for text in labels)
        )
        self.assertEqual(self.window.bar_actions.winfo_children()[0].cget("text"), "Done")
        self.window.finish_setup()
        self.finish()
        self.assertEqual(self.window.page, "closed")

    def test_settings_page_argument(self):
        self.window.destroy()
        with patch.object(self.service, "completed", return_value=True):
            self.root = self.gui.tk.Tk()
            self.root.withdraw()
            self.window = self.gui.App(self.root, self.service, "settings")
        self.assertEqual(self.window.page, "settings")
        with patch.object(self.gui.tk, "Tk"), patch.object(self.gui, "App") as window:
            self.assertEqual(self.gui.main(["--settings"]), 0)
            self.assertEqual(window.call_args.args[2], "settings")
            self.assertEqual(self.gui.main(["--shortcut"]), 0)
            self.assertEqual(window.call_args.args[2], "shortcut")
            self.assertEqual(self.gui.main([]), 0)
            self.assertEqual(window.call_args.args[2], "")

    def test_own_ai_service_choice(self):
        with patch.object(self.service, "microphones", return_value=["USB Mic"]):
            self.window.settings()
        service = self.window.choices["service"][0]
        self.assertEqual(service.winfo_manager(), "")  # Hidden until chosen.
        self.window.model_source.set("service")
        self.window.show_choice()
        self.assertEqual(service.winfo_manager(), "pack")
        self.window.provider.set("Groq")
        self.window.choose_provider()
        self.assertIn("groq.com", self.window.endpoint.get())
        self.window.endpoint.set("")
        self.window.prepare()
        self.assertIn("service URL", self.window.status.get())
        self.window.choose_provider()
        self.window.api_key.set("gsk-test")
        self.window.device.set("USB Mic")
        with patch.object(self.service, "prepare") as prepare:
            self.window.prepare()
            self.finish()
        remote = prepare.call_args.args[4]
        self.assertEqual((remote.model, remote.key), ("whisper-large-v3-turbo", "gsk-test"))
        # Reopening settings for a configured service selects it again.
        d.private_dir(self.paths.config.parent)
        d.atomic(
            self.paths.config,
            '{"backend": "http", "endpoint": "https://custom.example/v1", "api_model": "m"}',
        )
        with patch.object(self.service, "microphones", return_value=["USB Mic"]):
            self.window.settings()
        self.assertEqual(self.window.model_source.get(), "service")
        self.assertEqual(self.window.provider.get(), "Other (OpenAI-compatible)")
        self.assertEqual(self.window.endpoint.get(), "https://custom.example/v1")

    def test_the_dictation_tutorial_shows_the_shortcut_for_each_platform(self):
        with patch.object(desktop, "platform_name", return_value="windows"):
            self.window.tutorial()
            texts = self.texts()
        self.assertTrue(
            any(f"Press {hotkeys.DEFAULT.label('windows')} in any app" in text for text in texts)
        )
        self.assertTrue(any("system tray" in text for text in texts))
        with patch.object(desktop, "platform_name", return_value="linux"):
            self.window.tutorial()
            self.assertTrue(any("dictate-toggle" in text for text in self.texts()))

    def texts(self):
        found, stack = [], [self.window.frame]
        while stack:
            widget = stack.pop()
            stack.extend(widget.winfo_children())
            if widget.winfo_class() == "TLabel":
                found.append(str(widget.cget("text")))
        return found

    def test_the_window_is_named_after_the_app(self):
        self.assertEqual(self.root.title(), hotkeys.APP_NAME)
        self.assertIn(hotkeys.APP_NAME, self.texts_in(self.window.root))

    def texts_in(self, widget):
        found, stack = [], [widget]
        while stack:
            current = stack.pop()
            stack.extend(current.winfo_children())
            if current.winfo_class() == "TLabel":
                found.append(str(current.cget("text")))
        return found

    def test_home_leads_with_the_shortcut_and_makes_recording_optional(self):
        self.window.home()
        texts = self.texts()
        shortcut = hotkeys.Preferences(self.paths).shortcut().label()
        self.assertTrue(any(f"Press {shortcut} to dictate" in text for text in texts))
        self.assertTrue(any("don’t need this window" in text for text in texts))
        self.assertTrue(any("Optional" in text for text in texts))
        self.assertNotIn("What’s on your mind?", texts)

    def test_shortcut_window_captures_keys(self):
        self.window.shortcut_page()
        capture = self.paths.runtime / "shortcut-capture"
        self.assertTrue(capture.exists())

        def key(kind, keysym):
            event = Mock(keysym=keysym)
            return (
                self.window.shortcut_key(event)
                if kind == "press"
                else self.window.shortcut_release(event)
            )

        self.assertEqual(key("press", "Shift_L"), "break")
        key("press", "d")
        self.assertIn("Include", self.window.shortcut_hint.cget("text"))
        self.assertIn("disabled", self.window.save_shortcut_button.state())
        key("release", "Shift_L")
        key("press", "Control_L")
        key("press", "Alt_L")
        key("press", "space")
        self.assertNotIn("disabled", self.window.save_shortcut_button.state())
        self.window.save_shortcut()
        self.assertEqual(
            self.gui.hotkeys.Preferences(self.paths).shortcut(),
            self.gui.hotkeys.Shortcut(("ctrl", "alt"), "Space"),
        )
        self.assertEqual(self.window.page, "closed")
        self.assertFalse(capture.exists())

    def test_the_mouse_wheel_scrolls_a_long_page(self):
        seen = []
        self.window.canvas.yview_scroll = lambda steps, what: seen.append(steps)
        event = SimpleNamespace(widget=self.window.frame, num=5, delta=0)
        self.window.wheel(event)
        self.assertEqual(seen, [])  # Nothing to scroll while the page fits.
        self.window.scroll(0.0, 0.5)
        self.window.wheel(event)
        self.window.wheel(SimpleNamespace(widget=self.window.frame, num=4, delta=0))
        self.assertEqual(seen, [3, -3])
        self.window.wheel(SimpleNamespace(widget="popdown", num=5, delta=0))
        self.assertEqual(len(seen), 2)  # A combobox list scrolls itself.

    def test_the_mouse_wheel_never_changes_a_dropdown(self):
        # Scrolling the page past a setting must not change it.
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.assertEqual(self.window.root.bind_class("TCombobox", sequence), "")
        from tkinter import ttk

        box = ttk.Combobox(self.window.frame, values=("One", "Two"), state="readonly")
        box.pack()
        box.set("One")
        self.window.root.update()
        box.event_generate("<Button-5>")
        box.event_generate("<MouseWheel>", delta=-120)
        self.assertEqual(box.get(), "One")

    def test_settings_after_setup_is_a_settings_page_that_saves_in_place(self):
        with (
            patch.object(self.service, "completed", return_value=True),
            patch.object(self.service, "microphones", return_value=["Mic"]),
            patch.object(self.service, "prepare"),
        ):
            self.window.settings()
            self.assertEqual(self.window.step.get(), "")
            # Everything saves as it changes: no Save button to forget.
            self.assertEqual(self.window.bar_actions.winfo_children(), [])
            self.window.language.set("Multilingual / auto-detect")
            self.window.save_voice()
            self.assertEqual(d.read_json(self.paths.config)["language"], "auto")
            # The AI choice shows Apply only once it differs from what's in use.
            self.root.update()
            self.assertFalse(self.window.apply_row.winfo_manager())
            self.window.model_source.set("service")
            self.root.update()
            self.assertTrue(self.window.apply_row.winfo_manager())
            self.window.apply_button.invoke()
            self.finish()
            self.root.update()
            self.assertFalse(self.window.apply_row.winfo_manager())
        self.assertEqual(self.window.page, "settings")
        self.assertEqual(self.window.status.get(), "Transcription AI applied.")

    def test_scrollbar_hides_when_content_fits(self):
        self.window.scroll(0.0, 1.0)
        self.assertEqual(self.window.scrollbar.winfo_manager(), "")
        self.window.scroll(0.0, 0.5)
        self.assertEqual(self.window.scrollbar.winfo_manager(), "pack")

    def test_existing_setup_and_recording_controls(self):
        with patch.object(self.service, "ready", return_value=True):
            self.window.after_features("dictation")
        self.assertEqual(self.window.page, "tutorial")
        self.window.tray = False
        with patch.object(self.service, "completed", return_value=True):
            self.window.settings()
            self.window.tab("dictation")
        self.assertEqual(self.window.page, "home")
        self.tick()
        self.assertIn("disabled", self.window.cancel.state())
        d.atomic(self.paths.text, "Words to copy")
        self.tick()
        self.assertIn("Words to copy", self.window.transcript.get("1.0", "end"))
        self.assertNotIn("disabled", self.window.copy.state())
        with patch.object(self.service, "action") as action:
            self.window.action("copy")
            self.finish()
            self.assertIn("Copied", self.window.status.get())
            action.assert_called_once_with("copy")
        d.atomic(self.paths.state, '{"phase":"recording","elapsed_seconds":3}')
        d.atomic(self.paths.preview, "live words")
        with patch.object(d, "busy", return_value=True):
            self.tick()
            self.assertEqual(self.window.record.cget("text"), "Stop and transcribe")
            self.assertNotIn("disabled", self.window.cancel.state())
            self.assertIn("live words", self.window.transcript.get("1.0", "end"))
        self.paths.audio.write_bytes(b"saved")
        self.tick()
        self.assertIn("Retry", self.window.status.get())
        d.atomic(
            self.paths.state,
            '{"phase":"error","message":"Transcript saved, but clipboard copy failed."}',
        )
        self.tick()
        self.assertIn("clipboard copy failed", self.window.status.get())
        d.atomic(self.paths.state, '{"phase":"idle"}')
        self.tick()
        self.assertIn("discard", self.window.status.get())
        self.paths.audio.unlink()
        self.window.status.set("Transcribing")
        self.tick()
        self.assertIn("Ready", self.window.status.get())

    def test_background_errors_and_close_safety(self):
        self.window.settings()
        self.window.submit(
            lambda: (_ for _ in ()).throw(d.DictationError("Friendly failure")),
            lambda _: None,
            "Working",
        )
        self.window.submit(lambda: None, lambda _: None, "Should not replace")
        with self.assertRaises(d.DictationError):
            self.window.pending.result(timeout=5)
        self.tick()
        self.assertEqual(self.window.status.get(), "Friendly failure")
        self.window.submit(lambda: time.sleep(0.05), lambda _: None, "Working")
        with patch.object(self.gui.messagebox, "showinfo") as info:
            self.window.close()
            info.assert_called_once()
        self.finish()
        self.window.home()
        with patch.object(self.gui.messagebox, "askyesno", return_value=False):
            self.window.discard_audio()
        with (
            patch.object(self.gui.messagebox, "askyesno", return_value=True),
            patch.object(self.service, "action") as action,
        ):
            self.window.discard_audio()
            self.finish()
            action.assert_called_with("discard")
        # A recording runs in its own process: closing never stops or waits for it.
        with (
            patch.object(d, "busy", return_value=True),
            patch.object(self.service, "action") as action,
        ):
            self.window.close()
            action.assert_not_called()
        self.assertEqual(self.window.page, "closed")

    def test_activation_existing_user_and_file_errors(self):
        d.atomic(self.paths.runtime / "show-window", "show")
        with patch.object(self.root, "deiconify"), patch.object(self.root, "lift") as lift:
            self.tick()
            lift.assert_called_once()
        self.window.home()
        with patch.object(self.gui.workflow, "snapshot", side_effect=OSError("private")):
            self.tick()
        self.assertNotIn("private", self.window.status.get())
        self.window.close()
        with patch.object(self.service, "completed", return_value=True):
            self.root = self.gui.tk.Tk()
            self.root.withdraw()
            self.window = self.gui.App(self.root, self.service)
        self.assertEqual(self.window.page, "home")

    def test_second_launch_opens_the_requested_page_in_the_running_window(self):
        fd = desktop.lock(self.paths.runtime / "app.lock")
        try:
            self.assertEqual(self.gui.main(["--shortcut"]), 0)
        finally:
            os.close(fd)
        self.assertEqual((self.paths.runtime / "show-window").read_text(), "shortcut")
        self.tick()
        self.assertEqual(self.window.page, "shortcut")
        self.window.destroy()
        self.root = self.gui.tk.Tk()
        self.root.withdraw()
        with patch.object(self.service, "completed", return_value=True):
            self.window = self.gui.App(self.root, self.service)
            d.atomic(self.paths.runtime / "show-window", "settings")
            with patch.object(self.service, "microphones", return_value=[]):
                self.tick()
        self.assertEqual(self.window.page, "settings")

    def test_leaving_the_shortcut_page_resumes_the_shortcut(self):
        self.window.shortcut_page()
        self.assertTrue((self.paths.runtime / "shortcut-capture").exists())
        self.window.home()
        self.assertFalse((self.paths.runtime / "shortcut-capture").exists())
        self.assertEqual(self.root.bind("<KeyPress>"), "")

    def test_an_unexpected_error_does_not_stop_the_window_updating(self):
        self.window.submit(lambda: None, lambda _: 1 / 0, "Working")
        self.window.pending.result(timeout=5)
        self.tick()
        self.assertIn("Something went wrong", self.window.status.get())
        self.assertIn(self.window.timer, self.root.tk.call("after", "info"))

    def test_home_shows_cancel_retry_and_discard_only_when_they_apply(self):
        self.window.home()

        def shown():
            names = ("cancel", "retry", "discard")
            return {n for n in names if getattr(self.window, n).winfo_manager()}

        def state(**current):
            base = {"active": False, "phase": "idle", "retained_audio": False, "message": ""}
            with patch.object(self.gui.workflow, "snapshot", return_value=base | current):
                self.window.refresh()

        state()
        self.assertEqual(shown(), set())
        state(active=True, phase="recording")
        self.assertEqual(shown(), {"cancel"})
        state(retained_audio=True)
        self.assertEqual(shown(), {"retry", "discard"})
        # Retry stays rightmost, Discard to its left.
        self.root.update_idletasks()
        self.assertLess(self.window.discard.winfo_x(), self.window.retry.winfo_x())
        state()
        self.assertEqual(shown(), set())

    def test_recording_bar_and_live_draft_switches_save_at_once(self):
        with patch.object(self.service, "microphones", return_value=["default"]):
            self.window.settings()
        switches = [
            w
            for w in self.all_widgets()
            if w.winfo_class() == "TCheckbutton" and "recording bar" in str(w.cget("text"))
        ]
        self.assertEqual(len(switches), 1)
        switches[0].invoke()  # On by default: this turns it off.
        self.assertFalse(d.read_json(self.paths.config)["overlay"])
        self.assertIn("next recording", self.window.status.get())
        self.assertIn("Settings saved", self.window.toast.get())
        self.assertTrue(self.window.toast_label.winfo_manager())
        self.service.set_option("live", True)
        self.assertTrue(d.Config(self.paths).b("live"))
        with self.assertRaises(ValueError):
            self.service.set_option("backend", True)

    def all_widgets(self):
        found, stack = [], [self.window.root]
        while stack:
            widget = stack.pop()
            stack.extend(widget.winfo_children())
            found.append(widget)
        return found

    def test_home_explains_a_shortcut_that_could_not_be_set(self):
        hotkeys.record_status(self.paths, False)
        with patch.object(desktop, "platform_name", return_value="linux"):
            self.window.home()
        self.assertTrue(any("keyboard settings" in text for text in self.texts()))
        hotkeys.record_status(self.paths, True)
        self.window.home()
        self.assertTrue(any("to dictate" in text for text in self.texts()))

    def test_home_names_a_shortcut_taken_by_something_else_and_takes_it_back(self):
        old = hotkeys.Conflict("Whisper Dictation", hotkeys.GNOME_LIST_PREFIX + "custom0/")
        hotkeys.record_status(self.paths, True, conflict=old)
        self.window.home()
        texts = self.texts()
        self.assertTrue(any("taken by something else" in text for text in texts))
        self.assertTrue(any("Whisper Dictation also uses" in text for text in texts))
        take = next(b for b in self.window.buttons if str(b.cget("text")).startswith("Use "))
        with patch.object(hotkeys, "gnome_release", return_value=True) as release:
            take.invoke()
        release.assert_called_once_with(old.path)
        self.assertTrue(any("to dictate" in text for text in self.texts()))
        self.assertIsNone(hotkeys.shortcut_conflict(self.paths))
        # The desktop's own shortcuts are never unbound: only another choice is offered.
        hotkeys.record_status(self.paths, True, conflict=hotkeys.Conflict("the desktop’s x"))
        self.window.home()
        labels = [str(b.cget("text")) for b in self.window.buttons if b.winfo_exists()]
        self.assertIn("Choose another shortcut", labels)
        self.assertFalse(any(label.startswith("Use ") for label in labels))

    def test_second_launch_focuses_existing_window(self):
        fd = desktop.lock(self.paths.runtime / "app.lock")
        try:
            self.assertEqual(self.gui.main(), 0)
            self.assertTrue((self.paths.runtime / "show-window").exists())
        finally:
            os.close(fd)
        with patch.object(self.gui.tk, "Tk") as root, patch.object(self.gui, "App") as window:
            self.assertEqual(self.gui.main(), 0)
            window.assert_called_once()
            root.return_value.mainloop.assert_called_once()


if __name__ == "__main__":
    unittest.main()
