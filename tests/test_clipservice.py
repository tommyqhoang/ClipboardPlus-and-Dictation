"""The clipboard service: what it stores, when it stops, and how it survives failures."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import clipservice
import clipstore
import clipwatch
import desktop
import dictation as d
import hotkeys
from support import make_png


class FakeWatcher:
    """Hands out queued clips (or raises queued exceptions), one per call."""

    def __init__(self, *events: clipwatch.Clip | Exception | None) -> None:
        self.events = list(events)
        self.closed = False
        self.calls = 0

    def next_change(self, timeout: float) -> clipwatch.Clip | None:
        self.calls += 1
        if not self.events:
            return None
        event = self.events.pop(0)
        if isinstance(event, Exception):
            raise event
        return event

    def close(self) -> None:
        self.closed = True


class ServiceCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        folder = Path(temporary.name)
        environment = patch.dict(
            os.environ,
            {
                "XDG_CONFIG_HOME": str(folder / "config"),
                "XDG_CACHE_HOME": str(folder / "cache"),
                "XDG_RUNTIME_DIR": str(folder / "runtime"),
            },
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.paths = d.Paths()
        d.private_dir(self.paths.runtime)
        self.store = clipstore.Store(self.paths.clipboard)
        self.addCleanup(self.store.close)
        self.now = 1_000_000.0
        self.settings = hotkeys.ClipboardSettings()
        self.enabled = True

    def service(self, *events: clipwatch.Clip | Exception | None) -> clipservice.Service:
        self.watcher = FakeWatcher(*events)
        return clipservice.Service(
            self.store,
            self.watcher,
            lambda: self.settings,
            lambda: self.enabled,
            self.paths,
            clock=lambda: self.now,
        )

    def texts(self) -> list[str]:
        return [item.text for item in self.store.list(limit=100)]


class CaptureTests(ServiceCase):
    def test_copied_text_is_stored_as_a_desktop_item(self):
        service = self.service(clipwatch.Clip(text="hello"))
        service.step(0)
        item = self.store.list()[0]
        self.assertEqual((item.text, item.source), ("hello", "desktop"))

    def test_a_link_is_stored_as_a_link(self):
        self.service(clipwatch.Clip(text="https://example.com")).step(0)
        self.assertEqual(self.store.list()[0].kind, "url")

    def test_concealed_clips_are_never_stored(self):
        # Even a watcher that wrongly hands over the content must not get it stored.
        leaky = clipwatch.Clip(text="hunter2", image_png=make_png(), concealed=True)
        service = self.service(leaky, clipwatch.Clip(text="visible"))
        service.step(0)
        service.step(0)
        self.assertEqual(self.texts(), ["visible"])

    def test_nothing_is_stored_while_paused_and_capture_resumes_afterwards(self):
        self.settings = hotkeys.ClipboardSettings(paused_until=self.now + 60)
        service = self.service(
            clipwatch.Clip(text="during"), clipwatch.Clip(text="after"), clipwatch.Clip(text="x")
        )
        service.step(0)
        self.assertTrue(service.paused())
        self.now += 61
        self.assertFalse(service.paused())
        service.step(0)
        self.assertEqual(self.texts(), ["after"])

    def test_pausing_until_resumed_has_no_end_time(self):
        self.settings = hotkeys.ClipboardSettings(paused_until=-1)
        service = self.service(clipwatch.Clip(text="secret plans"))
        self.now += 10**7
        service.step(0)
        self.assertTrue(service.paused())
        self.assertEqual(self.texts(), [])

    def test_nothing_is_stored_when_the_feature_is_off(self):
        self.enabled = False
        self.service(clipwatch.Clip(text="hello")).step(0)
        self.assertEqual(self.texts(), [])

    def test_images_are_stored_unless_switched_off(self):
        png = make_png(3, 3)
        self.service(clipwatch.Clip(image_png=png)).step(0)
        self.assertEqual([item.kind for item in self.store.list()], ["image"])
        self.settings = hotkeys.ClipboardSettings(images=False)
        self.service(clipwatch.Clip(image_png=make_png(5, 5))).step(0)
        self.assertEqual(self.store.count(), 1)

    def test_text_wins_when_a_clip_has_both(self):
        self.service(clipwatch.Clip(text="cells", image_png=make_png())).step(0)
        self.assertEqual([item.kind for item in self.store.list()], ["text"])

    def test_no_change_stores_nothing(self):
        self.service(None).step(0)
        self.assertEqual(self.store.count(), 0)

    def test_a_burst_of_copies_is_all_kept(self):
        service = self.service(*[clipwatch.Clip(text=f"item {n}") for n in range(200)])
        for _ in range(200):
            service.step(0)
        self.assertEqual(self.store.count(), 200)


class DiskFullTests(ServiceCase):
    def test_a_full_disk_while_storing_an_image_is_reported_not_fatal(self):
        service = self.service(
            clipwatch.Clip(image_png=make_png()), clipwatch.Clip(text="still captured")
        )
        with patch.object(self.store, "add_image", side_effect=OSError(28, "No space left")):
            self.assertFalse(service.step(0))  # Tells the loop to pause before retrying.
        status = clipservice.read_status(self.paths, lambda: self.now)
        self.assertEqual(status["state"], "error")
        self.assertIn("disk", status["message"])
        self.assertTrue(service.step(0))
        self.assertEqual(self.texts(), ["still captured"])
        self.assertEqual(
            clipservice.read_status(self.paths, lambda: self.now)["state"], "capturing"
        )

    def test_a_failure_scheduling_the_sync_never_stops_capture(self):
        class Broken:
            state = "off"
            synced = 0.0

            def step(self, now):
                raise OSError("key file unreadable")

            def poke(self, now):
                pass

        service = self.service(clipwatch.Clip(text="kept"))
        service._syncer = Broken()  # type: ignore[assignment]
        service.step(0)
        self.assertEqual(self.texts(), ["kept"])


class OwnWriteTests(ServiceCase):
    def test_the_transcript_dictation_just_copied_is_not_captured_again(self):
        clipservice.mark_own_write(self.paths, "my transcript", clock=lambda: self.now)
        service = self.service(clipwatch.Clip(text="my transcript"))
        service.step(0)
        self.assertEqual(self.texts(), [])

    def test_the_marker_expires_and_only_covers_its_own_text(self):
        clipservice.mark_own_write(self.paths, "my transcript", clock=lambda: self.now)
        service = self.service(
            clipwatch.Clip(text="something else"), clipwatch.Clip(text="my transcript")
        )
        service.step(0)
        self.now += clipservice.OWN_WRITE_SECONDS + 1
        service.step(0)
        self.assertEqual(sorted(self.texts()), ["my transcript", "something else"])

    def test_a_damaged_marker_is_ignored(self):
        (self.paths.runtime / "clip-ignore.json").write_text("{not json")
        self.service(clipwatch.Clip(text="hello")).step(0)
        self.assertEqual(self.texts(), ["hello"])

    def test_marking_creates_the_runtime_folder_if_needed(self):
        for entry in self.paths.runtime.iterdir():
            entry.unlink()
        self.paths.runtime.rmdir()
        clipservice.mark_own_write(self.paths, "x")
        self.assertTrue((self.paths.runtime / "clip-ignore.json").is_file())


class TranscriptTests(ServiceCase):
    def test_a_transcript_joins_the_history_and_is_marked_as_the_apps_own_write(self):
        hotkeys.Preferences(self.paths).save(features=hotkeys.Features(True, True))
        clipservice.record_transcript(self.paths, "spoken words", clock=lambda: self.now)
        item = self.store.list()[0]
        self.assertEqual((item.text, item.source), ("spoken words", "dictation"))
        service = self.service(clipwatch.Clip(text="spoken words"))
        service.step(0)  # The watcher then sees the copy the engine made.
        self.assertEqual(self.store.count(), 1)

    def test_nothing_is_recorded_when_the_clipboard_feature_is_off(self):
        hotkeys.Preferences(self.paths).save(features=hotkeys.Features(True, False))
        clipservice.record_transcript(self.paths, "spoken words")
        self.assertFalse(self.paths.clipboard.exists() and self.store.count())

    def test_a_broken_history_never_fails_a_dictation(self):
        hotkeys.Preferences(self.paths).save(features=hotkeys.Features(True, True))
        with patch.object(
            clipservice.clipstore, "Store", side_effect=sqlite3.OperationalError("x")
        ):
            clipservice.record_transcript(self.paths, "spoken words")
        with patch.object(clipservice.clipstore, "Store", side_effect=clipstore.StoreError("new")):
            clipservice.record_transcript(self.paths, "spoken words")

    def test_finishing_a_dictation_records_the_transcript_before_copying_it(self):
        order: list[str] = []
        paths = self.paths
        paths.audio.write_bytes(b"pcm")
        config = d.Config(paths)
        with (
            patch.object(d, "transcribe", return_value="spoken words"),
            patch.object(d, "copy_text", side_effect=lambda *a, **k: order.append("copy")),
            patch.object(d, "notify"),
            patch.object(
                clipservice, "record_transcript", side_effect=lambda p, t: order.append("record")
            ),
        ):
            d.finish(config, paths)
        self.assertEqual(order, ["record", "copy"])

    def test_dictation_still_works_when_the_history_module_is_missing(self):
        paths = self.paths
        paths.audio.write_bytes(b"pcm")
        with (
            patch.object(d, "transcribe", return_value="spoken words"),
            patch.object(d, "copy_text") as copy,
            patch.object(d, "notify"),
            patch.dict(sys.modules, {"clipservice": None}),
        ):
            d.finish(d.Config(paths), paths)
        copy.assert_called_once()


class ResilienceTests(ServiceCase):
    def test_a_watcher_error_is_reported_and_capture_continues(self):
        service = self.service(OSError("display closed"), clipwatch.Clip(text="later"))
        service.step(0)
        self.assertEqual(
            clipservice.read_status(self.paths, clock=lambda: self.now)["state"], "error"
        )
        service.step(0)
        self.assertEqual(self.texts(), ["later"])
        self.assertEqual(
            clipservice.read_status(self.paths, clock=lambda: self.now)["state"], "capturing"
        )

    def test_an_unavailable_watcher_carries_its_reason_into_the_status(self):
        service = self.service(clipwatch.Unavailable("No clipboard access on this desktop."))
        with self.assertRaises(clipwatch.Unavailable):
            service.step(0)
        status = clipservice.read_status(self.paths, clock=lambda: self.now)
        self.assertEqual(status["state"], "error")
        self.assertIn("No clipboard access", status["message"])

    def test_a_locked_database_is_reported_not_fatal(self):
        service = self.service(clipwatch.Clip(text="a"), clipwatch.Clip(text="b"))
        with patch.object(self.store, "add_text", side_effect=sqlite3.OperationalError("locked")):
            service.step(0)
        self.assertEqual(
            clipservice.read_status(self.paths, clock=lambda: self.now)["state"], "error"
        )
        service.step(0)
        self.assertEqual(self.texts(), ["b"])


class MaintenanceTests(ServiceCase):
    def test_retention_runs_on_a_schedule(self):
        self.settings = hotkeys.ClipboardSettings(keep_items=50, keep_days=1)
        for number in range(60):
            self.store.add_text(f"old {number}", now=self.now - 100 - number)
        service = self.service(None, None)
        service.step(0)
        self.assertEqual(self.store.count(), 60)  # The first pass only schedules the next.
        self.now += clipservice.PRUNE_SECONDS + 1
        service.step(0)
        self.assertEqual(self.store.count(), 50)

    def test_status_reports_the_count_and_the_pause(self):
        service = self.service(clipwatch.Clip(text="one"))
        service.step(0)
        status = clipservice.read_status(self.paths, clock=lambda: self.now)
        self.assertEqual((status["state"], status["count"]), ("capturing", 1))
        self.settings = hotkeys.ClipboardSettings(paused_until=-1)
        service.step(0)
        self.assertEqual(
            clipservice.read_status(self.paths, clock=lambda: self.now)["state"], "paused"
        )

    def test_status_is_not_rewritten_on_every_idle_step(self):
        service = self.service()
        service.step(0)
        target = self.paths.runtime / "clip-status.json"
        first = target.stat().st_mtime_ns
        for _ in range(5):
            service.step(0)
        self.assertEqual(target.stat().st_mtime_ns, first)
        self.now += clipservice.STATUS_SECONDS + 1  # A heartbeat lets others see it is alive.
        service.step(0)
        self.assertGreater(json.loads(target.read_text())["updated"], self.now - 5)

    def test_a_stale_status_reads_as_stopped(self):
        self.service().step(0)
        later = self.now + clipservice.STALE_SECONDS + 1
        self.assertEqual(
            clipservice.read_status(self.paths, clock=lambda: later)["state"], "stopped"
        )

    def test_a_missing_status_reads_as_stopped(self):
        self.assertEqual(clipservice.read_status(self.paths)["state"], "stopped")


class RunTests(ServiceCase):
    def test_a_second_service_does_not_start(self):
        held = desktop.lock(self.paths.runtime / "clipservice.lock")
        self.addCleanup(os.close, held)
        factory_calls: list[int] = []
        result = clipservice.run(self.paths, watcher_factory=lambda: factory_calls.append(1))
        self.assertEqual(result, 0)
        self.assertEqual(factory_calls, [])
        self.assertTrue(clipservice.running(self.paths))

    def test_it_runs_until_asked_to_quit_and_releases_everything(self):
        hotkeys.Preferences(self.paths).save(features=hotkeys.Features(True, True))
        watcher = FakeWatcher(clipwatch.Clip(text="captured"))
        original = watcher.next_change

        def next_change(timeout):
            clip = original(timeout)
            if watcher.calls >= 3:
                (self.paths.runtime / "clip-quit").write_text("quit")
            return clip

        watcher.next_change = next_change  # type: ignore[method-assign]
        self.assertEqual(
            clipservice.run(self.paths, watcher_factory=lambda: watcher, sleep=lambda s: None), 0
        )
        self.assertTrue(watcher.closed)
        self.assertFalse((self.paths.runtime / "clip-quit").exists())
        self.assertFalse(clipservice.running(self.paths))
        again = clipstore.Store(self.paths.clipboard)
        self.addCleanup(again.close)
        self.assertEqual([i.text for i in again.list()], ["captured"])

    def test_it_exits_when_the_clipboard_feature_is_turned_off(self):
        hotkeys.Preferences(self.paths).save(features=hotkeys.Features(True, False))
        watcher = FakeWatcher()
        self.assertEqual(
            clipservice.run(self.paths, watcher_factory=lambda: watcher, sleep=lambda s: None), 0
        )
        self.assertEqual(watcher.calls, 0)

    def test_an_unavailable_watcher_is_retried_and_reported(self):
        hotkeys.Preferences(self.paths).save(features=hotkeys.Features(False, True))
        attempts: list[int] = []
        working = FakeWatcher()

        def factory():
            attempts.append(1)
            if len(attempts) < 3:
                raise clipwatch.Unavailable("Clipboard access is not available yet.")
            (self.paths.runtime / "clip-quit").write_text("quit")
            return working

        naps: list[float] = []
        self.assertEqual(
            clipservice.run(
                self.paths, watcher_factory=factory, sleep=naps.append, retry_seconds=30.0
            ),
            0,
        )
        self.assertEqual(len(attempts), 3)
        self.assertEqual(naps[:2], [30.0, 30.0])

    def test_preferences_are_read_again_only_when_the_file_changes(self):
        prefs = hotkeys.Preferences(self.paths)
        prefs.save(features=hotkeys.Features(True, True))
        cache = clipservice.CachedPreferences(prefs)
        with patch.object(prefs, "read", wraps=prefs.read) as read:
            for _ in range(5):
                self.assertTrue(cache.enabled())
                cache.settings()
            self.assertLessEqual(read.call_count, 2)
            prefs.save(features=hotkeys.Features(True, False))
            os.utime(prefs.path, (self.now, self.now + 5))  # Make the change visible.
            self.assertFalse(cache.enabled())


if __name__ == "__main__":
    unittest.main()
