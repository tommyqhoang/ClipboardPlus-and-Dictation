from __future__ import annotations

import errno
import importlib.util
import json
import os
import plistlib
import shlex
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


HAS_DISPLAY = desktop.has_display  # The real one: tests patch it.


def setup_module():
    spec = importlib.util.spec_from_file_location("setup_desktop", ROOT / "setup-desktop.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.values = dictation.DEFAULTS.copy()
        # Launching needs a desktop session; tests run with and without one.
        display = patch.object(desktop, "has_display", return_value=True)
        display.start()
        self.addCleanup(display.stop)

    def test_has_display_needs_a_desktop_session_only_on_linux(self):
        for platform, environment, expected in (
            ("linux", {}, False),  # SSH or a container.
            ("linux", {"DISPLAY": ":0"}, True),
            ("linux", {"WAYLAND_DISPLAY": "wayland-0"}, True),
            ("macos", {}, True),
            ("windows", {}, True),
        ):
            with (
                patch.object(desktop, "platform_name", return_value=platform),
                patch.dict(os.environ, environment, clear=True),
            ):
                self.assertEqual(HAS_DISPLAY(), expected, (platform, environment))

    def test_install_without_a_desktop_session_does_not_fail_to_open_it(self):
        # Installing over SSH or in a container: the app starts at the next login.
        setup = setup_module()
        with tempfile.TemporaryDirectory() as folder:
            prefix = Path(folder)
            (prefix / setup.LIB).mkdir(parents=True)
            (prefix / setup.LIB / "app.py").touch()
            with (
                patch.object(
                    desktop, "platform_name", return_value="linux"
                ),  # A tray, not a bundle.
                patch.object(desktop, "has_display", return_value=False),
                patch.object(setup.subprocess, "Popen") as process,
                patch.object(setup, "stop_menubar") as stop,
            ):
                self.assertFalse(setup.launch(prefix))
            process.assert_not_called()
            stop.assert_called_once()  # An older copy still quits, so the upgrade applies.

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
            module = prefix / "lib/whisper-dictation/dictation.py"
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

    def test_normal_installer_opens_the_app_when_it_finishes(self):
        setup = setup_module()
        with tempfile.TemporaryDirectory() as folder:
            prefix = Path(folder) / ".local"
            with (
                patch.object(setup, "install", return_value=prefix / "bin/dictate-toggle"),
                patch.object(setup, "launch") as launch,
                patch.object(setup.telemetry, "set_component"),
                patch.object(setup.telemetry, "event"),
                patch.object(sys, "argv", ["setup-desktop.py", "--prefix", str(prefix)]),
            ):
                self.assertEqual(setup.main(), 0)
            launch.assert_called_once_with(prefix.resolve())

    def test_windows_start_menu_shortcut_is_renamed_and_the_old_one_removed(self):
        setup = setup_module()
        with patch.object(setup.subprocess, "run") as run:
            setup.windows_shortcut(Path("tray.py"), Path("C:/venv/pythonw.exe"))
        payload = json.loads(run.call_args.kwargs["input"])
        self.assertEqual(payload["name"], setup.hotkeys.APP_NAME + ".lnk")
        self.assertEqual(
            payload["old_names"],
            [
                "Clipboard+ and Dictation.lnk",
                "Clipboard+ Desktop.lnk",
                "Whisper Dictation & Clipboard+.lnk",
                "Whisper Dictation.lnk",
            ],
        )
        script = run.call_args.args[0][-1]
        self.assertIn("$p.name", script)
        self.assertIn("$p.old_names", script)

    def test_windows_shortcut_from_before_the_app_folder_is_still_ours(self):
        # Older installs pointed it at lib\tray.py; refusing it as "unrelated" failed
        # every reinstall and uninstall over one.
        setup = setup_module()
        prefix = Path(self.id()).resolve()
        with patch.object(setup.subprocess, "run") as run:
            setup.windows_shortcut(prefix / setup.LIB / "tray.py", Path("C:/venv/pythonw.exe"))
        legacy = json.loads(run.call_args.kwargs["input"])["legacy_arguments"]
        self.assertIn(setup.subprocess.list2cmdline([str(prefix / "lib/tray.py")]), legacy)
        self.assertIn(setup.subprocess.list2cmdline([str(prefix / setup.LIB / "app.py")]), legacy)
        self.assertNotIn(
            setup.subprocess.list2cmdline([str(prefix / setup.LIB / "tray.py")]), legacy
        )

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
            library = root / ".local/lib/whisper-dictation"
            for name in ("tray.py", "tray-recording.png", "whisper-dictation.png", "app.py"):
                self.assertTrue((library / name).is_file(), f"{name} was not installed")

    def test_install_moves_an_old_install_out_of_the_shared_lib_folder(self):
        setup = setup_module()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            shared = root / ".local/lib"
            shared.mkdir(parents=True)
            for name in ("dictation.py", "app.py", "tray-recording.png", "desktop.py"):
                (shared / name).write_text("old")
            (shared / "dictation.py").write_text("# whisper-dictation old installation")
            (shared / "desktop.py").write_text("# whisper-dictation old desktop")
            (shared / "someone-elses.py").write_text("keep me")
            with (
                patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(root / "run")}),
                patch.dict(os.environ, {"XDG_CONFIG_HOME": str(root / "config")}),
                patch.object(setup.desktop, "platform_name", return_value="linux"),
                patch.object(setup, "install_app_launcher"),
            ):
                setup.install(root / ".local")
            self.assertEqual(
                {path.name for path in shared.glob("*.py")}, {"app.py", "someone-elses.py"}
            )
            self.assertTrue((shared / "tray-recording.png").exists())
            self.assertTrue((shared / "whisper-dictation/dictation.py").is_file())

    def test_shared_legacy_names_are_kept_when_they_are_not_ours(self):
        setup = setup_module()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            prefix = root / ".local"
            shared = prefix / "lib"
            shared.mkdir(parents=True)
            for name in ("dictation.py", "desktop.py", "app.py"):
                (shared / name).write_text("someone else's module")
            with (
                patch.dict(
                    os.environ,
                    {"XDG_RUNTIME_DIR": str(root / "run"), "XDG_CONFIG_HOME": str(root / "config")},
                ),
                patch.object(setup.desktop, "platform_name", return_value="linux"),
                patch.object(setup, "install_app_launcher"),
            ):
                setup.install(prefix)
                setup.uninstall(prefix)
            self.assertEqual((shared / "app.py").read_text(), "someone else's module")
            self.assertEqual((shared / "dictation.py").read_text(), "someone else's module")

    def test_non_utf8_legacy_module_is_not_treated_as_ours(self):
        setup = setup_module()
        with tempfile.TemporaryDirectory() as folder:
            shared = Path(folder)
            (shared / "dictation.py").write_bytes(b"\xff\xfe")
            (shared / "desktop.py").write_text("other app", encoding="utf-8")
            self.assertFalse(setup.legacy_owned(shared))

    def test_failed_launcher_registration_does_not_write_receipt(self):
        setup = setup_module()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            prefix = root / ".local"
            with (
                patch.dict(
                    os.environ,
                    {"XDG_RUNTIME_DIR": str(root / "run"), "XDG_CONFIG_HOME": str(root / "config")},
                ),
                patch.object(setup.desktop, "platform_name", return_value="linux"),
                patch.object(setup, "install_app_launcher", side_effect=OSError("launcher failed")),
                self.assertRaisesRegex(OSError, "launcher failed"),
            ):
                setup.install(prefix)
            self.assertFalse((prefix / ".dictation-install.json").exists())

    def test_failed_gui_repair_keeps_existing_modules_during_update(self):
        setup = setup_module()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            prefix = root / ".local"
            existing = prefix / setup.LIB / "dictation.py"
            existing.parent.mkdir(parents=True)
            existing.write_text("old working module", encoding="utf-8")
            receipt = prefix / ".dictation-install.json"
            receipt.write_text('{"shortcut": true}', encoding="utf-8")
            with (
                patch.dict(
                    os.environ,
                    {"XDG_RUNTIME_DIR": str(root / "run"), "XDG_CONFIG_HOME": str(root / "config")},
                ),
                patch.object(
                    setup,
                    "gui_environment",
                    side_effect=dictation.DictationError("dependency failed"),
                ),
                patch.object(setup, "install_app_launcher") as launcher,
                self.assertRaisesRegex(dictation.DictationError, "dependency failed"),
            ):
                setup.install(prefix)
            self.assertEqual(existing.read_text(encoding="utf-8"), "old working module")
            self.assertEqual(receipt.read_text(encoding="utf-8"), '{"shortcut": true}')
            launcher.assert_not_called()

    def test_notifications_carry_the_product_name(self):
        import hotkeys

        self.assertEqual(desktop.NOTIFY_NAME, hotkeys.APP_NAME)
        with patch.object(desktop, "platform_name", return_value="linux"):
            command = desktop.notification_command({"notify": ""})
        self.assertEqual(command[:3], ["notify-send", "-a", hotkeys.APP_NAME])
        self.assertIn(f"string:desktop-entry:{desktop.DESKTOP_ENTRY_ID}", command)

    def test_application_launchers_and_launch(self):
        setup = setup_module()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            prefix = root / ".local"
            (prefix / "lib/whisper-dictation").mkdir(parents=True)
            (prefix / "lib/whisper-dictation/app.py").write_text("# app")
            venv = PurePosixPath("/venv/bin/python")
            with (
                patch.object(setup.desktop, "platform_name", return_value="linux"),
                patch.object(setup, "gui_environment", return_value=venv),
                patch.object(setup.hotkeys, "set_login_item") as login,
                patch.object(setup, "stop_menubar") as stop,
            ):
                setup.install_app_launcher(prefix)
                login.assert_called_once_with(
                    True, [str(venv), str(prefix / "lib/whisper-dictation/tray.py")]
                )
                entry = (
                    prefix / f"share/applications/{desktop.DESKTOP_ENTRY_ID}.desktop"
                ).read_text()
                self.assertIn(f"Name={setup.hotkeys.APP_NAME}\n", entry)
                self.assertIn('Exec="/venv/bin/python"', entry)
                self.assertIn("tray.py", entry)
                self.assertIn("Terminal=false", entry)
                self.assertIn("X-GNOME-UsesNotifications=true", entry)
                self.assertEqual(desktop.DESKTOP_ENTRY_ID, "clipboardplus")
                self.assertEqual(desktop.FORMER_DESKTOP_ENTRY_IDS, ("whisper-dictation",))
                self.assertIn(
                    f"Icon={prefix / 'lib/whisper-dictation/whisper-dictation.png'}", entry
                )
                with patch.object(setup.subprocess, "Popen") as process:
                    # Still running when the check ends: it started.
                    running = setup.subprocess.TimeoutExpired("app", 3)
                    process.return_value.wait.side_effect = running
                    setup.launch(prefix)  # No private environment yet: the window.
                    self.assertIn(
                        str(prefix / "lib/whisper-dictation/app.py"), process.call_args.args[0]
                    )
                    windowed = setup.gui_python(prefix)[1]
                    windowed.parent.mkdir(parents=True)
                    windowed.touch()
                    setup.launch(prefix)
                    self.assertEqual(
                        process.call_args.args[0],
                        [str(windowed), str(prefix / "lib/whisper-dictation/tray.py")],
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
                bundle = root / "Applications/Clipboard+.app"
                # The executable itself, so launchd supervises it (KeepAlive).
                login.assert_called_once_with(
                    True, [str(bundle / "Contents/MacOS/WhisperDictation")]
                )
                info = plistlib.loads((bundle / "Contents/Info.plist").read_bytes())
                self.assertEqual(info["CFBundleIdentifier"], setup.hotkeys.BUNDLE_ID)
                self.assertIn("Record speech only", info["NSMicrophoneUsageDescription"])
                self.assertEqual(info["CFBundleIconFile"], "AppIcon")
                self.assertEqual(info["CFBundleDisplayName"], setup.hotkeys.APP_NAME)
                self.assertEqual(info["CFBundleName"], setup.hotkeys.APP_NAME)
                self.assertTrue((bundle / "Contents/Resources/AppIcon.icns").is_file())
                self.assertTrue(info["LSUIElement"])
                executable = bundle / "Contents/MacOS/WhisperDictation"
                self.assertIn("/venv/bin/python", executable.read_text())
                self.assertIn("menubar.py", executable.read_text())
                self.assertIn(
                    f"WHISPER_DICTATION_BUNDLE={shlex.quote(str(bundle))}\n", executable.read_text()
                )
                # argv[0] is renamed so Activity Monitor/Login Items show the app's name,
                # not the interpreter's.
                self.assertIn(f"exec -a {setup.hotkeys.APP_NAME}", executable.read_text())
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

    def test_macos_upgrade_renames_the_previous_bundle(self):
        setup = setup_module()
        for name, lib, moved in (
            ("Clipboard+ Desktop.app", "lib", False),
            ("Clipboard+ and Dictation.app", setup.LIB, False),
            # Left in ~/Applications by an earlier install, while /Applications is used now.
            ("Clipboard+ and Dictation.app", setup.LIB, True),
        ):
            with tempfile.TemporaryDirectory() as folder:
                prefix = Path(folder) / ".local"
                applications = Path(folder) / ("system/Applications" if moved else "Applications")
                legacy = prefix.parent / "Applications" / name
                executable = legacy / "Contents/MacOS/WhisperDictation"
                executable.parent.mkdir(parents=True)
                executable.write_text(f"exec python {prefix / lib / 'menubar.py'}")
                (legacy / "Contents/Info.plist").write_bytes(
                    plistlib.dumps({"CFBundleIdentifier": setup.hotkeys.FORMER_BUNDLE_IDS[0]})
                )
                with (
                    patch.object(setup.desktop, "platform_name", return_value="macos"),
                    patch.object(setup, "gui_environment", return_value=Path("/venv/bin/python")),
                    patch.object(setup.hotkeys, "set_login_item"),
                    patch.object(setup, "applications_root", return_value=applications),
                ):
                    setup.install_app_launcher(prefix)
                self.assertFalse(legacy.exists(), name)
                new_info = applications / "Clipboard+.app/Contents/Info.plist"
                self.assertTrue(new_info.is_file())
                # The old bundle id is upgraded along with everything else, not just moved.
                self.assertEqual(
                    plistlib.loads(new_info.read_bytes())["CFBundleIdentifier"],
                    setup.hotkeys.BUNDLE_ID,
                )

    def test_macos_upgrade_leaves_a_bundle_of_another_installation_alone(self):
        setup = setup_module()
        with tempfile.TemporaryDirectory() as folder:
            prefix = Path(folder) / ".local"
            legacy = prefix.parent / "Applications/Clipboard+ and Dictation.app"
            executable = legacy / "Contents/MacOS/WhisperDictation"
            executable.parent.mkdir(parents=True)
            executable.write_text("exec python /somewhere/else/lib/menubar.py")
            (legacy / "Contents/Info.plist").write_bytes(
                plistlib.dumps({"CFBundleIdentifier": setup.hotkeys.FORMER_BUNDLE_IDS[0]})
            )
            with (
                patch.object(setup.desktop, "platform_name", return_value="macos"),
                patch.object(setup, "gui_environment", return_value=Path("/venv/bin/python")),
                patch.object(setup.hotkeys, "set_login_item"),
            ):
                setup.install_app_launcher(prefix)
            self.assertTrue(legacy.exists())

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

    def test_failed_environment_repair_preserves_the_previous_venv(self):
        setup = setup_module()
        with tempfile.TemporaryDirectory() as folder:
            prefix = Path(folder)
            venv = prefix / "share/whisper-dictation/venv"
            (venv / "bin").mkdir(parents=True)
            (venv / "bin/python").write_text("existing interpreter")
            (venv / "pyvenv.cfg").write_text("existing settings")
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
                self.assertRaises(dictation.DictationError),
            ):
                setup.gui_environment(prefix)
            self.assertEqual((venv / "bin/python").read_text(), "existing interpreter")
            self.assertEqual((venv / "pyvenv.cfg").read_text(), "existing settings")

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

    def test_macos_uninstall_unloads_the_login_item_that_runs_our_executable(self):
        setup = setup_module()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        prefix = Path(folder.name) / ".local"
        prefix.mkdir()
        agent = Path(folder.name) / "agent.plist"
        bundle = setup.app_bundle(prefix)
        for program, removed in (
            ([str(bundle / "Contents/MacOS/WhisperDictation")], True),  # Current versions.
            (["/usr/bin/open", str(bundle)], True),  # Older versions.
            (["/usr/bin/open", str(bundle) + " copy.app"], False),  # Someone else's.
        ):
            (prefix / ".dictation-install.json").write_text("{}")
            agent.write_bytes(plistlib.dumps({"ProgramArguments": program}))
            with (
                patch.object(setup.desktop, "platform_name", return_value="macos"),
                patch.object(setup.dictation, "busy", return_value=False),
                patch.object(setup, "stop_clipboard_service"),
                patch.object(setup, "stop_menubar"),
                patch.object(setup.hotkeys, "agent_path", return_value=agent),
                patch.object(setup.hotkeys, "set_login_item") as login,
            ):
                setup.uninstall(prefix)
            self.assertEqual(login.called, removed, program)
            if removed:
                self.assertFalse(login.call_args.args[0])

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


class RelaunchTests(unittest.TestCase):
    def test_frozen_root_is_none_from_source(self):
        with patch.object(sys, "frozen", False, create=True):
            self.assertIsNone(desktop.frozen_root())

    def test_frozen_root_is_the_executables_own_directory(self):
        with (
            patch.object(sys, "frozen", True, create=True),
            patch.object(sys, "executable", "/opt/Clipboard+/tray"),
            patch.dict(os.environ, {}, clear=True),
        ):
            self.assertEqual(desktop.frozen_root(), Path("/opt/Clipboard+"))

    def test_frozen_root_prefers_appdir_inside_an_appimage(self):
        with (
            patch.object(sys, "frozen", True, create=True),
            patch.dict(os.environ, {"APPDIR": "/tmp/.mount_ClipboardAbc123"}),
        ):
            self.assertEqual(desktop.frozen_root(), Path("/tmp/.mount_ClipboardAbc123/usr/bin"))

    def test_relaunch_from_source_uses_overlay_python_and_the_lib_dir(self):
        with patch.object(sys, "frozen", False, create=True):
            command = desktop.relaunch("dictation", "--worker", "abc")
        lib = Path(desktop.__file__).resolve().parent
        self.assertEqual(command[1:], [str(lib / "dictation.py"), "--worker", "abc"])
        self.assertTrue(Path(command[0]).name.startswith("python"))

    def test_relaunch_when_frozen_uses_a_sibling_binary_with_no_extension_on_linux(self):
        with (
            patch.object(sys, "frozen", True, create=True),
            patch.object(sys, "executable", "/opt/Clipboard+/tray"),
            patch.object(desktop, "platform_name", return_value="linux"),
            patch.dict(os.environ, {}, clear=True),
        ):
            self.assertEqual(
                desktop.relaunch("dictation", "--worker", "abc"),
                ["/opt/Clipboard+/dictation", "--worker", "abc"],
            )

    def test_relaunch_when_frozen_appends_exe_on_windows(self):
        with (
            patch.object(sys, "frozen", True, create=True),
            patch.object(
                sys, "executable", r"C:\Users\a\AppData\Local\Programs\Clipboard+\tray.exe"
            ),
            patch.object(desktop, "platform_name", return_value="windows"),
            patch.dict(os.environ, {}, clear=True),
        ):
            command = desktop.relaunch("dictation")
            self.assertEqual(Path(command[0]).name, "dictation.exe")

    def test_python_for_gui_moved_here_still_works(self):
        self.assertTrue(Path(desktop.python_for_gui()).name.startswith("python"))

    def test_overlay_python_prefers_the_installed_private_venv(self):
        with tempfile.TemporaryDirectory() as folder:
            prefix = Path(folder)
            (prefix / "lib").mkdir()
            module = prefix / "lib/desktop.py"
            with patch.object(desktop, "__file__", str(module)):
                self.assertEqual(desktop.overlay_python(), desktop.python_for_gui())
                private = prefix / "share/whisper-dictation/venv/bin/python"
                private.parent.mkdir(parents=True)
                private.touch()
                with patch.object(desktop, "platform_name", return_value="linux"):
                    self.assertEqual(desktop.overlay_python(), str(private.resolve()))


class BundledBinaryTests(unittest.TestCase):
    def test_returns_none_from_source(self):
        with patch.object(sys, "frozen", False, create=True):
            self.assertIsNone(desktop.bundled_binary("ffmpeg"))

    def test_finds_a_sibling_binary_when_frozen(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "ffmpeg").touch()
            with (
                patch.object(sys, "frozen", True, create=True),
                patch.object(desktop, "frozen_root", return_value=root),
                patch.object(desktop, "platform_name", return_value="linux"),
            ):
                self.assertEqual(desktop.bundled_binary("ffmpeg"), root / "ffmpeg")

    def test_returns_none_when_frozen_but_the_binary_is_missing(self):
        with tempfile.TemporaryDirectory() as folder:
            with (
                patch.object(sys, "frozen", True, create=True),
                patch.object(desktop, "frozen_root", return_value=Path(folder)),
            ):
                self.assertIsNone(desktop.bundled_binary("ffmpeg"))


if __name__ == "__main__":
    unittest.main()
