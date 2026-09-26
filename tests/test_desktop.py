from __future__ import annotations

import errno
import importlib.util
import json
import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import desktop
import dictation

ROOT = Path(__file__).resolve().parents[1]


def setup_module():
    spec = importlib.util.spec_from_file_location("setup_desktop", ROOT / "setup-desktop.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.values = dictation.DEFAULTS.copy()

    def test_platform_selection(self):
        for system, expected in (("darwin", "macos"), ("win32", "windows"), ("linux", "linux")):
            with patch.object(sys, "platform", system):
                self.assertEqual(desktop.platform_name(), expected)

    def test_native_and_override_paths(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(os, "getuid", return_value=1000, create=True),
            patch.object(Path, "home", return_value=Path("/users/tester")),
        ):
            for system, expected in (
                ("macos", "Application Support"),
                ("windows", "WhisperDictation"),
                ("linux", ".config"),
            ):
                with patch.object(desktop, "platform_name", return_value=system):
                    self.assertIn(expected, str(desktop.roots()[0]))
        with patch.dict(
            os.environ,
            {
                "XDG_CONFIG_HOME": "/config",
                "XDG_CACHE_HOME": "/cache",
                "XDG_RUNTIME_DIR": "/runtime",
            },
        ):
            self.assertEqual(
                desktop.roots()[:2], (Path("/config/dictation"), Path("/cache/dictation"))
            )
            self.assertEqual(desktop.roots()[2].parent, Path("/runtime"))

    def test_macos_audio_only_and_device_listing(self):
        with patch.object(desktop, "platform_name", return_value="macos"):
            args = desktop.recorder_command(self.values)
            self.assertEqual(args[args.index("-i") + 1], "none:default")
            self.assertIn("avfoundation", args)
            self.assertIn("s16le", args)
            listing = desktop.recorder_command(self.values, listing=True)
            self.assertIn("-list_devices", listing)
            self.assertEqual(listing[-1], "")
            self.assertEqual(desktop.clipboard_command(self.values), ["/usr/bin/pbcopy"])
            self.assertIn("osascript", desktop.notification_command(self.values)[0])

    def test_windows_device_names_are_not_shell_code(self):
        self.values["device"] = 'Microphone (USB) & "quoted"'
        with patch.object(desktop, "platform_name", return_value="windows"):
            args = desktop.recorder_command(self.values)
            self.assertIn('audio=Microphone (USB) & "quoted"', args)
            self.assertEqual(desktop.recorder_command(self.values, listing=True)[-1], "dummy")
            self.assertIn("Set-Clipboard", desktop.clipboard_command(self.values)[-1])
            self.assertIn("ShowBalloonTip", desktop.notification_command(self.values)[-1])
        with patch.object(sys, "platform", "win32"):
            self.assertTrue(desktop.process_options(detached=True)["creationflags"] & 0x08000000)

    def test_hooks_and_linux_commands(self):
        self.values["arecord"] = "/custom/input.py"
        self.values["wl_copy"] = "/custom/copy.py"
        self.values["audio_backend"] = "alsa"
        self.values["clipboard_backend"] = "wayland"
        self.values["notify"] = "/custom/notify.py"
        self.assertEqual(
            desktop.recorder_command(self.values)[:2], [sys.executable, "/custom/input.py"]
        )
        self.assertEqual(
            desktop.clipboard_command(self.values)[:2], [sys.executable, "/custom/copy.py"]
        )
        self.assertIn("/custom/notify.py", desktop.notification_command(self.values))
        self.assertFalse(desktop.available("/missing/hook.py"))
        self.assertTrue(desktop.available(sys.executable))

    def test_windows_notification_text_is_stdin(self):
        config = Mock()
        config.values = self.values
        config.s.side_effect = lambda key: str(self.values[key])
        with (
            patch.object(desktop, "platform_name", return_value="windows"),
            patch.object(desktop, "available", return_value=True),
            patch.object(dictation.subprocess, "run") as run,
        ):
            dictation.notify(config, "秘密; $unsafe")
            self.assertEqual(run.call_args.kwargs["input"], "秘密; $unsafe".encode())
            self.assertNotIn("秘密; $unsafe", run.call_args.args[0][-1])

    def test_native_lock_released_on_close(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "lock"
            fd = desktop.lock(path)
            self.assertIsNotNone(fd)
            self.assertIsNone(desktop.lock(path))
            os.close(fd)
            replacement = desktop.lock(path)
            self.assertIsNotNone(replacement)
            os.close(replacement)

    @unittest.skipIf(sys.platform == "win32", "Simulates Windows error mapping on Unix")
    def test_windows_lock_error_mapping(self):
        fake = Mock()
        fake.LK_NBLCK = 2
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(desktop, "msvcrt", fake, create=True),
            patch.object(sys, "platform", "win32"),
        ):
            path = Path(folder) / "lock"
            fake.locking.side_effect = OSError(errno.EACCES, "locked")
            self.assertIsNone(desktop.lock(path))
            fake.locking.side_effect = OSError(errno.EIO, "disk error")
            with self.assertRaises(OSError):
                desktop.lock(path)
            fake.locking.side_effect = None
            fd = desktop.lock(path)
            os.close(fd)

    def test_desktop_installer_round_trip(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            prefix = root / "app with spaces"
            env = os.environ | {
                "XDG_CONFIG_HOME": str(root / "config"),
                "XDG_CACHE_HOME": str(root / "cache"),
                "XDG_RUNTIME_DIR": str(root / "runtime"),
                "DICTATION_NOTIFY": str(root / "missing-notifier"),
            }
            setup = [
                sys.executable,
                str(ROOT / "setup-desktop.py"),
                "--prefix",
                str(prefix),
                "--no-shortcut",
            ]
            subprocess.run(setup, env=env, check=True, capture_output=True)
            config = root / "config/dictation/config.json"
            before = config.read_bytes()
            subprocess.run(setup, env=env, check=True, capture_output=True)
            self.assertEqual(config.read_bytes(), before)
            module = prefix / "lib/dictation.py"
            result = subprocess.run(
                [sys.executable, str(module), "--status"],
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn('"phase": "idle"', result.stdout)
            review = subprocess.run(
                [sys.executable, str(module), "--review"],
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(review.returncode, 1)
            self.assertIn("No saved transcript", review.stderr)
            subprocess.run(setup + ["--uninstall"], env=env, check=True, capture_output=True)
            self.assertFalse(module.exists())
            self.assertTrue(config.exists())
            # Nothing of the application is left, including compiled module caches.
            leftovers = [path for path in prefix.rglob("*") if path.is_file()]
            self.assertEqual(leftovers, [])

    def test_windows_start_menu_shortcut_is_renamed_and_the_old_one_removed(self):
        setup = setup_module()
        with patch.object(setup.subprocess, "run") as run:
            setup.windows_shortcut(Path("tray.py"), Path("C:/venv/pythonw.exe"))
        payload = json.loads(run.call_args.kwargs["input"])
        self.assertEqual(payload["name"], setup.hotkeys.APP_NAME + ".lnk")
        self.assertEqual(
            payload["old_names"], ["Whisper Dictation.lnk", "Whisper Dictation & Clipboard+.lnk"]
        )
        script = run.call_args.args[0][-1]
        self.assertIn("$p.name", script)
        self.assertIn("$p.old_names", script)

    def test_windows_shortcut_uses_structured_paths(self):
        setup = setup_module()
        with patch.object(setup.subprocess, "run") as run:
            setup.windows_shortcut(
                Path("path with spaces") / "tray.py", Path("C:/venv/pythonw.exe")
            )
            payload = json.loads(run.call_args.kwargs["input"])
            self.assertIn("path with spaces", payload["arguments"])
            self.assertEqual(payload["python"], str(Path("C:/venv/pythonw.exe")))
            self.assertTrue(any("app.py" in item for item in payload["legacy_arguments"]))
            self.assertNotIn("path with spaces", run.call_args.args[0][-1])
            # The tray registers the shortcut; a Start Menu hotkey would collide.
            self.assertIn("$link.Hotkey=''", setup.SHORTCUT_SCRIPT)
            setup.windows_shortcut(Path("tray.py"))
            self.assertIn("python", json.loads(run.call_args.kwargs["input"])["python"])

    def test_installer_lists_every_module_and_icon_in_lib(self):
        # A module missing from this list installs without error and then fails at launch.
        setup = setup_module()
        lib = ROOT / "lib"
        self.assertEqual({path.name for path in lib.glob("*.py")}, set(setup.MODULES))
        shipped = {name for _, name in setup.ICONS}
        self.assertTrue({path.name for path in lib.glob("*.png")} <= shipped)

    def test_install_copies_every_file_the_launchers_run(self):
        # A real copy, not a mock: the tray script and its icons must reach the prefix.
        setup = setup_module()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with (
                patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(root / "run")}),
                patch.dict(os.environ, {"XDG_CONFIG_HOME": str(root / "config")}),
                patch.object(setup.desktop, "platform_name", return_value="linux"),
                patch.object(setup, "install_app_launcher"),
            ):
                setup.install(root / ".local")
            library = root / ".local/lib"
            for name in ("tray.py", "tray-recording.png", "whisper-dictation.png", "app.py"):
                self.assertTrue((library / name).is_file(), f"{name} was not installed")

    def test_notifications_carry_the_product_name(self):
        import hotkeys

        self.assertEqual(desktop.NOTIFY_NAME, hotkeys.APP_NAME)
        with patch.object(desktop, "platform_name", return_value="linux"):
            command = desktop.notification_command({"notify": ""})
        self.assertEqual(command[:3], ["notify-send", "-a", hotkeys.APP_NAME])

    def test_application_launchers_and_launch(self):
        setup = setup_module()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            prefix = root / ".local"
            (prefix / "lib").mkdir(parents=True)
            (prefix / "lib/app.py").write_text("# app")
            venv = PurePosixPath("/venv/bin/python")
            with (
                patch.object(setup.desktop, "platform_name", return_value="linux"),
                patch.object(setup, "gui_environment", return_value=venv),
                patch.object(setup.hotkeys, "set_login_item") as login,
                patch.object(setup, "stop_menubar") as stop,
            ):
                setup.install_app_launcher(prefix)
                login.assert_called_once_with(True, [str(venv), str(prefix / "lib/tray.py")])
                entry = (prefix / "share/applications/whisper-dictation.desktop").read_text()
                self.assertIn(f"Name={setup.hotkeys.APP_NAME}\n", entry)
                self.assertIn('Exec="/venv/bin/python"', entry)
                self.assertIn("tray.py", entry)
                self.assertIn("Terminal=false", entry)
                self.assertIn(f"Icon={prefix / 'lib/whisper-dictation.png'}", entry)
                with patch.object(setup.subprocess, "Popen") as process:
                    # Still running when the check ends: it started.
                    running = setup.subprocess.TimeoutExpired("app", 3)
                    process.return_value.wait.side_effect = running
                    setup.launch(prefix)  # No private environment yet: the window.
                    self.assertIn(str(prefix / "lib/app.py"), process.call_args.args[0])
                    windowed = setup.gui_python(prefix)[1]
                    windowed.parent.mkdir(parents=True)
                    windowed.touch()
                    setup.launch(prefix)
                    self.assertEqual(
                        process.call_args.args[0], [str(windowed), str(prefix / "lib/tray.py")]
                    )
                    process.return_value.wait.side_effect = None
                    process.return_value.wait.return_value = 0  # A second copy handing over.
                    setup.launch(prefix)
                    process.return_value.wait.return_value = 1  # It died: say so.
                    with self.assertRaisesRegex(setup.dictation.DictationError, "didn't start"):
                        setup.launch(prefix)
                self.assertEqual(stop.call_count, 4)
            with (
                patch.object(setup.desktop, "platform_name", return_value="macos"),
                patch.object(setup, "gui_environment", return_value=venv),
                patch.object(setup.hotkeys, "set_login_item") as login,
                patch.object(setup, "stop_menubar") as stop,
            ):
                setup.install_app_launcher(prefix)
                bundle = root / "Applications/Whisper Dictation.app"
                login.assert_called_once_with(True, ["/usr/bin/open", str(bundle)])
                info = plistlib.loads((bundle / "Contents/Info.plist").read_bytes())
                self.assertEqual(info["CFBundleIdentifier"], "org.whisperdictation.desktop")
                self.assertIn("Record speech only", info["NSMicrophoneUsageDescription"])
                self.assertEqual(info["CFBundleIconFile"], "AppIcon")
                self.assertEqual(info["CFBundleDisplayName"], setup.hotkeys.APP_NAME)
                self.assertEqual(info["CFBundleName"], setup.hotkeys.APP_NAME)
                self.assertTrue((bundle / "Contents/Resources/AppIcon.icns").is_file())
                self.assertTrue(info["LSUIElement"])
                executable = bundle / "Contents/MacOS/WhisperDictation"
                self.assertIn("/venv/bin/python", executable.read_text())
                self.assertIn("menubar.py", executable.read_text())
                self.assertIn(f"WHISPER_DICTATION_BUNDLE='{bundle}'", executable.read_text())
                with patch.object(setup.subprocess, "run") as run:
                    setup.launch(prefix)
                    self.assertEqual(run.call_args.args[0][:1], ["/usr/bin/open"])
                stop.assert_called_once()
            with (
                patch.object(setup.desktop, "platform_name", return_value="windows"),
                patch.object(setup, "gui_environment", return_value=Path("C:/v/pythonw.exe")),
                patch.object(setup.hotkeys, "set_login_item"),
                patch.object(setup, "windows_shortcut") as shortcut,
            ):
                setup.install_app_launcher(prefix)
                self.assertEqual(shortcut.call_args.args[0].name, "tray.py")
                self.assertEqual(shortcut.call_args.args[1], Path("C:/v/pythonw.exe"))
                python, windowed = setup.gui_python(prefix)
                self.assertEqual((python.name, windowed.name), ("python.exe", "pythonw.exe"))

    def test_gui_environment_installs_once(self):
        setup = setup_module()
        with tempfile.TemporaryDirectory() as folder:
            prefix = Path(folder)
            python = prefix / "share/whisper-dictation/venv/bin/python"
            for platform, requirement in (("macos", "pyobjc"), ("linux", "pystray")):
                with (
                    patch.object(setup.desktop, "platform_name", return_value=platform),
                    patch.object(setup, "base_python", return_value="python3"),
                    patch.object(setup.subprocess, "run") as run,
                ):
                    run.return_value.returncode = 1
                    self.assertEqual(setup.gui_environment(prefix), python)
                    commands = [call.args[0] for call in run.call_args_list]
                    self.assertIn("venv", commands[0])
                    self.assertEqual("--system-site-packages" in commands[0], platform == "linux")
                    self.assertTrue(any(requirement in part for part in commands[1]))
            python.parent.mkdir(parents=True)
            python.touch()
            with (
                patch.object(setup.desktop, "platform_name", return_value="linux"),
                patch.object(setup.subprocess, "run") as run,
            ):
                run.return_value.returncode = 0
                setup.gui_environment(prefix)
                run.assert_called_once()  # Only the import probe.
            with (
                patch.object(setup.desktop, "platform_name", return_value="linux"),
                patch.object(setup, "base_python", return_value="python3"),
                patch.object(
                    setup.subprocess,
                    "run",
                    side_effect=[
                        Mock(returncode=1),
                        setup.subprocess.CalledProcessError(1, "venv"),
                    ],
                ),
            ):
                with self.assertRaisesRegex(dictation.DictationError, "tray component"):
                    setup.gui_environment(prefix)

    def test_the_environment_carries_what_the_clipboard_watchers_import(self):
        setup = setup_module()
        # Pillow turns copied images into PNG on macOS/Windows and makes thumbnails;
        # python-xlib is how the X11 watcher sees the clipboard (also under XWayland).
        wanted = {
            "macos": ("pyobjc-framework-Cocoa", "Pillow"),
            "linux": ("pystray", "Pillow", "python-xlib"),
            "windows": ("pystray", "Pillow"),
        }
        for platform, names in wanted.items():
            with self.subTest(platform=platform):
                pinned = " ".join(setup.GUI_REQUIREMENTS[platform])
                for name in names:
                    self.assertIn(name + "==", pinned)
                code = setup.probe_code(platform)
                self.assertIn("PIL", code)
                self.assertEqual("Xlib" in code, platform == "linux")
                self.assertIn("tkinter", code)
        # An install made before the clipboard existed fails the probe and is rebuilt.
        self.assertIn("AppKit", setup.probe_code("macos"))

    def test_base_python_skips_interpreter_without_tk(self):
        setup = setup_module()

        def probe(command, **_):
            # Only the system interpreter can import Tk and GTK.
            return Mock(returncode=0 if command[0] == "/usr/bin/python3" else 1)

        with (
            patch.object(setup.sys, "executable", "/opt/brew/bin/python3"),
            patch.object(setup.shutil, "which", return_value=None),
            patch.object(setup.os.path, "isfile", return_value=True),
            patch.object(setup.subprocess, "run", side_effect=probe),
        ):
            self.assertEqual(setup.base_python("linux"), "/usr/bin/python3")
            # Other platforms keep the running interpreter.
            self.assertEqual(setup.base_python("macos"), "/opt/brew/bin/python3")

    def test_gui_environment_error_names_the_cause(self):
        setup = setup_module()
        with tempfile.TemporaryDirectory() as folder:
            failure = setup.subprocess.CalledProcessError(
                1, "probe", stderr="ImportError: no _tkinter\n"
            )
            with (
                patch.object(setup.desktop, "platform_name", return_value="linux"),
                patch.object(setup, "base_python", return_value="python3"),
                patch.object(
                    setup.subprocess,
                    "run",
                    side_effect=[Mock(), Mock(), failure],
                ),
            ):
                with self.assertRaisesRegex(dictation.DictationError, "no _tkinter"):
                    setup.gui_environment(Path(folder))

    def clipboard_runtime(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        with patch.dict(
            os.environ,
            {
                "XDG_RUNTIME_DIR": folder.name + "/runtime",
                "XDG_CONFIG_HOME": folder.name + "/config",
            },
        ):
            paths = dictation.Paths()
        dictation.private_dir(paths.runtime)
        for name in ("clip-status.json", "clip-sync-now", "clip-ignore.json"):
            (paths.runtime / name).write_text("{}")
        dictation.private_dir(paths.clipboard)
        (paths.clipboard / "clips.db").write_bytes(b"history")
        return paths

    def test_uninstall_stops_the_clipboard_service_and_keeps_the_history(self):
        setup = setup_module()
        paths = self.clipboard_runtime()
        held = desktop.lock(paths.runtime / "clipservice.lock")
        asked = []

        def service_exits(_):
            asked.append((paths.runtime / "clip-quit").exists())
            os.close(held)

        with patch.object(setup.time, "sleep", side_effect=service_exits):
            setup.stop_clipboard_service(paths)
        self.assertEqual(asked, [True])  # It was asked to quit, and waited for it.
        self.assertFalse(list(paths.runtime.glob("clip-*")))
        self.assertEqual((paths.clipboard / "clips.db").read_bytes(), b"history")

    def test_uninstall_leaves_a_stopped_service_alone_and_asks_nothing_of_it(self):
        setup = setup_module()
        paths = self.clipboard_runtime()
        with patch.object(setup.time, "sleep") as sleep:
            setup.stop_clipboard_service(paths)
        sleep.assert_not_called()
        self.assertFalse((paths.runtime / "clip-quit").exists())
        self.assertFalse((paths.runtime / "clip-status.json").exists())

    def test_stop_menubar_requests_quit_and_waits(self):
        setup = setup_module()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": folder.name}):
            paths = dictation.Paths()
        dictation.private_dir(paths.runtime)
        setup.stop_menubar(paths)  # Not running: returns at once.
        self.assertFalse((paths.runtime / "menubar-quit").exists())
        held = desktop.lock(paths.runtime / "menubar.lock")
        with patch.object(setup.time, "sleep", side_effect=lambda _: os.close(held)):
            setup.stop_menubar(paths)
        self.assertTrue((paths.runtime / "menubar-quit").exists())

    def test_launcher_refuses_unrelated_macos_bundle_and_missing_install(self):
        setup = setup_module()
        with tempfile.TemporaryDirectory() as folder:
            prefix = Path(folder) / ".local"
            bundle = setup.app_bundle(prefix)
            bundle.mkdir(parents=True)
            with patch.object(setup.desktop, "platform_name", return_value="macos"):
                with self.assertRaises(dictation.DictationError):
                    setup.install_app_launcher(prefix)
            with self.assertRaises(dictation.DictationError):
                setup.launch(prefix)


if __name__ == "__main__":
    unittest.main()
