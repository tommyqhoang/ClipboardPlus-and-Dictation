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
        model.write_bytes(b"fixture")
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
        for language, device in (("bad", "default"), ("en", "")):
            with self.assertRaises(d.DictationError):
                self.service.prepare(language, device, "")
        with patch.object(desktop, "platform_name", return_value="windows"):
            with self.assertRaisesRegex(d.DictationError, "Choose a microphone"):
                self.service.prepare("en", "default", "")
        with patch.object(d, "busy", return_value=True):
            with self.assertRaises(d.DictationError):
                self.service.prepare("en", "default", "")

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
        self.assertEqual(self.window.page, "welcome")
        with (
            patch.object(self.service, "action") as record,
            patch.object(self.service, "ready", return_value=False),
        ):
            self.window.begin_setup()
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

    def test_existing_setup_and_recording_controls(self):
        with patch.object(self.service, "ready", return_value=True):
            self.window.begin_setup()
        self.assertEqual(self.window.page, "tutorial")
        self.window.home()
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
