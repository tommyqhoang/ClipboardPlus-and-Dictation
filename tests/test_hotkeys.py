from __future__ import annotations

import json
import os
import plistlib
import sys
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from unittest.mock import MagicMock, Mock, call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import dictation as d
import hotkeys


class HotkeyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        environment = patch.dict(
            os.environ,
            {
                "XDG_CONFIG_HOME": str(self.folder / "config"),
                "XDG_RUNTIME_DIR": str(self.folder / "runtime"),
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.preferences = hotkeys.Preferences(d.Paths())

    def test_same_shortcut_on_every_platform(self):
        shortcut = hotkeys.default_shortcut("linux")
        self.assertEqual(shortcut.label("macos"), "⌃⇧D")
        self.assertEqual(shortcut.label("windows"), "Ctrl+Shift+D")
        self.assertEqual(shortcut.label("linux"), "Ctrl+Shift+D")
        self.assertEqual(hotkeys.Shortcut(("cmd",), "F5").label("windows"), "Win+F5")
        self.assertEqual(hotkeys.Shortcut(("cmd",), "F5").label("linux"), "Super+F5")
        self.assertEqual(shortcut.carbon_modifiers(), 0x200 | 0x1000)
        self.assertEqual(shortcut.mac_key_code(), 2)
        self.assertEqual(shortcut.windows_key(), ord("D"))
        self.assertEqual(shortcut.windows_modifiers(), 0x4 | 0x2 | hotkeys.MOD_NOREPEAT)
        self.assertEqual(shortcut.gnome(), "<Control><Shift>d")
        space = hotkeys.Shortcut(("alt",), "Space")
        self.assertEqual((space.windows_key(), space.gnome()), (0x20, "<Alt>space"))
        f12 = hotkeys.Shortcut((), "F12")
        self.assertEqual((f12.windows_key(), f12.gnome(), f12.mac_key_code()), (0x7B, "F12", 111))
        self.assertEqual(len(hotkeys.MAC_KEY_CODES), len(hotkeys.KEYS))
        self.assertEqual(len(set(hotkeys.PRESETS)), len(hotkeys.PRESETS))
        for preset in hotkeys.PRESETS:
            self.assertEqual(preset.problem(), "")

    def test_rejects_shortcuts_that_capture_typing(self):
        self.assertIn("Include", hotkeys.Shortcut((), "D").problem())
        self.assertIn("Include", hotkeys.Shortcut(("shift",), "D").problem())
        self.assertIn("Most apps", hotkeys.Shortcut(("cmd",), "V").problem())
        self.assertIn("Most apps", hotkeys.Shortcut(("ctrl",), "C").problem())
        self.assertIn("letter", hotkeys.Shortcut(("ctrl",), "Home").problem())
        self.assertEqual(hotkeys.Shortcut((), "F5").problem(), "")
        self.assertEqual(hotkeys.Shortcut(("cmd",), "D").problem(), "")

    def test_captured_keys(self):
        flags = hotkeys.EVENT_FLAGS["shift"] | hotkeys.EVENT_FLAGS["ctrl"]
        self.assertEqual(hotkeys.from_mac_event(2, flags, "d"), hotkeys.default_shortcut("linux"))
        self.assertEqual(hotkeys.from_mac_event(49, flags, " ").key, "Space")
        # Every modifier survives, whatever order the event flags are read in.
        every = sum(hotkeys.EVENT_FLAGS.values())
        self.assertEqual(hotkeys.from_mac_event(2, every, "d").modifiers, hotkeys.MODIFIER_ORDER)
        self.assertEqual(hotkeys.from_mac_event(200, 0, "é").key, "É")
        self.assertIsNone(hotkeys.from_tk("Control_L", set()))
        self.assertEqual(hotkeys.from_tk("d", {"ctrl", "shift"}), hotkeys.default_shortcut("linux"))
        self.assertEqual(hotkeys.from_tk("space", {"alt"}), hotkeys.Shortcut(("alt",), "Space"))
        self.assertEqual(hotkeys.from_tk("F9", set()).key, "F9")

    def test_preferences_round_trip_and_fallbacks(self):
        self.assertEqual(self.preferences.shortcut(), hotkeys.DEFAULT)
        self.assertEqual(self.preferences.stamp(), (0, 0))
        self.assertTrue(self.preferences.open_at_login())
        custom = hotkeys.Shortcut(("alt",), "Space")
        self.preferences.save(shortcut=custom)
        self.preferences.save(open_at_login=False)
        self.assertEqual(self.preferences.shortcut(), custom)
        self.assertFalse(self.preferences.open_at_login())
        self.assertGreater(self.preferences.stamp(), (0, 0))
        for broken in (
            '{"shortcut": "nope"}',
            '{"shortcut": {"modifiers": "ctrl", "key": "D"}}',
            '{"shortcut": {"modifiers": [], "key": "D"}}',
            "not json",
        ):
            self.preferences.path.write_text(broken)
            self.assertEqual(self.preferences.shortcut(), hotkeys.DEFAULT)
            self.assertTrue(self.preferences.open_at_login())
        # Files written by the first macOS version also carried key_code.
        self.preferences.path.write_text(
            '{"shortcut": {"key_code": 49, "modifiers": ["alt"], "key": "Space"}}'
        )
        self.assertEqual(self.preferences.shortcut(), custom)

    def test_the_agent_is_registered_with_launchd_right_away(self):
        calls = []

        def run(args, **_):
            calls.append(args)
            return Mock(returncode=0, stdout="", stderr="")

        command = ["/usr/bin/open", str(self.folder / "Clipboard+ and Dictation.app")]
        with patch.object(hotkeys.shutil, "which", return_value="/usr/bin/launchctl"):
            with patch.object(hotkeys, "_menubar_is_idle", return_value=True):
                hotkeys.set_login_item(True, command, self.folder, "macos", run=run)
                self.assertEqual(calls[-1][:2], ["launchctl", "bootstrap"])
                agent = plistlib.loads(hotkeys.agent_path(self.folder).read_bytes())
                # Comes back after a crash or force quit; a clean Quit stays quit.
                self.assertEqual(agent["KeepAlive"], {"SuccessfulExit": False})
                self.assertEqual(agent["AssociatedBundleIdentifiers"], [hotkeys.BUNDLE_ID])
                hotkeys.set_login_item(False, command, self.folder, "macos", run=run)
                self.assertEqual(calls[-1][:2], ["launchctl", "bootout"])
                self.assertFalse(hotkeys.agent_path(self.folder).exists())

    def test_packaged_login_item_is_associated_with_its_own_bundle(self):
        contents = self.folder / "Clipboard+.app/Contents"
        contents.mkdir(parents=True)
        (contents / "Info.plist").write_bytes(
            plistlib.dumps(
                {
                    "CFBundleIdentifier": "com.apercallc.clipboardplus",
                }
            )
        )
        self.assertEqual(
            hotkeys.login_bundle_id([str(contents / "MacOS/menubar")]),
            "com.apercallc.clipboardplus",
        )
        (contents / "Info.plist").write_text("broken")
        self.assertEqual(
            hotkeys.login_bundle_id([str(contents / "MacOS/menubar")]), hotkeys.BUNDLE_ID
        )
        self.assertEqual(hotkeys.login_bundle_id([]), hotkeys.BUNDLE_ID)

    def test_a_changed_agent_replaces_the_one_launchd_already_loaded(self):
        calls = []

        def run(args, **_):
            calls.append(args[:2])
            return Mock(returncode=0, stdout="", stderr="")

        bundle = str(self.folder / "Clipboard+ and Dictation.app")
        executable = [bundle + "/Contents/MacOS/WhisperDictation"]
        with patch.object(hotkeys, "_menubar_is_idle", return_value=False):  # App running.
            hotkeys.set_login_item(True, ["/usr/bin/open", bundle], self.folder, "macos", run=run)
            calls.clear()
            # The old `open` agent never owned the app, so unloading it is safe.
            hotkeys.set_login_item(True, executable, self.folder, "macos", run=run)
            self.assertEqual(calls, [["launchctl", "bootout"], ["launchctl", "bootstrap"]])
            calls.clear()
            # Unchanged: nothing to unload (it would quit the running app).
            hotkeys.set_login_item(True, executable, self.folder, "macos", run=run)
            self.assertEqual(calls, [["launchctl", "bootstrap"]])

    def test_start_login_item_starts_the_app_under_launchd(self):
        calls = []
        codes = iter([0, 5, 5, 0])  # bootout, then bootstrap busy twice while it unloads.

        def run(args, **_):
            calls.append(args[1])
            return Mock(returncode=next(codes))

        with patch.object(hotkeys, "agent_path", return_value=self.folder / "agent.plist"):
            self.assertFalse(hotkeys.start_login_item(run=run))  # No login item: `open`.
            (self.folder / "agent.plist").write_text("x")
            self.assertTrue(hotkeys.start_login_item(run=run, sleep=Mock()))
            self.assertEqual(calls, ["bootout", "bootstrap", "bootstrap", "bootstrap"])
            always_busy = Mock(return_value=Mock(returncode=5))
            self.assertFalse(hotkeys.start_login_item(run=always_busy, sleep=Mock()))
            broken = Mock(side_effect=OSError)
            self.assertFalse(hotkeys.start_login_item(run=broken, sleep=Mock()))

    def test_menubar_is_idle_only_when_its_lock_is_free(self):
        with patch.object(hotkeys.desktop, "lock", return_value=None):
            self.assertFalse(hotkeys._menubar_is_idle())  # Held: running.
        with patch.object(hotkeys.desktop, "lock", side_effect=OSError):
            self.assertFalse(hotkeys._menubar_is_idle())
        with (
            patch.object(hotkeys.desktop, "lock", return_value=7),
            patch.object(hotkeys.os, "close") as close,
        ):
            self.assertTrue(hotkeys._menubar_is_idle())
        close.assert_called_once_with(7)

    def test_a_former_labeled_agent_is_retired_only_while_idle(self):
        former = self.folder / "Library/LaunchAgents/org.whisperdictation.menubar.plist"
        former.parent.mkdir(parents=True)
        former.write_text("x")
        calls = []

        def run(args, **_):
            calls.append(args[:2])
            return Mock(returncode=0, stdout="", stderr="")

        with patch.object(hotkeys, "_menubar_is_idle", return_value=False):
            hotkeys._migrate_former_agents(self.folder, run)
        self.assertTrue(former.exists())  # Running under the old label: left alone.
        self.assertEqual(calls, [])
        with patch.object(hotkeys, "_menubar_is_idle", return_value=True):
            hotkeys._migrate_former_agents(self.folder, run)
        self.assertFalse(former.exists())
        self.assertEqual(calls, [["launchctl", "bootout"]])

    def test_bundle_login_command_runs_the_executable_so_launchd_supervises_it(self):
        bundle = self.folder / "Clipboard+ and Dictation.app"
        # No executable yet (e.g. a half-written bundle): fall back to `open`.
        self.assertEqual(hotkeys.bundle_login_command(str(bundle)), ["/usr/bin/open", str(bundle)])
        executable = bundle / "Contents/MacOS/WhisperDictation"
        executable.parent.mkdir(parents=True)
        executable.write_text("#!/bin/sh\n")
        self.assertEqual(hotkeys.bundle_login_command(str(bundle)), [str(executable)])

    def test_bundle_login_command_reads_the_executable_name_from_info_plist(self):
        # A packaged build's Info.plist names its own executable ("menubar", not
        # the source-install wrapper's "WhisperDictation") — read it, don't guess.
        bundle = self.folder / "Clipboard+.app"
        macos_dir = bundle / "Contents/MacOS"
        macos_dir.mkdir(parents=True)
        (bundle / "Contents/Info.plist").write_bytes(
            plistlib.dumps({"CFBundleExecutable": "menubar"})
        )
        executable = macos_dir / "menubar"
        executable.write_text("#!/bin/sh\n")
        self.assertEqual(hotkeys.bundle_login_command(str(bundle)), [str(executable)])

    def test_login_items_per_platform(self):
        command = ["/usr/bin/open", str(self.folder / "Whisper Dictation.app")]
        hotkeys.set_login_item(True, command, self.folder, "macos", run=Mock())
        agent = plistlib.loads(hotkeys.agent_path(self.folder).read_bytes())
        self.assertEqual(agent["Label"], hotkeys.AGENT_LABEL)
        self.assertEqual(agent["ProgramArguments"], command)
        self.assertTrue(agent["RunAtLoad"])
        hotkeys.set_login_item(False, command, self.folder, "macos", run=Mock())
        self.assertFalse(hotkeys.agent_path(self.folder).exists())
        tray = ["/venv/bin/python", "/app with space/tray.py"]
        hotkeys.set_login_item(True, tray, self.folder, "linux")
        entry = hotkeys.autostart_path(self.folder).read_text()
        self.assertIn("Exec=/venv/bin/python '/app with space/tray.py'", entry)
        hotkeys.set_login_item(False, tray, self.folder, "linux")
        self.assertFalse(hotkeys.autostart_path(self.folder).exists())
        registry = MagicMock()
        key = registry.CreateKey.return_value.__enter__.return_value
        hotkeys.set_login_item(
            True, ["C:/py/pythonw.exe", "C:/a b/tray.py"], None, "windows", registry
        )
        registry.SetValueEx.assert_called_once_with(
            key, hotkeys.RUN_VALUE_NAME, 0, registry.REG_SZ, 'C:/py/pythonw.exe "C:/a b/tray.py"'
        )
        # A former release's value name is always cleaned up alongside the new one.
        self.assertIn(call(key, "WhisperDictation"), registry.DeleteValue.call_args_list)
        registry.DeleteValue.reset_mock(side_effect=True)
        registry.DeleteValue.side_effect = FileNotFoundError
        hotkeys.set_login_item(False, [], None, "windows", registry)
        registry.DeleteValue.assert_any_call(key, hotkeys.RUN_VALUE_NAME)
        registry.DeleteValue.assert_any_call(key, "WhisperDictation")

    def test_a_former_named_autostart_entry_is_removed_when_the_new_one_is_written(self):
        for former in hotkeys.former_autostart_paths(self.folder):
            former.parent.mkdir(parents=True, exist_ok=True)
            former.write_text("x")
        hotkeys.set_login_item(True, ["/x/python", "/x/tray.py"], self.folder, "linux")
        self.assertTrue(hotkeys.autostart_path(self.folder).exists())
        for former in hotkeys.former_autostart_paths(self.folder):
            self.assertFalse(former.exists())

    def test_gnome_shortcut_binds_command(self):
        calls = []

        def run(args, **_):
            calls.append(args[1:])
            output = "['/other/']" if args[1] == "get" else ""
            return Mock(returncode=0, stdout=output, stderr="")

        linux_default = hotkeys.default_shortcut("linux")
        with patch.object(hotkeys.shutil, "which", return_value="/usr/bin/gsettings"):
            self.assertTrue(
                hotkeys.gnome_shortcut(linux_default, PurePosixPath("/bin/toggle"), run)
            )
            self.assertIn(
                ["set", *hotkeys.GNOME_LIST, f"['/other/', {hotkeys.GNOME_PATH!r}]"], calls
            )
            self.assertEqual(calls[-1][-2:], ["binding", "<Control><Shift>d"])
            failing = Mock(return_value=Mock(returncode=1, stdout="", stderr="no schema"))
            self.assertFalse(hotkeys.gnome_shortcut(linux_default, Path("/t"), failing))
        with patch.object(hotkeys.shutil, "which", return_value=None):
            self.assertFalse(hotkeys.gnome_shortcut(linux_default, Path("/t")))

    def gsettings(self, custom, built_in=""):
        """A fake gsettings: custom shortcuts {path: (name, binding)} and built-in listings."""

        def run(args, **_):
            command = args[1:]
            if command[0] == "get" and command[1:] == list(hotkeys.GNOME_LIST):
                return Mock(returncode=0, stdout=str(list(custom)))
            if command[0] == "get":
                path = command[1].split(":", 1)[1]
                name, binding = custom[path]
                return Mock(returncode=0, stdout=repr(binding if command[2] == "binding" else name))
            if command[0] == "list-recursively":
                return Mock(
                    returncode=0, stdout=built_in if command[1].endswith("wm.keybindings") else ""
                )
            return Mock(returncode=0, stdout="")

        return run

    def test_another_shortcut_on_the_same_keys_is_found(self):
        old = hotkeys.GNOME_LIST_PREFIX + "custom0/"
        custom = {
            hotkeys.GNOME_PATH: ("Clipboard+ and Dictation", "<Control><Shift>d"),
            old: ("Whisper Dictation", "<Shift><Control>D"),  # Other order and case: same keys.
        }
        built_in = (
            "org.gnome.desktop.wm.keybindings activate-window-menu ['<Alt>space']\n"
            "org.gnome.desktop.wm.keybindings close ['<Primary>q', '<Alt>F4']"
        )
        run = self.gsettings(custom, built_in)
        linux_default = hotkeys.default_shortcut("linux")
        with patch.object(hotkeys.shutil, "which", return_value="/usr/bin/gsettings"):
            found = hotkeys.gnome_conflict(linux_default, hotkeys.GNOME_PATH, run)
            self.assertEqual(found, hotkeys.Conflict("Whisper Dictation", old))
            window_menu = hotkeys.gnome_conflict(
                hotkeys.Shortcut(("alt",), "Space"), hotkeys.GNOME_PATH, run
            )
            self.assertIn("activate window menu", window_menu.name)
            self.assertEqual(window_menu.path, "")
            quit_key = hotkeys.gnome_conflict(
                hotkeys.Shortcut(("ctrl",), "Q"), hotkeys.GNOME_PATH, run
            )
            self.assertIn("close", quit_key.name)  # <Primary> is Ctrl.
            free = hotkeys.Shortcut(("ctrl", "alt"), "Space")
            self.assertIsNone(hotkeys.gnome_conflict(free, hotkeys.GNOME_PATH, run))
        with patch.object(hotkeys.shutil, "which", return_value=None):
            self.assertIsNone(hotkeys.gnome_conflict(linux_default, hotkeys.GNOME_PATH, run))

    def test_a_conflict_is_shared_with_the_window_and_can_be_released(self):
        paths = d.Paths()
        conflict = hotkeys.Conflict("Whisper Dictation", hotkeys.GNOME_LIST_PREFIX + "custom0/")
        hotkeys.record_status(paths, True, conflict=conflict)
        self.assertFalse(hotkeys.shortcut_working(paths))
        self.assertEqual(hotkeys.shortcut_conflict(paths), conflict)
        hotkeys.record_status(paths, True)
        self.assertTrue(hotkeys.shortcut_working(paths))
        self.assertIsNone(hotkeys.shortcut_conflict(paths))
        run = Mock(return_value=Mock(returncode=0))
        with patch.object(hotkeys.shutil, "which", return_value="/usr/bin/gsettings"):
            self.assertTrue(hotkeys.gnome_release(conflict.path, run))
            self.assertEqual(run.call_args.args[0][-2:], ["binding", ""])
            self.assertFalse(hotkeys.gnome_release("/org/gnome/desktop/other/", run))
        self.assertEqual(run.call_count, 1)  # Only custom shortcuts are ever touched.

    def test_alt_space_is_offered_only_where_the_desktop_leaves_it_free(self):
        alt_space = hotkeys.Shortcut(("alt",), "Space")
        expected = hotkeys.desktop.platform_name() == "macos"
        self.assertEqual(alt_space in hotkeys.PRESETS, expected)

    def test_the_history_shortcut_has_its_own_gnome_entry_and_command(self):
        calls = []

        def run(args, **_):
            calls.append(args[1:])
            output = f"[{hotkeys.GNOME_PATH!r}]" if args[1] == "get" else ""
            return Mock(returncode=0, stdout=output, stderr="")

        command = hotkeys.history_command(PurePosixPath("/lib"), "/venv/python")
        self.assertEqual(command, ["/venv/python", "/lib/app.py", "--clipboard"])
        linux_history = hotkeys.default_history_shortcut("linux")
        with patch.object(hotkeys.shutil, "which", return_value="/usr/bin/gsettings"):
            self.assertTrue(
                hotkeys.gnome_shortcut(linux_history, command, run, path=hotkeys.GNOME_HISTORY_PATH)
            )
        listed = f"[{hotkeys.GNOME_PATH!r}, {hotkeys.GNOME_HISTORY_PATH!r}]"
        self.assertIn(["set", *hotkeys.GNOME_LIST, listed], calls)
        self.assertIn("/venv/python /lib/app.py --clipboard", calls[-3][-1])
        # Cleared, then set: GNOME grabs the keys again even if the value is unchanged.
        self.assertEqual([call[-1] for call in calls[-2:]], ["", "<Control><Shift>f"])

    def test_history_command_default_matches_relaunch_when_frozen(self):
        with (
            patch.object(hotkeys.desktop, "frozen_root", return_value=Path("/opt/Clipboard+")),
            patch.object(hotkeys.desktop, "platform_name", return_value="linux"),
        ):
            self.assertEqual(
                hotkeys.history_command(PurePosixPath("/lib")),
                [str(Path("/opt/Clipboard+") / "app"), "--clipboard"],
            )

    def test_history_command_default_uses_appimage_not_the_ephemeral_mount(self):
        # A GNOME custom keybinding is persisted to disk; the AppImage mount that
        # frozen_root() would otherwise point at disappears once every running
        # instance exits, so this must go through the stable $APPIMAGE path.
        with (
            patch.object(hotkeys.desktop, "frozen_root", return_value=Path("/tmp/.mount_Abc123")),
            patch.dict(os.environ, {"APPIMAGE": "/home/user/Clipboard+.AppImage"}),
        ):
            self.assertEqual(
                hotkeys.history_command(PurePosixPath("/lib")),
                ["/home/user/Clipboard+.AppImage", "app", "--clipboard"],
            )

    def test_history_command_default_ignores_frozen_state_when_python_is_given(self):
        # setup-desktop.py:636 passes an explicit python for a *different* install
        # prefix than the one currently running; it must never be redirected to
        # this process's own frozen sibling binary.
        with patch.object(hotkeys.desktop, "frozen_root", return_value=Path("/opt/Clipboard+")):
            self.assertEqual(
                hotkeys.history_command(PurePosixPath("/lib"), "/other/prefix/venv/python"),
                ["/other/prefix/venv/python", "/lib/app.py", "--clipboard"],
            )

    def test_gnome_shortcut_pauses_and_removes_only_its_own_binding(self):
        calls = []
        state = {"command": "'/bin/toggle'", "list": f"['/other/', {hotkeys.GNOME_PATH!r}]"}

        def run(args, **_):
            calls.append(args[1:])
            output = state["list"] if args[-1] == "custom-keybindings" else state["command"]
            return Mock(returncode=0, stdout=output if args[1] == "get" else "", stderr="")

        with patch.object(hotkeys.shutil, "which", return_value="/usr/bin/gsettings"):
            # Pausing (while a new shortcut is recorded) clears the keys but keeps the entry.
            self.assertTrue(hotkeys.gnome_shortcut(None, PurePosixPath("/bin/toggle"), run))
            self.assertEqual(calls[-1][-2:], ["binding", ""])
            calls.clear()
            hotkeys.gnome_remove(PurePosixPath("/other/toggle"), run)  # Someone else's command.
            self.assertFalse(any(call[0] in ("set", "reset-recursively") for call in calls))
            hotkeys.gnome_remove(PurePosixPath("/bin/toggle"), run)
            self.assertIn(["set", *hotkeys.GNOME_LIST, "['/other/']"], calls)
            self.assertEqual(calls[-1][0], "reset-recursively")

    def write_raw(self, text: str) -> None:
        """Put arbitrary text in the preferences file, as a hand edit or crash would."""
        self.preferences.path.parent.mkdir(parents=True, exist_ok=True)
        self.preferences.path.write_text(text)

    def test_the_history_shortcut_defaults_to_f_and_can_be_changed_or_switched_off(self):
        self.assertEqual(self.preferences.history_shortcut(), hotkeys.DEFAULT_HISTORY)
        self.assertEqual(hotkeys.default_history_shortcut("linux").label("linux"), "Ctrl+Shift+F")
        self.assertEqual(hotkeys.default_history_shortcut("macos").label("macos"), "⌃⇧F")
        for preset in hotkeys.HISTORY_PRESETS:
            self.assertEqual(preset.problem(), "")
            self.assertNotEqual(preset, hotkeys.DEFAULT)
        choice = hotkeys.HISTORY_PRESETS[1]
        self.preferences.save(history_shortcut=choice)
        self.assertEqual(self.preferences.history_shortcut(), choice)
        self.preferences.save(history_shortcut=False)
        self.assertIsNone(self.preferences.history_shortcut())
        self.preferences.save(open_at_login=False)  # Other changes keep it off.
        self.assertIsNone(self.preferences.history_shortcut())

    def test_defaults_are_control_shift_d_and_f_on_every_platform(self):
        for platform in ("macos", "linux", "windows"):
            self.assertEqual(
                hotkeys.default_shortcut(platform), hotkeys.Shortcut(("ctrl", "shift"), "D")
            )
            self.assertEqual(
                hotkeys.default_history_shortcut(platform),
                hotkeys.Shortcut(("ctrl", "shift"), "F"),
            )

    def test_presets_start_with_the_default_and_keep_the_previous_default(self):
        self.assertEqual(hotkeys.PRESETS[0], hotkeys.DEFAULT)
        self.assertIn(hotkeys.Shortcut(("ctrl", "alt"), "D"), hotkeys.PRESETS)
        self.assertEqual(len(set(hotkeys.PRESETS)), len(hotkeys.PRESETS))

    def test_the_app_name_is_used_for_the_login_entry_and_gnome_binding(self):
        self.assertEqual(hotkeys.APP_NAME, "Clipboard+")
        calls = []

        def run(args, **_):
            calls.append(args[1:])
            return Mock(returncode=0, stdout="[]", stderr="")

        with patch.object(hotkeys.shutil, "which", return_value="/usr/bin/gsettings"):
            hotkeys.gnome_shortcut(hotkeys.DEFAULT, PurePosixPath("/bin/toggle"), run)
        self.assertTrue(
            any(call[-2:] == ["name", hotkeys.DICTATION_SHORTCUT_NAME] for call in calls)
        )
        hotkeys.set_login_item(True, ["/x/python", "/x/tray.py"], self.folder, "linux")
        entry = hotkeys.autostart_path(self.folder).read_text()
        self.assertIn(f"Name={hotkeys.APP_NAME}\n", entry)

    def test_features_keep_clipboard_off_until_setup_and_round_trip(self):
        self.assertEqual(self.preferences.features(), hotkeys.Features(True, False))
        self.assertTrue(self.preferences.clipboard().images)
        self.assertTrue(self.preferences.open_at_login())
        self.assertFalse(self.preferences.share_usage())  # Off until the user agrees.
        for features in (hotkeys.Features(False, True), hotkeys.Features(True, True)):
            self.preferences.save(features=features)
            self.assertEqual(self.preferences.features(), features)

    def test_features_ignore_corrupt_values_and_never_turn_everything_off(self):
        for raw in ("nonsense", {"dictation": "yes"}, {"dictation": False, "clipboard": False}, []):
            self.write_raw(json.dumps({"features": raw}))
            self.assertEqual(self.preferences.features(), hotkeys.Features(True, False), raw)
        self.write_raw("{not json")
        self.assertEqual(self.preferences.features(), hotkeys.Features(True, False))

    def test_saving_features_keeps_the_other_preferences(self):
        self.preferences.save(shortcut=hotkeys.PRESETS[1], open_at_login=False)
        self.preferences.save(features=hotkeys.Features(True, True))
        self.assertEqual(self.preferences.shortcut(), hotkeys.PRESETS[1])
        self.assertFalse(self.preferences.open_at_login())

    def test_clipboard_settings_defaults_round_trip_and_clamping(self):
        self.assertEqual(self.preferences.clipboard(), hotkeys.ClipboardSettings())
        settings = hotkeys.ClipboardSettings(
            keep_items=200, keep_days=7, images=False, paused_until=-1
        )
        self.preferences.save(clipboard=settings)
        self.assertEqual(self.preferences.clipboard(), settings)
        self.preferences.save(clipboard=hotkeys.ClipboardSettings(keep_items=1, keep_days=0))
        self.assertEqual(self.preferences.clipboard().keep_items, 50)
        self.assertEqual(self.preferences.clipboard().keep_days, 1)
        self.preferences.save(
            clipboard=hotkeys.ClipboardSettings(keep_items=10**9, keep_days=10**9)
        )
        self.assertEqual(self.preferences.clipboard().keep_items, 10000)
        self.assertEqual(self.preferences.clipboard().keep_days, 3650)

    def test_clipboard_settings_ignore_wrong_types(self):
        self.write_raw(
            json.dumps({"clipboard": {"keep_items": "many", "keep_days": None, "images": "no"}})
        )
        self.assertEqual(self.preferences.clipboard(), hotkeys.ClipboardSettings())

    def test_pause_state(self):
        settings = hotkeys.ClipboardSettings(paused_until=-1)
        self.assertTrue(settings.paused(now=100.0))  # Until resumed.
        self.assertTrue(hotkeys.ClipboardSettings(paused_until=200.0).paused(now=100.0))
        self.assertFalse(hotkeys.ClipboardSettings(paused_until=200.0).paused(now=300.0))
        self.assertFalse(hotkeys.ClipboardSettings().paused(now=100.0))

    def test_helpers(self):
        with patch("webbrowser.open") as browser:
            hotkeys.open_link()
        browser.assert_called_once_with("https://clipboardplus.apercallc.com")
        self.assertTrue(Path(hotkeys.python_for_gui()).name.startswith("python"))

    def test_python_for_gui_is_the_desktop_implementation(self):
        self.assertIs(hotkeys.python_for_gui, hotkeys.desktop.python_for_gui)


if __name__ == "__main__":
    unittest.main()
