"""Paste and microphone permission handling, atomic writes, and config read errors."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import dictation as d
import permissions


class PasteFailureTests(unittest.TestCase):
    def setUp(self):
        self.config = MagicMock()
        self.config.s.return_value = "powershell.exe"

    def failure(self, error, platform="linux", wayland=True):
        env = {"WAYLAND_DISPLAY": "wayland-0"} if wayland else {}
        with (
            patch.object(d.desktop, "platform_name", return_value=platform),
            patch.dict(os.environ, env, clear=False),
            patch.object(d.subprocess, "run", side_effect=error),
        ):
            if not wayland:
                os.environ.pop("WAYLAND_DISPLAY", None)
            ok = d.paste_text(self.config)
        return ok, d.paste_failure

    def test_a_missing_helper_is_named(self):
        ok, reason = self.failure(FileNotFoundError())
        self.assertFalse(ok)
        self.assertEqual(reason, "wtype is not installed")
        ok, reason = self.failure(FileNotFoundError(), wayland=False)
        self.assertEqual(reason, "xdotool is not installed")

    def test_the_helpers_own_error_output_is_kept(self):
        error = subprocess.CalledProcessError(
            1, "wtype", stderr=b"\nCompositor does not support it\n"
        )
        ok, reason = self.failure(error)
        self.assertFalse(ok)
        self.assertEqual(reason, "Compositor does not support it")
        self.assertEqual(self.failure(subprocess.CalledProcessError(2, "x"))[1], "exit status 2")

    def test_a_timeout_is_a_reason(self):
        self.assertIn("timed out", self.failure(subprocess.TimeoutExpired("wtype", 3))[1])

    def test_success_clears_the_reason(self):
        d.paste_failure = "old"
        with (
            patch.object(d.desktop, "platform_name", return_value="linux"),
            patch.object(d.subprocess, "run"),
        ):
            self.assertTrue(d.paste_text(self.config))
        self.assertEqual(d.paste_failure, "")

    def test_macos_without_accessibility_never_tries(self):
        with (
            patch.object(d.desktop, "platform_name", return_value="macos"),
            patch.object(d.permissions, "accessibility_trusted", return_value=False),
            patch.object(d.subprocess, "run") as run,
        ):
            self.assertFalse(d.paste_text(self.config))
        run.assert_not_called()
        self.assertIn("Accessibility", d.paste_failure)

    def test_macos_trusted_pastes(self):
        with (
            patch.object(d.desktop, "platform_name", return_value="macos"),
            patch.object(d.permissions, "accessibility_trusted", return_value=True),
            patch.object(d.subprocess, "run") as run,
        ):
            self.assertTrue(d.paste_text(self.config))
        self.assertEqual(run.call_args.args[0][0], "osascript")


class MessageTests(unittest.TestCase):
    def test_paste_advice_per_system(self):
        mac = permissions.paste_failure_message("macos")
        self.assertIn(permissions.ACCESSIBILITY_URL, mac)
        self.assertIn("Command+V", mac)
        self.assertIn("ms-settings", permissions.paste_failure_message("windows"))
        with patch.object(permissions.shutil, "which", return_value=None):
            self.assertIn("install wtype", permissions.paste_failure_message("wayland"))
            self.assertIn("install xdotool", permissions.paste_failure_message("x11"))
        with patch.object(permissions.shutil, "which", return_value="/usr/bin/wtype"):
            self.assertIn("GNOME", permissions.paste_failure_message("wayland"))

    def test_microphone_advice_links_to_the_right_place(self):
        self.assertIn(permissions.MAC_MICROPHONE_URL, permissions.microphone_message("macos"))
        self.assertIn("ms-settings:privacy-microphone", permissions.microphone_message("windows"))
        with patch.object(permissions, "linux_audio_server", return_value="none"):
            self.assertIn("PipeWire", permissions.microphone_message("x11"))
        with patch.object(permissions, "linux_audio_server", return_value="alsa"):
            self.assertIn("ALSA", permissions.microphone_message("x11", "busy"))
        self.assertEqual(
            permissions.microphone_settings_url("windows"), "ms-settings:privacy-microphone"
        )
        self.assertIsNone(permissions.microphone_settings_url("x11"))

    def test_the_accessibility_link_is_exact(self):
        self.assertEqual(
            permissions.ACCESSIBILITY_URL,
            "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
        )

    def test_probes_answer_unknown_off_macos(self):
        with patch.object(permissions.desktop, "platform_name", return_value="linux"):
            self.assertIsNone(permissions.accessibility_trusted())
            self.assertEqual(permissions.microphone_status(), "unknown")
            self.assertIsNone(permissions.microphone_blocked())

    def test_microphone_probe_reads_pyobjc(self):
        av = MagicMock()
        av.AVCaptureDevice.authorizationStatusForMediaType_.return_value = 2
        with (
            patch.object(permissions.desktop, "platform_name", return_value="macos"),
            patch.dict(sys.modules, {"AVFoundation": av}),
        ):
            self.assertEqual(permissions.microphone_status(), "denied")
            self.assertIn("denied", permissions.microphone_blocked() or "")
            av.AVCaptureDevice.authorizationStatusForMediaType_.return_value = 3
            self.assertIsNone(permissions.microphone_blocked())

    def test_accessibility_probe_reads_pyobjc(self):
        services = MagicMock()
        services.AXIsProcessTrusted.return_value = True
        with (
            patch.object(permissions.desktop, "platform_name", return_value="macos"),
            patch.dict(sys.modules, {"HIServices": services}),
        ):
            self.assertTrue(permissions.accessibility_trusted())

    def test_open_settings_uses_the_system_opener(self):
        with (
            patch.object(permissions.desktop, "platform_name", return_value="macos"),
            patch.object(permissions.shutil, "which", return_value="/usr/bin/open"),
            patch.object(permissions.subprocess, "Popen") as popen,
        ):
            self.assertTrue(permissions.open_settings(permissions.ACCESSIBILITY_URL))
        self.assertEqual(popen.call_args.args[0], ["open", permissions.ACCESSIBILITY_URL])
        with (
            patch.object(permissions.desktop, "platform_name", return_value="linux"),
            patch.object(permissions.shutil, "which", return_value=None),
        ):
            self.assertFalse(permissions.open_settings("x"))

    def test_tail_keeps_the_last_lines(self):
        self.assertEqual(permissions.tail_of(b"a\nb\nc\nd\n"), "b c d")
        self.assertEqual(permissions.tail_of(None), "")


class AtomicAndReadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_atomic_flushes_the_file_and_the_directory(self):
        target = self.root / "a.json"
        with patch.object(d.os, "fsync", wraps=os.fsync) as sync:
            d.atomic(target, "{}")
        self.assertEqual(target.read_text(), "{}")
        expected = 1 if sys.platform == "win32" else 2
        self.assertEqual(sync.call_count, expected)
        self.assertEqual([p.name for p in self.root.iterdir()], ["a.json"])

    def test_atomic_removes_its_temp_file_only_when_it_fails(self):
        target = self.root / "a.json"
        with patch.object(d.os, "replace", side_effect=PermissionError("locked")):
            with self.assertRaises(PermissionError):
                d.atomic(target, "x")
        self.assertEqual(list(self.root.iterdir()), [])

    def test_a_syntax_error_and_a_permission_error_read_differently(self):
        bad = self.root / "config.json"
        bad.write_text('{"a": ', encoding="utf-8")
        with self.assertRaises(d.DictationError) as syntax:
            d.read_json(bad)
        self.assertIn("valid JSON", str(syntax.exception))
        self.assertIn("line 1", str(syntax.exception))
        with (
            patch.object(Path, "read_text", side_effect=PermissionError("no")),
            patch.object(d.sys, "platform", "linux"),
            self.assertRaises(d.DictationError) as denied,
        ):
            d.read_json(bad)
        self.assertIn("permission denied", str(denied.exception))
        self.assertNotIn("valid JSON", str(denied.exception))
        self.assertEqual(d.read_json(self.root / "missing.json"), {})
        bad.write_text(json.dumps([1]), encoding="utf-8")
        with self.assertRaises(d.DictationError):
            d.read_json(bad)
