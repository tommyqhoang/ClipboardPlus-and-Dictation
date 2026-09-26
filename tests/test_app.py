from __future__ import annotations

import gc
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import app_service
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
            with self.assertRaisesRegex(d.DictationError, "Choose a microphone"):
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
            local = app_service.Remote("http://127.0.0.1:8080/inference", "", "")
            self.service.prepare("en", "USB Mic", "", remote=local)
            self.assertFalse(d.read_json(self.paths.config)["allow_remote"])
            model = self.folder / "model.bin"
            model.write_bytes(b"lmgg-fixture")
            self.service.prepare("en", "USB Mic", str(model))
            self.assertFalse(key.exists())  # Switching back to on-device forgets the key.
            self.assertEqual(d.read_json(self.paths.config)["backend"], "local")

    def test_microphone_parsing_without_capturing(self):
        samples = {
            "alsa": (
                "default\n    Default device\nhw:CARD=USB\n  USB Mic\n",
                ["default", "hw:CARD=USB"],
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

    def test_connect_saves_the_key_only_after_the_service_accepts_it(self):
        self.assertFalse(self.service.clipboard_plus_linked())
        for result, message in (
            ("read-only", "write access"),
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
                self.finish()
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

    def test_settings_discovers_microphones_and_reports_download(self):
        with patch.object(self.service, "microphones", return_value=["Built-in", "USB Mic"]):
            self.window.settings()
            self.root.update()  # Runs the scheduled discovery.
            self.finish()
        self.assertEqual(self.window.device.get(), "Built-in")
        self.assertIn("2 microphones found", self.window.status.get())
        self.window.model_source.set("file")
        self.window.model.set("")
        self.window.prepare()
        self.assertIsNone(self.window.pending)
        self.assertIn("Choose a model file", self.window.status.get())
        self.window.model_source.set("download")
        release = __import__("threading").Event()

        def download(language, device, model, progress, remote=None):
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

    def test_tutorial_recommends_clipboard_plus(self):
        with patch.object(desktop, "platform_name", return_value="windows"):
            self.window.tutorial()
            texts = self.texts()
        self.assertTrue(
            any(f"Press {hotkeys.DEFAULT.label('windows')} in any app" in text for text in texts)
        )
        self.assertTrue(any("system tray" in text for text in texts))
        button = next(
            widget for widget in self.window.buttons if widget.cget("text") == "Get Clipboard+"
        )
        with patch("webbrowser.open") as browser:
            button.invoke()
        browser.assert_called_once_with("https://clipboardplus.apercallc.com")
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

    def test_clipboard_plus_card_connects_and_disconnects(self):
        key = ClipboardPlusServiceTests.KEY
        self.window.tutorial()
        self.assertTrue(any("Clipboard+" in text for text in self.texts()))
        self.assertFalse(any(text.startswith("Connected") for text in self.texts()))
        self.window.clip_key.set(key)
        with patch.object(self.gui.clipboardplus, "verify", return_value="ok"):
            self.window.connect_clipboard_plus()
            self.finish()
        self.assertTrue(self.service.clipboard_plus_linked())
        self.assertEqual(self.window.clip_key.get(), "")  # The key is not left on screen.
        self.assertTrue(any(text.startswith("Connected") for text in self.texts()))
        self.window.disconnect_clipboard_plus()
        self.finish()
        self.assertFalse(self.service.clipboard_plus_linked())
        self.assertFalse(any(text.startswith("Connected") for text in self.texts()))

    def test_clipboard_plus_rejected_key_reports_and_stays_unlinked(self):
        self.window.tutorial()
        self.window.clip_key.set(ClipboardPlusServiceTests.KEY)
        with patch.object(self.gui.clipboardplus, "verify", return_value="invalid"):
            self.window.connect_clipboard_plus()
            self.window.pending.exception(timeout=5)
            self.tick()
        self.assertIn("didn’t accept", self.window.status.get())
        self.assertFalse(self.service.clipboard_plus_linked())

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
        cancel = self.window.bar_actions.winfo_children()[-1]
        self.assertEqual(cancel.cget("text"), "Cancel")
        cancel.invoke()
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
            with patch.object(d, "busy", return_value=True):
                self.window.close()
            self.window.discard_audio()
        with (
            patch.object(self.gui.messagebox, "askyesno", return_value=True),
            patch.object(self.service, "action") as action,
        ):
            self.window.discard_audio()
            self.finish()
            action.assert_called_with("discard")
            with patch.object(d, "busy", return_value=True):
                self.window.close()
                self.finish()
            self.assertTrue(self.window.closing)
        self.tick()
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

    def test_home_explains_a_shortcut_that_could_not_be_set(self):
        hotkeys.record_status(self.paths, False)
        with patch.object(desktop, "platform_name", return_value="linux"):
            self.window.home()
        self.assertTrue(any("keyboard settings" in text for text in self.texts()))
        hotkeys.record_status(self.paths, True)
        self.window.home()
        self.assertTrue(any("to dictate" in text for text in self.texts()))

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
