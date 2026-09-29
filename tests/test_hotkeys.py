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
        shortcut = hotkeys.Shortcut(("ctrl", "shift"), "D")  # The earlier default's spellings.
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
        self.assertEqual(hotkeys.Shortcut(("cmd",), "D").problem("macos"), "")

    def test_captured_keys(self):
        flags = hotkeys.EVENT_FLAGS["shift"] | hotkeys.EVENT_FLAGS["ctrl"]
        old = hotkeys.Shortcut(("ctrl", "shift"), "D")
        self.assertEqual(hotkeys.from_mac_event(2, flags, "d"), old)
        self.assertEqual(hotkeys.from_mac_event(49, flags, " ").key, "Space")
        # Every modifier survives, whatever order the event flags are read in.
        every = sum(hotkeys.EVENT_FLAGS.values())
        self.assertEqual(hotkeys.from_mac_event(2, every, "d").modifiers, hotkeys.MODIFIER_ORDER)
        self.assertEqual(hotkeys.from_mac_event(200, 0, "é").key, "É")
        self.assertIsNone(hotkeys.from_tk("Control_L", set()))
        self.assertEqual(hotkeys.from_tk("d", {"ctrl", "shift"}), old)
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
            self.assertEqual(calls[-1][-2:], ["binding", "<Shift><Super>d"])
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
            hotkeys.GNOME_PATH: ("Clipboard+ and Dictation", "<Shift><Super>d"),
            old: ("Whisper Dictation", "<Super><Shift>D"),  # Other order and case: same keys.
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
        self.assertEqual([call[-1] for call in calls[-2:]], ["", "<Shift><Super>f"])

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
        self.assertEqual(hotkeys.default_history_shortcut("linux").label("linux"), "Super+Shift+F")
        self.assertEqual(
            hotkeys.default_history_shortcut("windows").label("windows"), "Win+Shift+F"
        )
        self.assertEqual(hotkeys.default_history_shortcut("macos").label("macos"), "⇧⌘F")
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

    def test_defaults_are_the_same_cmd_shift_d_and_f_on_every_platform(self):
        for platform in ("macos", "linux", "windows"):
            self.assertEqual(
                hotkeys.default_shortcut(platform), hotkeys.Shortcut(("shift", "cmd"), "D")
            )
            self.assertEqual(
                hotkeys.default_history_shortcut(platform),
                hotkeys.Shortcut(("shift", "cmd"), "F"),
            )
        default, history = (
            hotkeys.default_shortcut("linux"),
            hotkeys.default_history_shortcut("linux"),
        )
        self.assertEqual(
            [default.label(p) for p in ("macos", "windows", "linux")],
            ["⇧⌘D", "Win+Shift+D", "Super+Shift+D"],
        )
        self.assertEqual(
            [history.label(p) for p in ("macos", "windows", "linux")],
            ["⇧⌘F", "Win+Shift+F", "Super+Shift+F"],
        )

    def test_every_backend_spells_the_cmd_defaults(self):
        for shortcut, letter in (
            (hotkeys.default_shortcut("linux"), "D"),
            (hotkeys.default_history_shortcut("linux"), "F"),
        ):
            self.assertEqual(shortcut.gnome(), f"<Shift><Super>{letter.lower()}")
            self.assertEqual(shortcut.qt(), f"Shift+Meta+{letter}")
            self.assertEqual(shortcut.sway(), f"Shift+Mod4+{letter.lower()}")
            self.assertEqual(shortcut.hyprland(), ("SHIFT SUPER", letter))
            self.assertEqual(shortcut.carbon_modifiers(), 0x200 | 0x100)  # shift + cmdKey
            self.assertEqual(shortcut.windows_modifiers(), 0x4 | 0x8 | hotkeys.MOD_NOREPEAT)
            self.assertEqual(shortcut.windows_key(), ord(letter))
        self.assertEqual(
            hotkeys.default_shortcut("macos").mac_key_code(), hotkeys.MAC_KEY_CODES["D"]
        )
        self.assertEqual(hotkeys.default_history_shortcut("macos").mac_key_code(), 3)

    def test_the_default_is_not_reserved_anywhere_and_old_and_new_are_presets(self):
        for platform in ("macos", "windows", "linux"):
            for shortcut in (
                hotkeys.default_shortcut(platform),
                hotkeys.default_history_shortcut(platform),
            ):
                self.assertEqual(shortcut.problem(platform), "", platform)
            self.assertEqual(hotkeys.presets(platform)[0], hotkeys.default_shortcut(platform))
            self.assertIn(hotkeys.Shortcut(("ctrl", "shift"), "D"), hotkeys.presets(platform))
            self.assertEqual(
                hotkeys.history_presets(platform)[:2],
                (
                    hotkeys.default_history_shortcut(platform),
                    hotkeys.Shortcut(("ctrl", "shift"), "F"),
                ),
            )
            for preset in (*hotkeys.presets(platform), *hotkeys.history_presets(platform)):
                self.assertEqual(preset.problem(platform), "", (platform, preset))
        self.assertIn(hotkeys.Shortcut(("alt",), "Space"), hotkeys.presets("macos"))
        self.assertNotIn(hotkeys.Shortcut(("alt",), "Space"), hotkeys.presets("windows"))

    def test_reserved_combinations_are_refused_with_a_clear_message(self):
        cases = (
            ("macos", ("cmd",), "Space", "macOS"),
            ("macos", ("cmd",), "Tab", "macOS"),
            ("macos", ("alt", "cmd"), "D", "macOS"),  # Shows and hides the Dock.
            ("windows", ("cmd",), "D", "Windows"),
            ("windows", ("cmd",), "L", "Windows"),
            ("windows", ("cmd",), "R", "Windows"),
            ("windows", ("shift", "cmd"), "S", "Windows"),
            ("windows", ("alt",), "F4", "Windows"),
            ("linux", ("cmd",), "L", "your desktop"),
            ("linux", ("alt",), "Tab", "your desktop"),
        )
        for platform, modifiers, key, who in cases:
            shortcut = hotkeys.Shortcut(modifiers, key)
            message = shortcut.problem(platform)
            self.assertIn(who, message, (platform, shortcut))
            self.assertIn(shortcut.label(platform), message)
        self.assertNotEqual(hotkeys.Shortcut(("cmd",), "Q").problem("macos"), "")  # Quit.
        # The same keys are fine where nothing owns them.
        self.assertEqual(hotkeys.Shortcut(("alt", "cmd"), "D").problem("linux"), "")
        self.assertEqual(hotkeys.Shortcut(("shift", "cmd"), "D").problem("windows"), "")
        # A saved shortcut is never thrown away for being reserved by a later release.
        self.preferences.save(shortcut=hotkeys.Shortcut(("alt", "cmd"), "D"))
        self.assertEqual(self.preferences.shortcut(), hotkeys.Shortcut(("alt", "cmd"), "D"))

    def test_a_saved_shortcut_wins_over_the_new_default(self):
        old = hotkeys.Shortcut(("ctrl", "shift"), "D")
        self.preferences.save(
            shortcut=old, history_shortcut=hotkeys.Shortcut(("ctrl", "shift"), "F")
        )
        self.assertEqual(self.preferences.shortcut(), old)
        self.assertEqual(
            self.preferences.history_shortcut(), hotkeys.Shortcut(("ctrl", "shift"), "F")
        )

    def test_the_marker_is_added_once_and_removal_matches_either_command(self):
        marked = hotkeys.via_shortcut(PurePosixPath("/bin/toggle"))
        self.assertEqual(marked, ["/bin/toggle", "--via-shortcut"])
        self.assertEqual(hotkeys.via_shortcut(marked), marked)
        self.assertEqual(
            hotkeys.via_shortcut(["/x/python", "/x/app.py", "--clipboard"])[-1], "--via-shortcut"
        )
        for stored in ("/bin/toggle", "/bin/toggle --via-shortcut"):
            calls = []

            def run(args, stored=stored, **_):
                calls.append(args[1:])
                text = (
                    f"[{hotkeys.GNOME_PATH!r}]"
                    if args[1:3] == ["get", *hotkeys.GNOME_LIST[:1]]
                    else repr(stored)
                )
                return Mock(returncode=0, stdout=text, stderr="")

            with patch.object(hotkeys.shutil, "which", return_value="/usr/bin/gsettings"):
                hotkeys.gnome_remove(PurePosixPath("/bin/toggle"), run)
            self.assertTrue(any(call[0] == "reset-recursively" for call in calls), stored)
        with patch.object(hotkeys.shutil, "which", return_value="/usr/bin/gsettings"):
            calls.clear()
            hotkeys.gnome_remove(
                PurePosixPath("/bin/toggle"),
                lambda args, **_: Mock(returncode=0, stdout="'/other'", stderr=""),
            )
        self.assertFalse(any(call[0] == "reset-recursively" for call in calls))

    def test_presets_start_with_the_default_and_keep_the_previous_default(self):
        self.assertEqual(hotkeys.PRESETS[0], hotkeys.DEFAULT)
        self.assertIn(hotkeys.Shortcut(("ctrl", "shift"), "D"), hotkeys.PRESETS)
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


class MacKeyCaptureTests(unittest.TestCase):
    """What the recorder makes of Tk's events, per platform (Tk Aqua differs from X11)."""

    def test_command_is_meta_on_a_mac_and_option_is_alt(self):
        self.assertEqual(hotkeys.tk_modifier("Meta_L", "macos"), "cmd")
        self.assertEqual(hotkeys.tk_modifier("Meta_R", "macos"), "cmd")
        self.assertEqual(hotkeys.tk_modifier("Alt_L", "macos"), "alt")
        self.assertEqual(hotkeys.tk_modifier("Option_R", "macos"), "alt")
        self.assertEqual(hotkeys.tk_modifier("Meta_L", "linux"), "alt")  # X11's Meta is Alt.
        self.assertEqual(hotkeys.tk_modifier("Super_L", "linux"), "cmd")
        self.assertEqual(hotkeys.tk_modifier("Win_L", "windows"), "cmd")
        self.assertEqual(hotkeys.tk_modifier("d", "macos"), "")

    def test_state_masks_per_platform(self):
        # Tk Aqua: Command is Mod1 (0x8), Option is Mod2 (0x10).
        self.assertEqual(
            hotkeys.modifiers_from_state(0x1 | 0x4 | 0x8, "macos"), {"shift", "ctrl", "cmd"}
        )
        self.assertEqual(hotkeys.modifiers_from_state(0x10, "macos"), {"alt"})
        # X11: Alt is Mod1, Super is Mod4; NumLock (Mod2) and CapsLock mean nothing.
        self.assertEqual(hotkeys.modifiers_from_state(0x8 | 0x40, "linux"), {"alt", "cmd"})
        self.assertEqual(hotkeys.modifiers_from_state(0x10 | 0x2, "linux"), set())
        # Windows: Alt is 0x20000, and 0x8 is NumLock, not Alt.
        self.assertEqual(hotkeys.modifiers_from_state(0x20000 | 0x4, "windows"), {"alt", "ctrl"})
        self.assertEqual(hotkeys.modifiers_from_state(0x8, "windows"), set())
        self.assertEqual(hotkeys.modifiers_from_state(0, "macos"), set())

    def test_the_mac_state_decides_even_when_a_release_was_missed(self):
        # Aqua sometimes never reports a modifier's release: only the mask is trusted.
        shortcut = hotkeys.from_tk("d", {"cmd", "shift"}, state=0x4 | 0x1, platform="macos")
        self.assertEqual(shortcut, hotkeys.Shortcut(("ctrl", "shift"), "D"))
        # With nothing in the mask the tracked keys are all there is.
        self.assertEqual(
            hotkeys.from_tk("F5", {"ctrl"}, state=0, platform="macos"),
            hotkeys.Shortcut(("ctrl",), "F5"),
        )

    def test_command_and_option_are_recorded_as_pressed(self):
        command_d = hotkeys.from_tk("d", set(), state=0x8 | 0x1, keycode=2, platform="macos")
        self.assertEqual(command_d, hotkeys.Shortcut(("shift", "cmd"), "D"))
        option_space = hotkeys.from_tk("space", set(), state=0x10, keycode=49, platform="macos")
        self.assertEqual(option_space, hotkeys.Shortcut(("alt",), "Space"))

    def test_other_platforms_merge_tracked_keys_with_the_mask(self):
        linux = hotkeys.from_tk("d", {"cmd"}, state=0x4, platform="linux")
        self.assertEqual(linux, hotkeys.Shortcut(("ctrl", "cmd"), "D"))
        windows = hotkeys.from_tk("d", {"cmd"}, state=0x20000, platform="windows")
        self.assertEqual(windows, hotkeys.Shortcut(("alt", "cmd"), "D"))

    def test_option_letters_use_the_hardware_key_code(self):
        # Option+D types "∂" on a Mac; Tk puts the virtual key code in the low 16 bits.
        keycode = (ord("∂") << 16) | hotkeys.MAC_KEY_CODES["D"]
        shortcut = hotkeys.from_tk("∂", set(), state=0x10, keycode=keycode, platform="macos")
        self.assertEqual(shortcut, hotkeys.Shortcut(("alt",), "D"))
        self.assertEqual(shortcut.problem(), "")
        # Shift+1 is "exclam" to Tk; the key code still says 1.
        bang = hotkeys.from_tk("exclam", set(), state=0x1 | 0x4, keycode=18, platform="macos")
        self.assertEqual(bang, hotkeys.Shortcut(("ctrl", "shift"), "1"))
        # The keysym still wins when it is a usable key.
        self.assertEqual(
            hotkeys.from_tk("d", set(), state=0x4, keycode=99, platform="macos").key, "D"
        )
        # An unknown key code leaves the keysym, which then fails validation.
        odd = hotkeys.from_tk("∂", set(), state=0x10, keycode=0xFFF, platform="macos")
        self.assertNotEqual(odd.problem(), "")
        # Only a Mac has this fallback.
        linux = hotkeys.from_tk("∂", set(), state=0x8, keycode=2, platform="linux")
        self.assertEqual(linux.key, "∂")

    def test_a_shortcut_the_other_feature_owns_is_refused(self):
        mine = hotkeys.Shortcut(("ctrl", "shift"), "D")
        self.assertIn("clipboard history", hotkeys.choice_problem(mine, mine, "history"))
        self.assertIn("dictation", hotkeys.choice_problem(mine, mine, "dictation"))
        self.assertEqual(hotkeys.choice_problem(mine, None, "history"), "")
        self.assertEqual(
            hotkeys.choice_problem(mine, hotkeys.Shortcut(("ctrl",), "F5"), "history"), ""
        )
        self.assertNotEqual(hotkeys.choice_problem(hotkeys.Shortcut((), "D"), None, "history"), "")


class HeardTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        folder = Path(temporary.name)
        environment = patch.dict(
            os.environ,
            {"XDG_CONFIG_HOME": str(folder / "config"), "XDG_RUNTIME_DIR": str(folder / "run")},
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.paths = d.Paths()
        self.shortcut = hotkeys.Shortcut(("ctrl", "shift"), "D")

    def test_a_press_is_written_atomically_and_read_back(self):
        hotkeys.record_heard(self.paths, "dictation", self.shortcut, now=1000.0)
        data = json.loads((self.paths.runtime / "shortcut-heard-dictation").read_text())
        self.assertEqual(data["at"], 1000.0)
        self.assertTrue(hotkeys.heard_recently(self.paths, "dictation", self.shortcut, now=1010.0))
        self.assertFalse(hotkeys.heard_recently(self.paths, "history", self.shortcut, now=1010.0))

    def test_an_old_or_other_shortcuts_press_is_not_heard(self):
        hotkeys.record_heard(self.paths, "history", self.shortcut, now=1000.0)
        later = 1000.0 + hotkeys.HEARD_SECONDS + 1
        self.assertFalse(hotkeys.heard_recently(self.paths, "history", self.shortcut, now=later))
        other = hotkeys.Shortcut(("ctrl", "alt"), "K")
        self.assertFalse(hotkeys.heard_recently(self.paths, "history", other, now=1001.0))

    def test_missing_or_broken_files_mean_not_heard(self):
        self.assertFalse(hotkeys.heard_recently(self.paths, "dictation", self.shortcut))
        d.private_dir(self.paths.runtime)
        (self.paths.runtime / "shortcut-heard-dictation").write_text("not json")
        self.assertFalse(hotkeys.heard_recently(self.paths, "dictation", self.shortcut))
        (self.paths.runtime / "shortcut-heard-dictation").write_text('{"at": "x"}')
        self.assertFalse(hotkeys.heard_recently(self.paths, "dictation", self.shortcut))

    def test_capture_flags_and_status_names_cover_both_kinds(self):
        self.assertEqual(set(hotkeys.CAPTURE_FLAGS), set(hotkeys.KINDS))
        self.assertEqual(hotkeys.CAPTURE_FLAGS["history"], "history-shortcut-capture")
        self.assertEqual(hotkeys.STATUS_NAMES["history"], hotkeys.HISTORY_STATUS)

    def test_the_log_location_names_a_file(self):
        self.assertTrue(hotkeys.log_location("menubar").endswith("menubar.log"))


class SettingsShortcutTextTests(unittest.TestCase):
    def test_test_line_says_ready_then_heard_then_why_not(self):
        import app_settings as a

        label, log = "⌃⇧D", "/logs/menubar.log"
        self.assertEqual(
            a.shortcut_test_text(label, False, True, "", log, "macos"),
            "Ready — press ⌃⇧D anywhere to test it",
        )
        self.assertEqual(
            a.shortcut_test_text(label, True, True, "", log, "windows"), "✓ Heard ⌃⇧D just now"
        )
        failed = a.shortcut_test_text(label, False, False, "It is taken.", log, "macos")
        self.assertIn("It is taken.", failed)
        self.assertIn(log, failed)
        self.assertIn(log, a.shortcut_test_text(label, False, False, "", log, "macos"))
        # A reason that already names the log is not repeated.
        again = a.shortcut_test_text(label, False, False, f"Details: {log}", log, "macos")
        self.assertEqual(again.count(log), 1)
        # Every platform can hear it now (on Linux the desktop's command leaves the note).
        self.assertEqual(
            a.shortcut_test_text(label, False, True, "", log, "linux"),
            "Ready — press ⌃⇧D anywhere to test it",
        )
        self.assertIn(
            "didn’t hear", a.shortcut_test_text(label, False, True, "", log, "linux", "silent")
        )

    def test_the_row_problem_prefers_what_the_tray_recorded(self):
        import app_settings as a

        self.assertEqual(a.shortcut_problem("X", "macos", "Because.", "cmd"), "Because.")
        self.assertIn("cmd", a.shortcut_problem("X", "linux", "", "cmd"))
        self.assertIn("already uses X", a.shortcut_problem("X", "windows", "", "cmd"))
        title, text = a.home_text("X", None, False, "macos", "macOS says no.")
        self.assertIn("macOS says no.", text)


if __name__ == "__main__":
    unittest.main()
