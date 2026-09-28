"""The service runs the sync engine: when, how often, and never on the capture thread."""

from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import clipboardplus as cp
import clipservice
import clipstore
import clipsync
import clipwatch
import dictation as d
import hotkeys
from test_clipservice import FakeWatcher, ServiceCase

KEY = "cp_live_" + "a1b2c3d4" * 6
OTHER_KEY = "cp_live_" + "e5f6a7b8" * 6


class FakeEngine:
    def __init__(self, key: str) -> None:
        self.key = key
        self.runs = 0
        self.state = "idle"
        self.delay: float | None = None
        self.gate: threading.Event | None = None
        self.started = threading.Event()

    def run_once(self) -> clipsync.Report:
        self.runs += 1
        self.started.set()
        if self.gate is not None:
            self.gate.wait(5)
        if self.state == "idle":
            self.state = "ok"
        return clipsync.Report()

    def retry_delay(self) -> float | None:
        return self.delay


class SyncCase(ServiceCase):
    def setUp(self):
        super().setUp()
        self.engines: list[FakeEngine] = []
        self.config_dir = self.paths.config.parent
        d.private_dir(self.config_dir)

    def syncer(self, *, threaded: bool = False) -> clipservice.Syncer:
        def factory(store: clipstore.Store, key: str) -> FakeEngine:
            engine = FakeEngine(key)
            engine.gate = getattr(self, "gate", None)
            self.engines.append(engine)
            return engine

        return clipservice.Syncer(
            self.store,
            self.paths,
            clock=lambda: self.now,
            engine_factory=factory,  # type: ignore[arg-type]
            threaded=threaded,
        )

    def link(self, key: str = KEY) -> None:
        cp.save_key(self.config_dir, key)


class ScheduleTests(SyncCase):
    def test_nothing_runs_without_a_key(self):
        syncer = self.syncer()
        syncer.step(self.now)
        self.assertEqual(self.engines, [])
        self.assertEqual(syncer.state, "off")

    def test_a_linked_account_syncs_at_once_then_every_minute(self):
        self.link()
        syncer = self.syncer()
        syncer.step(self.now)
        self.assertEqual(self.engines[0].runs, 1)
        self.assertEqual(self.engines[0].key, KEY)
        self.now += 59
        syncer.step(self.now)
        self.assertEqual(self.engines[0].runs, 1)
        self.now += 2
        syncer.step(self.now)
        self.assertEqual(self.engines[0].runs, 2)
        self.assertEqual(syncer.state, "ok")

    def test_a_local_change_brings_the_next_round_forward_to_five_seconds(self):
        self.link()
        syncer = self.syncer()
        syncer.step(self.now)
        self.now += 10
        syncer.poke(self.now)
        self.now += 4
        syncer.step(self.now)
        self.assertEqual(self.engines[0].runs, 1)
        self.now += 2
        syncer.step(self.now)
        self.assertEqual(self.engines[0].runs, 2)

    def test_a_poke_never_delays_a_round_that_is_already_due_sooner(self):
        self.link()
        syncer = self.syncer()
        syncer.step(self.now)
        self.now += 55
        syncer.poke(self.now)  # Due in 5 s anyway (at +60): the earlier time stays.
        self.now += 5
        syncer.step(self.now)
        self.assertEqual(self.engines[0].runs, 2)

    def test_sync_now_runs_at_once_and_the_request_is_consumed(self):
        self.link()
        syncer = self.syncer()
        syncer.step(self.now)
        request = self.paths.runtime / "clip-sync-now"
        request.write_text("")
        self.now += 1
        syncer.step(self.now)
        self.assertEqual(self.engines[0].runs, 2)
        self.assertFalse(request.exists())
        self.now += 1
        syncer.step(self.now)
        self.assertEqual(self.engines[0].runs, 2)

    def test_a_failure_waits_for_the_engines_backoff(self):
        self.link()
        syncer = self.syncer()
        syncer.step(self.now)
        engine = self.engines[0]
        engine.state, engine.delay = "offline", 30.0
        self.now += 60
        syncer.step(self.now)  # The round after this one is the failing one.
        self.now += 29
        syncer.step(self.now)
        self.assertEqual(engine.runs, 2)
        self.now += 2
        syncer.step(self.now)
        self.assertEqual(engine.runs, 3)
        self.assertEqual(syncer.state, "offline")

    def test_unlinking_stops_syncing_and_relinking_starts_afresh(self):
        self.link()
        syncer = self.syncer()
        syncer.step(self.now)
        cp.remove_key(self.config_dir)
        self.now += 100
        syncer.step(self.now)
        self.assertEqual((self.engines[0].runs, syncer.state), (1, "off"))
        self.link(OTHER_KEY)
        self.now += 1
        syncer.step(self.now)
        self.assertEqual([e.key for e in self.engines], [KEY, OTHER_KEY])
        self.assertEqual(self.engines[1].runs, 1)

    def test_a_refused_key_stops_until_a_new_key_is_saved(self):
        self.link()
        syncer = self.syncer()
        syncer.step(self.now)
        self.engines[0].state = "auth"
        self.now += 100
        syncer.step(self.now)
        runs = self.engines[0].runs
        for _ in range(3):
            self.now += 1000
            syncer.step(self.now)
        self.assertEqual(self.engines[0].runs, runs)
        self.assertEqual(syncer.state, "auth")
        self.link(OTHER_KEY)
        self.now += 1
        syncer.step(self.now)
        self.assertEqual(self.engines[-1].key, OTHER_KEY)
        self.assertEqual(self.engines[-1].runs, 1)
        self.assertEqual(syncer.state, "ok")

    def test_saving_the_same_key_again_also_reconnects(self):
        self.link()
        syncer = self.syncer()
        syncer.step(self.now)
        self.engines[0].state = "auth"
        self.now += 100
        syncer.step(self.now)
        cp.save_key(self.config_dir, KEY)
        self.now += 1
        syncer.step(self.now)
        self.assertEqual(len(self.engines), 2)
        self.assertEqual(syncer.state, "ok")

    def test_a_damaged_key_file_counts_as_not_linked(self):
        cp.key_path(self.config_dir).write_text("not a key")
        syncer = self.syncer()
        syncer.step(self.now)
        self.assertEqual((self.engines, syncer.state), ([], "off"))


class CrashTests(SyncCase):
    def test_a_round_that_crashes_is_reported_and_not_retried_every_second(self):
        self.link()
        syncer = self.syncer()
        syncer.step(self.now)
        engine = self.engines[0]

        def crash() -> clipsync.Report:
            engine.runs += 1
            raise RuntimeError("unexpected server data")

        engine.run_once = crash  # type: ignore[method-assign]
        self.now += 61
        syncer.step(self.now)
        self.assertEqual((engine.runs, syncer.state), (2, "error"))
        for _ in range(30):
            self.now += 1
            syncer.step(self.now)
        self.assertEqual(engine.runs, 2)  # Not a round per second.
        self.now += 600
        syncer.step(self.now)
        self.assertEqual(engine.runs, 3)

    def test_a_crash_on_the_thread_is_survived_too(self):
        self.link()
        syncer = self.syncer(threaded=True)
        syncer.step(self.now)
        syncer.wait(5)
        engine = self.engines[0]

        def crash() -> clipsync.Report:
            raise ValueError("bad")

        engine.run_once = crash  # type: ignore[method-assign]
        self.now += 61
        syncer.step(self.now)
        syncer.wait(5)
        self.assertEqual(syncer.state, "error")


class AccountSwitchTests(SyncCase):
    def linked_item(self):
        item = self.store.add_text("mine", now=1.0)
        self.store.mark_pushed(item.id, "text|k|mine")
        self.store.link(item.id, "cloud-old", False)
        self.store.meta_set("sync_cursor", "5.0")
        return item

    def test_removing_the_key_forgets_the_old_account_even_after_a_late_round(self):
        self.link()
        syncer = self.syncer()
        syncer.step(self.now)
        item = self.linked_item()  # A round finishing after the app reset things.
        cp.remove_key(self.config_dir)
        self.now += 5
        syncer.step(self.now)
        kept = self.store.get(item.id)
        self.assertEqual((kept.cloud_id, kept.cloud_key, kept.dirty), ("", "", True))
        self.assertEqual(self.store.meta_get("sync_cursor"), "")

    def test_a_key_that_was_never_linked_touches_nothing(self):
        item = self.linked_item()
        syncer = self.syncer()
        syncer.step(self.now)  # No key at all, and no earlier engine.
        self.assertEqual(self.store.get(item.id).cloud_id, "cloud-old")

    def test_immediate_account_switch_forgets_an_in_flight_rounds_old_ids(self):
        self.link()
        syncer = self.syncer(threaded=True)
        item = self.store.add_text("mine", now=1.0)
        syncer.step(self.now)
        syncer.wait(5)
        old_engine = self.engines[0]
        entered = threading.Event()
        release = threading.Event()

        def finish_old_round() -> clipsync.Report:
            entered.set()
            release.wait(5)
            self.store.mark_pushed(item.id, "text|old|mine")
            self.store.link(item.id, "cloud-old", False)
            self.store.meta_set(clipstore.META_CURSOR, "5.0")
            return clipsync.Report()

        old_engine.run_once = finish_old_round  # type: ignore[method-assign]
        self.now += 61
        syncer.step(self.now)
        self.assertTrue(entered.wait(5))
        # Release the old worker after the window has reset metadata and saved the
        # replacement key; the service has not yet observed an unlinked interval.
        self.store.reset_sync()
        self.link(OTHER_KEY)
        release.set()
        syncer.wait(5)
        self.assertEqual(self.store.get(item.id).cloud_id, "cloud-old")
        self.now += 1
        syncer.step(self.now)
        syncer.wait(5)
        kept = self.store.get(item.id)
        self.assertEqual((kept.cloud_id, kept.cloud_key, kept.dirty), ("", "", True))
        self.assertEqual(self.store.meta_get(clipstore.META_CURSOR), "")
        self.assertEqual(self.engines[1].key, OTHER_KEY)


class ThreadTests(SyncCase):
    def test_a_slow_round_never_blocks_the_caller_and_never_overlaps(self):
        self.link()
        syncer = self.syncer(threaded=True)
        self.gate = gate = threading.Event()
        syncer.step(self.now)  # Creates the engine and starts a round in the background.
        engine = self.engines[0]
        self.assertTrue(engine.started.wait(5))
        self.now += 500
        syncer.step(self.now)  # Still running: must return at once, starting nothing new.
        syncer.step(self.now)
        self.assertEqual(engine.runs, 1)
        self.assertEqual(syncer.state, "syncing")
        gate.set()
        syncer.wait(5)
        self.assertEqual(syncer.state, "ok")
        self.now += 100
        syncer.step(self.now)
        syncer.wait(5)
        self.assertEqual(engine.runs, 2)

    def test_closing_waits_for_a_running_round(self):
        self.link()
        syncer = self.syncer(threaded=True)
        syncer.step(self.now)
        syncer.close()
        self.assertEqual(self.engines[0].runs, 1)


class ServiceWiringTests(SyncCase):
    def service_with(self, syncer: clipservice.Syncer, *events: clipwatch.Clip):
        self.watcher = FakeWatcher(*events)
        return clipservice.Service(
            self.store,
            self.watcher,
            lambda: self.settings,
            lambda: self.enabled,
            self.paths,
            clock=lambda: self.now,
            syncer=syncer,
        )

    def test_a_capture_pokes_the_syncer_and_the_status_reports_the_sync(self):
        self.link()
        syncer = self.syncer()
        service = self.service_with(syncer, clipwatch.Clip(text="hello"))
        service.step(0)  # Captures "hello"; the syncer runs its first round.
        self.assertEqual(self.engines[0].runs, 1)
        self.now += 10
        service = self.service_with(syncer, clipwatch.Clip(text="again"))
        service.step(0)
        self.now += 6
        service = self.service_with(syncer)
        service.step(0)
        self.assertEqual(self.engines[0].runs, 2)
        status = clipservice.read_status(self.paths, lambda: self.now)
        self.assertEqual(status["sync"], "ok")
        self.assertGreater(status["synced"], 0)

    def test_nothing_captured_means_no_poke(self):
        self.link()
        syncer = self.syncer()
        service = self.service_with(syncer)
        service.step(0)
        self.now += 30
        service.step(0)
        self.assertEqual(self.engines[0].runs, 1)

    def test_an_unlinked_service_reports_sync_off(self):
        service = self.service_with(self.syncer(), clipwatch.Clip(text="x"))
        service.step(0)
        status = clipservice.read_status(self.paths, lambda: self.now)
        self.assertEqual(status["sync"], "off")

    def test_a_dictation_transcript_asks_for_an_immediate_sync_when_linked(self):
        self.link()
        hotkeys.Preferences(self.paths).save(features=hotkeys.Features(True, True))
        clipservice.record_transcript(self.paths, "spoken words", lambda: self.now)
        self.assertTrue((self.paths.runtime / "clip-sync-now").exists())

    def test_an_unlinked_transcript_leaves_no_request(self):
        hotkeys.Preferences(self.paths).save(features=hotkeys.Features(True, True))
        clipservice.record_transcript(self.paths, "spoken words", lambda: self.now)
        self.assertFalse((self.paths.runtime / "clip-sync-now").exists())


if __name__ == "__main__":
    unittest.main()
