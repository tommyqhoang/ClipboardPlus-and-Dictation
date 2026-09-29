"""Global-shortcut registration on each Linux desktop, and honest failure messages."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import dictation as d
import hotkeys

SHORTCUT = hotkeys.Shortcut(("ctrl", "shift"), "D")
COMMAND = Path("/home/me/.local/bin/dictate-toggle")


def finished(code=0, out="", err=""):
    return SimpleNamespace(returncode=code, stdout=out, stderr=err)


class Recorder:
    """A stand-in for subprocess.run that remembers every command."""

    def __init__(self, *results):
        self.calls = []
        self.results = list(results)

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        if self.results:
            result = self.results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        return finished()


class BackendTests(unittest.TestCase):
    def setUp(self):
        hotkeys._bound.clear()
        self.addCleanup(hotkeys._bound.clear)

    def test_the_desktop_is_recognised(self):
        cases = (
            ({"XDG_CURRENT_DESKTOP": "ubuntu:GNOME"}, "gnome"),
            ({"XDG_CURRENT_DESKTOP": "KDE"}, "kde"),
            ({"KDE_FULL_SESSION": "true"}, "kde"),
            ({"XDG_CURRENT_DESKTOP": "sway", "SWAYSOCK": "/run/sway.sock"}, "sway"),
            ({"SWAYSOCK": "/run/sway.sock"}, "sway"),
            ({"HYPRLAND_INSTANCE_SIGNATURE": "abc", "WAYLAND_DISPLAY": "wayland-1"}, "hyprland"),
            ({"XDG_CURRENT_DESKTOP": "river", "WAYLAND_DISPLAY": "wayland-1"}, "wayland"),
            ({"XDG_CURRENT_DESKTOP": "XFCE", "DISPLAY": ":0"}, "x11"),
        )
        with patch.object(hotkeys.shutil, "which", return_value=None):
            for environment, expected in cases:
                self.assertEqual(hotkeys.detect_backend(environment), expected, environment)

    def test_a_bare_session_with_gsettings_is_gnome(self):
        with patch.object(hotkeys.shutil, "which", return_value="/usr/bin/gsettings"):
            self.assertEqual(hotkeys.detect_backend({}), "gnome")

    def test_each_desktop_spells_the_keys_its_own_way(self):
        both = hotkeys.Shortcut(("cmd", "shift"), "Space")
        self.assertEqual(SHORTCUT.qt(), "Ctrl+Shift+D")
        self.assertEqual(both.qt(), "Meta+Shift+space")
        self.assertEqual(SHORTCUT.sway(), "Ctrl+Shift+d")
        self.assertEqual(both.sway(), "Mod4+Shift+space")
        self.assertEqual(SHORTCUT.hyprland(), ("CTRL SHIFT", "D"))
        self.assertEqual(hotkeys.Shortcut(("alt",), "F5").hyprland(), ("ALT", "F5"))

    def test_manual_instructions_are_the_exact_line(self):
        self.assertEqual(
            hotkeys.manual_instructions(SHORTCUT, COMMAND, "sway"),
            "Add to ~/.config/sway/config:  bindsym Ctrl+Shift+d exec /home/me/.local/bin/dictate-toggle",
        )
        self.assertIn(
            "bind = CTRL SHIFT, D, exec, /home/me/.local/bin/dictate-toggle",
            hotkeys.manual_instructions(SHORTCUT, COMMAND, "hyprland"),
        )
        self.assertIn("Command or Script", hotkeys.manual_instructions(SHORTCUT, COMMAND, "kde"))
        self.assertIn(
            "Wayland apps cannot", hotkeys.manual_instructions(SHORTCUT, COMMAND, "wayland")
        )
        self.assertIn("dictate-toggle", hotkeys.manual_instructions(None, COMMAND, "x11"))


class SwayTests(BackendTests):
    def test_binds_at_runtime_and_prints_the_config_line(self):
        run = Recorder(finished(out='[{"success": true}]'))
        with patch.object(hotkeys.shutil, "which", return_value="/usr/bin/swaymsg"):
            result = hotkeys.register_shortcut(SHORTCUT, COMMAND, backend="sway", run=run)
        self.assertTrue(result)
        self.assertEqual(
            run.calls[0],
            ["/usr/bin/swaymsg", "bindsym", "Ctrl+Shift+d", "exec", str(COMMAND)],
        )
        self.assertIn("until Sway restarts", result.message)
        self.assertIn("bindsym Ctrl+Shift+d exec", result.manual)

    def test_a_refusal_is_reported_not_swallowed(self):
        run = Recorder(finished(1, err="Unknown/invalid command"))
        with patch.object(hotkeys.shutil, "which", return_value="/usr/bin/swaymsg"):
            result = hotkeys.register_shortcut(SHORTCUT, COMMAND, backend="sway", run=run)
        self.assertFalse(result)
        self.assertIn("Unknown/invalid command", result.message)
        self.assertIn("bindsym", result.message)

    def test_a_missing_swaymsg_still_gives_the_line(self):
        with patch.object(hotkeys.shutil, "which", return_value=None):
            result = hotkeys.register_shortcut(SHORTCUT, COMMAND, backend="sway")
        self.assertFalse(result)
        self.assertIn("swaymsg was not found", result.message)
        self.assertIn("bindsym Ctrl+Shift+d exec", result.message)

    def test_changing_the_shortcut_unbinds_the_old_one(self):
        run = Recorder()
        with patch.object(hotkeys.shutil, "which", return_value="/usr/bin/swaymsg"):
            hotkeys.register_shortcut(SHORTCUT, COMMAND, backend="sway", run=run)
            hotkeys.register_shortcut(
                hotkeys.Shortcut(("alt",), "F9"), COMMAND, backend="sway", run=run
            )
            paused = hotkeys.register_shortcut(None, COMMAND, backend="sway", run=run)
        self.assertIn(["/usr/bin/swaymsg", "unbindsym", "Ctrl+Shift+d"], run.calls)
        self.assertIn(["/usr/bin/swaymsg", "unbindsym", "Alt+F9"], run.calls)
        self.assertTrue(paused)


class HyprlandTests(BackendTests):
    def test_binds_with_hyprctl(self):
        run = Recorder(finished(out="ok"))
        with patch.object(hotkeys.shutil, "which", return_value="/usr/bin/hyprctl"):
            result = hotkeys.register_shortcut(SHORTCUT, COMMAND, backend="hyprland", run=run)
        self.assertTrue(result)
        self.assertEqual(
            run.calls[0],
            ["/usr/bin/hyprctl", "keyword", "bind", f"CTRL SHIFT, D, exec, {COMMAND}"],
        )
        self.assertIn("bind = CTRL SHIFT, D, exec,", result.manual)

    def test_an_error_sentence_from_hyprctl_is_a_failure(self):
        run = Recorder(finished(out="invalid dispatcher"))
        with patch.object(hotkeys.shutil, "which", return_value="/usr/bin/hyprctl"):
            result = hotkeys.register_shortcut(SHORTCUT, COMMAND, backend="hyprland", run=run)
        self.assertFalse(result)
        self.assertIn("invalid dispatcher", result.message)

    def test_a_missing_hyprctl_and_a_crash(self):
        with patch.object(hotkeys.shutil, "which", return_value=None):
            self.assertFalse(hotkeys.register_shortcut(SHORTCUT, COMMAND, backend="hyprland"))
        run = Recorder(OSError("gone"))
        with patch.object(hotkeys.shutil, "which", return_value="/usr/bin/hyprctl"):
            result = hotkeys.register_shortcut(SHORTCUT, COMMAND, backend="hyprland", run=run)
        self.assertFalse(result)
        self.assertIn("gone", result.message)


class KdeTests(BackendTests):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        home = patch.object(hotkeys.Path, "home", return_value=Path(self.temp.name))
        home.start()
        self.addCleanup(home.stop)

    def test_writes_the_launcher_and_the_global_shortcut(self):
        run = Recorder()
        with patch.object(
            hotkeys.shutil,
            "which",
            side_effect=lambda n: "/usr/bin/" + n if n == "kwriteconfig6" else None,
        ):
            result = hotkeys.register_shortcut(SHORTCUT, COMMAND, backend="kde", run=run)
        self.assertTrue(result)
        desktop_file = (
            Path(self.temp.name)
            / ".local/share/applications/clipboardplus-clipboard-dictation.desktop"
        )
        text = desktop_file.read_text()
        self.assertIn(f"Exec={COMMAND}", text)
        self.assertIn("X-KDE-GlobalAccel-CommandShortcut=true", text)
        self.assertEqual(
            run.calls[0],
            [
                "/usr/bin/kwriteconfig6",
                "--file",
                "kglobalshortcutsrc",
                "--group",
                "services",
                "--group",
                "clipboardplus-clipboard-dictation.desktop",
                "--key",
                "_launch",
                "Ctrl+Shift+D",
            ],
        )
        self.assertIn("log out", result.message)
        self.assertIn("Command or Script", result.message)

    def test_kde_five_is_used_when_six_is_absent(self):
        run = Recorder()
        with patch.object(
            hotkeys.shutil,
            "which",
            side_effect=lambda n: "/usr/bin/" + n if n == "kwriteconfig5" else None,
        ):
            self.assertTrue(hotkeys.register_shortcut(SHORTCUT, COMMAND, backend="kde", run=run))
        self.assertEqual(run.calls[0][0], "/usr/bin/kwriteconfig5")

    def test_pausing_sets_none(self):
        run = Recorder()
        with patch.object(hotkeys.shutil, "which", return_value="/usr/bin/kwriteconfig6"):
            self.assertTrue(hotkeys.register_shortcut(None, COMMAND, backend="kde", run=run))
        self.assertEqual(run.calls[0][-1], "none")

    def test_failures_say_what_to_do(self):
        with patch.object(hotkeys.shutil, "which", return_value=None):
            result = hotkeys.register_shortcut(SHORTCUT, COMMAND, backend="kde")
        self.assertIn("kwriteconfig was not found", result.message)
        run = Recorder(finished(1, err="cannot write"))
        with patch.object(hotkeys.shutil, "which", return_value="/usr/bin/kwriteconfig6"):
            result = hotkeys.register_shortcut(SHORTCUT, COMMAND, backend="kde", run=run)
        self.assertFalse(result)
        self.assertIn("cannot write", result.message)


class GnomeAndPlainTests(BackendTests):
    def test_gnome_success_and_failure(self):
        with patch.object(hotkeys, "gnome_shortcut", return_value=True):
            result = hotkeys.register_shortcut(SHORTCUT, COMMAND, backend="gnome")
        self.assertTrue(result)
        self.assertEqual(result.backend, "gnome")
        with patch.object(hotkeys, "gnome_shortcut", return_value=False):
            result = hotkeys.register_shortcut(SHORTCUT, COMMAND, backend="gnome")
        self.assertFalse(result)
        self.assertIn("gsettings", result.message)
        self.assertIn("Custom Shortcuts", result.manual)

    def test_plain_wayland_is_honest(self):
        with patch.object(hotkeys.shutil, "which", return_value=None):
            result = hotkeys.register_shortcut(SHORTCUT, COMMAND, backend="wayland")
        self.assertFalse(result)
        self.assertIn("doesn’t let apps register", result.message)
        self.assertIn(str(COMMAND), result.message)

    def test_an_unknown_x11_desktop_is_honest(self):
        with patch.object(hotkeys.shutil, "which", return_value=None):
            result = hotkeys.register_shortcut(SHORTCUT, COMMAND, backend="x11")
        self.assertFalse(result)
        self.assertIn("no shortcut service", result.message)

    def test_a_failed_gsettings_call_is_not_an_exception(self):
        run = Recorder(subprocess.SubprocessError("boom"))
        with patch.object(hotkeys.shutil, "which", return_value="/usr/bin/gsettings"):
            self.assertFalse(hotkeys.gnome_shortcut(SHORTCUT, COMMAND, run))


class MessageFileTests(unittest.TestCase):
    def test_the_reason_is_kept_for_the_window(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        with patch.dict(
            os.environ,
            {
                "XDG_CONFIG_HOME": temp.name + "/c",
                "XDG_CACHE_HOME": temp.name + "/k",
                "XDG_RUNTIME_DIR": temp.name + "/r",
            },
        ):
            paths = d.Paths()
            self.assertEqual(hotkeys.shortcut_message(paths), "")
            hotkeys.record_message(paths, "Bind it by hand")
            hotkeys.record_message(paths, "history", hotkeys.HISTORY_STATUS)
            self.assertEqual(hotkeys.shortcut_message(paths), "Bind it by hand")
            self.assertEqual(hotkeys.shortcut_message(paths, hotkeys.HISTORY_STATUS), "history")
            hotkeys.record_message(paths, "")
            self.assertEqual(hotkeys.shortcut_message(paths), "")


if __name__ == "__main__":
    unittest.main()
