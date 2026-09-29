"""Test your shortcut: candidates, verdicts, the Linux acknowledgement path and the panel.

Platform-independent: the platform is always passed or patched, and Tk key masks are
never involved. The panel tests need a display (tests/with-xvfb.sh) and skip without one.
"""

from __future__ import annotations

import argparse
import os
import sys
import unittest
from pathlib import Path, PurePosixPath
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import desktop
import dictation
import hotkeys
import shortcut_test as st
from test_app import ServiceCase
from test_clipui import PageCase

S = hotkeys.Shortcut
PLATFORMS = ("macos", "windows", "linux")


def shortcut_named(kind, platform):
    return (
        hotkeys.default_history_shortcut(platform)
        if kind == "history"
        else hotkeys.default_shortcut(platform)
    )


class CandidateTests(unittest.TestCase):
    def test_every_platform_starts_at_the_shared_default_and_skips_reserved_keys(self):
        for platform in PLATFORMS:
            for kind in hotkeys.KINDS:
                found = st.candidates(kind, platform)
                self.assertEqual(found[0], shortcut_named(kind, platform), (platform, kind))
                self.assertEqual(len(set(found)), len(found))
                for option in found:
                    self.assertEqual(option.problem(platform), "", (platform, option))

    def test_option_command_d_is_not_offered_on_macos_where_it_hides_the_dock(self):
        dock = S(("alt", "cmd"), "D")
        self.assertNotIn(dock, st.candidates("dictation", "macos"))
        self.assertNotIn(dock, st.candidates("dictation", "windows"))
        self.assertIn(dock, st.candidates("dictation", "linux"))

    def test_cycling_visits_each_candidate_once_then_runs_out(self):
        for platform in PLATFORMS:
            current, tried, seen = shortcut_named("dictation", platform), [], []
            while True:
                option = st.next_candidate("dictation", platform, current, tried)
                if option is None:
                    break
                seen.append(option)
                tried.append(current)
                current = option
            self.assertEqual(seen, list(st.candidates("dictation", platform))[1:], platform)
            self.assertIsNone(st.next_candidate("dictation", platform, current, [*tried, current]))

    def test_it_skips_the_other_features_shortcut_and_blocked_ones(self):
        default = shortcut_named("dictation", "linux")
        second = st.candidates("dictation", "linux")[1]
        third = st.candidates("dictation", "linux")[2]
        self.assertEqual(st.next_candidate("dictation", "linux", default), second)
        self.assertEqual(st.next_candidate("dictation", "linux", default, other=second), third)
        self.assertEqual(
            st.next_candidate(
                "dictation", "linux", default, blocked=lambda s: "taken" if s == second else ""
            ),
            third,
        )
        self.assertEqual(st.next_candidate("dictation", "linux", None), default)

    def test_the_gnome_check_names_a_clashing_shortcut_only_on_gnome(self):
        clash = hotkeys.Conflict("Something")
        with (
            patch.object(hotkeys, "detect_backend", return_value="gnome"),
            patch.object(hotkeys, "gnome_conflict", return_value=clash),
        ):
            self.assertEqual(
                st.desktop_blocked("dictation", "linux")(S(("ctrl", "alt"), "D")), "Something"
            )
            self.assertEqual(st.desktop_blocked("dictation", "macos")(S(("ctrl", "alt"), "D")), "")
        with patch.object(hotkeys, "detect_backend", return_value="kde"):
            self.assertEqual(st.desktop_blocked("history", "linux")(S(("ctrl", "alt"), "H")), "")

    def test_the_last_resort_says_how_to_set_it_by_hand(self):
        default = shortcut_named("dictation", "linux")
        parts = st.command_parts("dictation")
        self.assertEqual(parts[-1], "--via-shortcut")
        self.assertIn("dictate-toggle --via-shortcut", st.command_line(parts))
        for backend, needle in (("gnome", "Settings, Keyboard"), ("kde", "System Settings")):
            with patch.object(hotkeys, "detect_backend", return_value=backend):
                self.assertIn(needle, st.manual_text("dictation", default, "linux", parts))
        self.assertIn("Change", st.manual_text("dictation", default, "macos", parts))
        history = st.command_parts("history", Path("/lib"))
        self.assertEqual(history[-2:], ["--clipboard", "--via-shortcut"])


class VerdictTests(ServiceCase):
    def setUp(self):
        super().setUp()
        self.shortcut = S(("ctrl", "alt"), "D")

    def assess(self, now, started=1000.0, **kw):
        return st.assess(
            self.paths, "dictation", self.shortcut, started, now, platform="linux", **kw
        )

    def test_waiting_then_heard_then_silent_after_the_timeout(self):
        self.assertEqual(self.assess(1001.0).phase, "waiting")
        hotkeys.record_heard(self.paths, "dictation", self.shortcut, now=1002.0)
        self.assertEqual(self.assess(1003.0).phase, "heard")
        other = st.assess(self.paths, "history", self.shortcut, 1000.0, 1003.0, platform="linux")
        self.assertEqual(other.phase, "waiting")  # A dictation press is not a history press.
        silent = st.assess(
            self.paths, "history", self.shortcut, 1000.0, 1000.0 + st.TIMEOUT, platform="linux"
        )
        self.assertEqual(silent.phase, "silent")
        self.assertIn("Your desktop", silent.reason)

    def test_a_press_from_before_the_test_does_not_count(self):
        hotkeys.record_heard(self.paths, "dictation", self.shortcut, now=900.0)
        self.assertEqual(self.assess(1001.0).phase, "waiting")

    def test_the_unknown_reason_is_specific_to_the_platform(self):
        for platform, word in (("macos", "browsers"), ("windows", "Windows"), ("linux", "desktop")):
            self.assertIn(word, st.unknown_cause(self.shortcut, platform))

    def test_a_failed_registration_or_a_conflict_is_reported_at_once(self):
        hotkeys.record_status(self.paths, False)
        hotkeys.record_message(self.paths, "Sway said no.")
        result = self.assess(1000.5)
        self.assertEqual((result.phase, result.reason), ("silent", "Sway said no."))
        hotkeys.record_message(self.paths, "")
        self.assertIn("couldn’t register", self.assess(1000.5).reason)
        hotkeys.record_status(
            self.paths, True, conflict=hotkeys.Conflict("the desktop’s “close” shortcut")
        )
        result = self.assess(1000.5)
        self.assertEqual(result.phase, "silent")
        self.assertIn("gets it first", result.reason)

    def test_after_a_change_only_a_newer_status_counts(self):
        hotkeys.record_status(self.paths, False)  # The old shortcut's failure.
        stale = self.paths.runtime / hotkeys.STATUS_NAMES["dictation"]
        os.utime(stale, (500.0, 500.0))
        self.assertEqual(self.assess(1001.0, fresh=True).phase, "waiting")
        os.utime(stale, (1000.5, 1000.5))
        self.assertEqual(self.assess(1001.0, fresh=True).phase, "silent")

    def test_the_outcome_survives_and_a_later_press_overrides_it(self):
        self.assertEqual(
            hotkeys.shortcut_outcome(self.paths, "dictation", self.shortcut), "untested"
        )
        hotkeys.record_outcome(self.paths, "dictation", self.shortcut, False, now=1000.0)
        self.assertEqual(hotkeys.shortcut_outcome(self.paths, "dictation", self.shortcut), "silent")
        self.assertEqual(
            hotkeys.shortcut_outcome(self.paths, "dictation", S(("ctrl",), "F5")), "untested"
        )
        hotkeys.record_heard(self.paths, "dictation", self.shortcut, now=1005.0)
        self.assertEqual(hotkeys.shortcut_outcome(self.paths, "dictation", self.shortcut), "heard")
        hotkeys.record_outcome(self.paths, "dictation", self.shortcut, False, now=2000.0)
        self.assertEqual(hotkeys.shortcut_outcome(self.paths, "dictation", self.shortcut), "silent")
        hotkeys.record_outcome(self.paths, "dictation", self.shortcut, True, now=3000.0)
        self.assertEqual(hotkeys.shortcut_outcome(self.paths, "dictation", self.shortcut), "heard")

    def test_home_text_mentions_an_unheard_shortcut_only_when_it_works(self):
        import app_settings

        _, quiet = app_settings.home_text("X", None, True, "linux", "", "heard")
        _, silent = app_settings.home_text("X", None, True, "linux", "", "silent")
        _, untested = app_settings.home_text("X", None, True, "linux", "", "untested")
        self.assertNotIn("Test your shortcut", quiet)
        self.assertIn("didn’t hear", silent)
        self.assertIn("Test your shortcut", untested)


class AcknowledgementTests(ServiceCase):
    """The Linux path: the desktop runs `... --via-shortcut`, and that records the press."""

    def test_the_marker_reaches_every_desktop_backend(self):
        command = PurePosixPath("/bin/toggle")
        shortcut = hotkeys.default_shortcut("linux")
        seen = []
        run = Mock(return_value=Mock(returncode=0, stdout="ok", stderr=""))
        with patch.object(
            hotkeys, "gnome_shortcut", side_effect=lambda s, c, *a, **k: seen.append(c) or True
        ):
            hotkeys.register_shortcut(shortcut, command, backend="gnome")
        self.assertEqual(seen, [["/bin/toggle", "--via-shortcut"]])
        with patch.object(hotkeys.shutil, "which", return_value="/usr/bin/tool"):
            for backend in ("sway", "hyprland"):
                hotkeys.register_shortcut(shortcut, command, backend=backend, run=run)
        joined = " ".join(str(call) for call in run.call_args_list)
        self.assertEqual(joined.count("/bin/toggle --via-shortcut"), 2)
        self.assertIn("Shift+Mod4+d", joined)
        self.assertIn("SHIFT SUPER, D", joined)
        home = self.folder / "home"
        with (
            patch.object(hotkeys.shutil, "which", return_value="/usr/bin/kwriteconfig6"),
            patch.object(Path, "home", return_value=home),
        ):
            result = hotkeys.register_shortcut(shortcut, command, backend="kde", run=run)
        self.assertTrue(result)
        launcher = next((home / ".local/share/applications").glob("*.desktop"))
        self.assertIn("Exec=/bin/toggle --via-shortcut", launcher.read_text())
        self.assertIn("Shift+Meta+D", " ".join(str(call) for call in run.call_args_list))

    def test_the_dictation_entry_acknowledges_first_when_marked(self):
        args = dictation.build_parser().parse_args(["--via-shortcut"])
        self.assertTrue(args.via_shortcut)
        self.assertFalse(dictation.build_parser().parse_args([]).via_shortcut)
        prefs = hotkeys.Preferences(self.paths)
        chosen = S(("ctrl", "alt"), "D")
        prefs.save(shortcut=chosen)
        dictation.acknowledge_press(argparse.Namespace(via_shortcut=False))
        self.assertFalse(hotkeys.heard_recently(self.paths, "dictation", chosen))
        dictation.acknowledge_press(args)
        self.assertTrue(hotkeys.heard_recently(self.paths, "dictation", chosen))
        # A press of an old binding for a different shortcut is not a press of this one.
        self.assertFalse(hotkeys.heard_recently(self.paths, "dictation", hotkeys.DEFAULT))

    def test_main_records_the_press_before_any_slow_work(self):
        order = []
        with (
            patch.object(sys, "argv", ["dictate-toggle", "--via-shortcut"]),
            patch.object(
                dictation.telemetry, "install", side_effect=lambda *_: order.append("slow")
            ),
            patch.object(
                dictation, "Config", side_effect=lambda *_: order.append("config") or Mock()
            ),
            patch.object(dictation, "run_command", return_value=0),
            patch.object(
                hotkeys, "acknowledge", side_effect=lambda *a: order.append("ack") or True
            ),
        ):
            self.assertEqual(dictation.main(), 0)
        self.assertEqual(order[0], "ack")

    def test_an_unmarked_binding_still_runs_and_leaves_no_note(self):
        with (
            patch.object(sys, "argv", ["dictate-toggle"]),
            patch.object(dictation.telemetry, "install"),
            patch.object(dictation, "Config", return_value=Mock()),
            patch.object(dictation, "run_command", return_value=0) as run,
        ):
            self.assertEqual(dictation.main(), 0)
        run.assert_called_once()
        self.assertFalse((self.paths.runtime / "shortcut-heard-dictation").exists())

    def test_the_history_window_acknowledges_when_marked(self):
        import app

        flag = hotkeys.Preferences(self.paths).history_shortcut()
        for argv, expect in (
            (["--clipboard", "--via-shortcut"], True),
            (["--clipboard"], False),
            (["--settings", "--via-shortcut"], False),
        ):
            (self.paths.runtime / "shortcut-heard-history").unlink(missing_ok=True)
            with patch.object(desktop, "lock", return_value=None):  # A window is already open.
                self.assertEqual(app.main(argv), 0)
            self.assertEqual(hotkeys.heard_recently(self.paths, "history", flag), expect, argv)

    def test_a_failure_to_write_never_blocks_the_press(self):
        with patch.object(hotkeys, "record_heard", side_effect=OSError("disk full")):
            self.assertFalse(hotkeys.acknowledge(self.paths, "dictation"))
        hotkeys.Preferences(self.paths).save(history_shortcut=False)
        self.assertFalse(
            hotkeys.acknowledge(self.paths, "history")
        )  # Switched off: nothing to hear.


class PanelTests(PageCase):
    def setUp(self):
        super().setUp()
        import shortcut_panel

        self.clock = [1000.0]
        self.panel = shortcut_panel.TestPanel(
            self.window.root,
            self.paths,
            self.window.frame,
            "dictation",
            clock=lambda: self.clock[0],
            platform="linux",
            blocked=lambda _: "",
        )
        self.panel.frame.pack()
        self.prefs = hotkeys.Preferences(self.paths)
        self.shortcut = self.prefs.shortcut()

    def buttons(self):
        return [b.cget("text") for b in self.panel.buttons.winfo_children()]

    def press(self, name):
        for button in self.panel.buttons.winfo_children():
            if button.cget("text") == name:
                button.invoke()
                return
        self.fail(f"no {name!r} button in {self.buttons()}")

    def test_press_it_now_then_it_works(self):
        self.panel.start()
        self.assertIn("Press Super+Shift+D now", self.panel.line.cget("text"))
        self.panel.poll()
        self.assertEqual(self.panel.phase, "waiting")
        hotkeys.record_heard(self.paths, "dictation", self.shortcut, now=self.clock[0] + 1)
        self.clock[0] += 2
        self.panel.poll()
        self.assertEqual(self.panel.phase, "heard")
        self.assertIn("✓ It works", self.panel.line.cget("text"))
        self.assertEqual(hotkeys.shortcut_outcome(self.paths, "dictation", self.shortcut), "heard")
        self.assertEqual(self.buttons(), [])

    def test_silence_says_why_and_walks_through_the_candidates_to_the_manual_steps(self):
        self.panel.start()
        tried = []
        for step in range(20):
            self.clock[0] += st.TIMEOUT + 1
            self.panel.poll()
            self.assertEqual(self.panel.phase, "silent")
            self.assertIn("Didn’t hear it", self.panel.line.cget("text"))
            tried.append(self.prefs.shortcut())
            offered = [b for b in self.buttons() if b.startswith("Try ")]
            if not offered:
                break
            self.assertIn(
                offered[0], [f"Try {c.label('linux')}" for c in st.candidates("dictation", "linux")]
            )
            self.press(offered[0])  # Saves it; the tray registers it; the test restarts.
            self.assertEqual(self.panel.phase, "waiting")
            self.assertNotEqual(self.prefs.shortcut(), tried[-1])
        else:
            self.fail("the candidates never ran out")
        self.assertEqual(tried, list(st.candidates("dictation", "linux")))
        self.assertEqual(hotkeys.shortcut_outcome(self.paths, "dictation", tried[-1]), "silent")
        text = self.panel.line.cget("text")
        self.assertIn("No other shortcut to try", text)
        self.assertIn("Test again", self.buttons())
        self.press("Copy command")
        self.assertIn("dictate-toggle --via-shortcut", self.window.root.clipboard_get())
        self.assertIn("Copied", self.buttons())

    def test_a_registration_failure_after_switching_moves_on_without_waiting(self):
        self.panel.start()
        self.clock[0] += st.TIMEOUT + 1
        self.panel.poll()
        self.press("Try " + st.candidates("dictation", "linux")[1].label("linux"))
        hotkeys.record_status(self.paths, False)
        hotkeys.record_message(self.paths, "Sway said no.")
        self.panel.poll()  # Same instant: no timeout, the refusal is enough.
        self.assertEqual(self.panel.phase, "silent")
        self.assertIn("Sway said no.", self.panel.line.cget("text"))

    def test_the_other_features_shortcut_is_never_offered(self):
        self.prefs.save(features=hotkeys.Features(True, True))
        second = st.candidates("dictation", "linux")[1]
        self.prefs.save(history_shortcut=second)
        self.panel.start()
        self.clock[0] += st.TIMEOUT + 1
        self.panel.poll()
        self.assertNotIn(f"Try {second.label('linux')}", self.buttons())

    def test_a_switched_off_shortcut_has_nothing_to_test(self):
        import shortcut_panel

        self.prefs.save(history_shortcut=False)
        panel = shortcut_panel.TestPanel(self.window.root, self.paths, self.window.frame, "history")
        panel.start()
        self.assertEqual(panel.phase, "idle")
        self.assertIn("off", panel.line.cget("text"))

    def test_leaving_the_page_stops_the_poll(self):
        self.window.shortcut_panels.append(self.panel)
        self.panel.start()
        self.assertIsNotNone(self.panel.timer)
        self.window.home()
        self.assertIsNone(self.panel.timer)
        self.assertNotIn(self.panel, self.window.shortcut_panels)


class RecorderAndTutorialTests(PageCase):
    def texts_all(self):
        return self.texts()

    def test_saving_in_the_recorder_offers_the_test_and_done(self):
        self.window.shortcut_page(back=self.window.home, kind="dictation")
        chosen = S(("ctrl", "alt"), "K")
        self.window.consider_shortcut(chosen)
        self.window.save_shortcut()
        self.assertEqual(self.window.page, "shortcut")
        self.assertEqual(len(self.window.shortcut_panels), 1)
        panel = self.window.shortcut_panels[0]
        self.assertEqual(panel.phase, "waiting")
        self.assertIn(chosen.label(), " | ".join(self.texts()))
        self.assertIn("Press ", panel.line.cget("text"))
        self.assertFalse((self.paths.runtime / "shortcut-capture").exists())
        hotkeys.record_heard(self.paths, "dictation", chosen)
        panel.poll()
        self.assertEqual(panel.phase, "heard")

    def test_the_tutorial_and_home_offer_the_test(self):
        with patch.object(self.service, "completed", return_value=False):
            self.window.tutorial()
        self.assertTrue(any("Try your shortcut" in t for t in self.texts()))
        self.assertEqual(len(self.window.shortcut_panels), 1)
        with (
            patch.object(self.service, "completed", return_value=True),
            patch.object(hotkeys, "shortcut_working", return_value=True),
        ):
            self.window.home()
        self.assertEqual(len(self.window.shortcut_panels), 1)  # Untested: a Test button.
        hotkeys.record_heard(self.paths, "dictation", hotkeys.Preferences(self.paths).shortcut())
        with (
            patch.object(self.service, "completed", return_value=True),
            patch.object(hotkeys, "shortcut_working", return_value=True),
        ):
            self.window.home()
        self.assertEqual(self.window.shortcut_panels, [])  # Heard: nothing to nag about.

    def test_macos_recorder_says_the_default_overrides_browsers_and_can_be_changed(self):
        with patch.object(desktop, "platform_name", return_value="macos"):
            self.window.shortcut_page(back=self.window.home, kind="dictation")
            text = " | ".join(self.texts())
        self.assertIn("browsers and editors", text)
        self.assertIn("⇧⌘D", text)


if __name__ == "__main__":
    unittest.main()
