"""Deleting and clearing stay in step between this device and the Clipboard+ account.

Everything runs on the real store and the real sync engine against a fake account, so it
holds on macOS, Windows and Linux alike.
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import app_service
import clipboardplus as cp
import clipstore
import clipsync
import test_app
import test_clipsync as base

MINUTE = base.MINUTE
HOUR = 60 * MINUTE


class Account(base.SyncCase):
    """A store, an engine and a fake account, with helpers to age synced items."""

    def setUp(self):
        super().setUp()
        self.now = base.START

    def synced(self, *texts: str, starred: tuple[str, ...] = ()) -> None:
        """Copy these here, send them, and let them age past the 10-minute grace."""
        for number, text in enumerate(texts):
            item = self.store.add_text(text, now=self.now - 1000 + number)
            if text in starred:
                self.store.set_favorite(item.id, True)
        self.engine.run_once()
        self.advance(40 * MINUTE)

    def lists(self) -> int:
        return self.cloud.names().count("list")


class WebDeletionsReachTheDesktop(Account):
    def test_a_single_web_delete_arrives_through_the_pull(self):
        self.synced("keep", "drop")
        self.cloud.web_delete(next(i for i in self.cloud.items.values() if i.text == "drop"))
        self.engine.run_once()
        self.assertEqual(self.texts(), ["keep"])
        self.assertEqual(self.store.tombstones(), [])  # The account is not told again.

    def test_a_single_web_delete_arrives_by_reconciliation_when_the_pull_missed_it(self):
        self.synced("keep", "drop")
        self.cloud.web_delete(
            next(i for i in self.cloud.items.values() if i.text == "drop"), tell=False
        )
        self.cloud.calls.clear()
        report = self.engine.run_once()
        self.assertEqual(self.texts(), ["keep"])
        self.assertEqual(report.removed, 1)
        self.assertEqual(self.store.tombstones(), [])
        self.assertNotIn("delete", self.cloud.names())

    def test_a_web_clear_all_empties_the_desktop(self):
        self.synced("one", "two", "three")
        self.cloud.items.clear()
        report = self.engine.run_once()
        self.assertEqual((self.texts(), report.removed), ([], 3))
        self.assertNotIn("delete", self.cloud.names())

    def test_a_large_web_clear_all_is_checked_against_a_full_pull_first(self):
        self.synced(*[f"clip {n}" for n in range(30)])
        self.cloud.items.clear()
        self.cloud.calls.clear()
        report = self.engine.run_once()
        self.assertEqual((self.store.count(), report.removed), (0, 30))
        self.assertIn(("pull", None), self.cloud.calls)

    def test_a_bulk_removal_the_full_pull_contradicts_is_refused(self):
        self.synced(*[f"clip {n}" for n in range(30)])
        # The listing claims an empty account, but the pull still returns everything.
        self.cloud.listings = [cp.Listing(frozenset(), True)]
        report = self.engine.run_once()
        self.assertEqual((self.store.count(), report.removed), (30, 0))

    def test_an_empty_listing_removes_a_handful_of_items_only_when_both_listings_agree(self):
        self.synced("a", "b")
        self.cloud.listings = [
            cp.Listing(frozenset(), True),
            cp.Listing(frozenset(self.cloud.items), True),
        ]
        self.assertEqual(self.engine.run_once().removed, 0)
        self.assertEqual(self.store.count(), 2)


class ReconciliationIsConservative(Account):
    def gone_from_the_web(self, *texts: str) -> None:
        for item in list(self.cloud.items.values()):
            if item.text in texts:
                self.cloud.web_delete(item, tell=False)

    def test_a_failed_listing_removes_nothing(self):
        self.synced("a", "b")
        self.gone_from_the_web("a")
        self.cloud.failures["list"] = [
            cp.SyncError("Clipboard+ answered with an error (500).", 500)
        ]
        report = self.engine.run_once()
        self.assertEqual((self.engine.state, report.removed, self.store.count()), ("ok", 0, 2))

    def test_an_offline_listing_fails_the_round_and_is_retried(self):
        self.synced("a", "b")
        self.gone_from_the_web("a")
        self.cloud.failures["list"] = [cp.SyncError("Couldn’t reach Clipboard+.")]
        self.engine.run_once()
        self.assertEqual((self.engine.state, self.store.count()), ("offline", 2))
        self.advance(MINUTE)
        self.engine.run_once()  # Not counted as a check: it runs again at once.
        self.assertEqual(self.texts(), ["b"])

    def test_a_partial_or_capped_listing_removes_nothing(self):
        self.synced("a", "b")
        self.cloud.listings = [cp.Listing(frozenset(), False)]
        self.assertEqual(self.engine.run_once().removed, 0)
        self.assertEqual(self.store.count(), 2)

    def test_two_listings_that_disagree_remove_only_what_both_lack(self):
        self.synced("a", "b")
        a, b = sorted(self.cloud.items.values(), key=lambda i: i.text)
        self.cloud.listings = [
            cp.Listing(frozenset(), True),  # First read: nothing at all.
            cp.Listing(frozenset({b.id}), True),  # Paging skipped nothing this time: b is there.
        ]
        self.engine.run_once()
        self.assertEqual(self.texts(), ["b"])

    def test_a_copy_confirmed_less_than_ten_minutes_ago_is_never_removed(self):
        self.synced("old")
        self.store.add_text("fresh", now=self.now - 30)
        self.engine.run_once()  # Sends it; the account then "lacks" it (a slow replica).
        for item in list(self.cloud.items.values()):
            if item.text == "fresh":
                self.cloud.items.pop(item.id)
        self.advance(5 * MINUTE)
        self.store.meta_set(clipstore.META_RECONCILED, "")
        self.assertEqual(self.engine.run_once().removed, 0)
        self.assertEqual(sorted(self.texts()), ["fresh", "old"])

    def test_items_with_unsent_changes_are_never_removed(self):
        self.synced("edited", "unsent", "labelled")
        self.store.set_favorite(self.local("edited").id, True)  # Star not sent yet.
        self.store.add_text("brand new", now=self.now)  # Never sent (also has no account id).
        self.cloud.items.clear()
        self.cloud.failures["toggle"] = [cp.SyncError("down")]
        self.engine.run_once()
        self.assertIn("edited", self.texts())
        self.assertIn("brand new", self.texts())

    def test_it_runs_at_most_every_thirty_minutes_unless_asked(self):
        self.synced("a")
        self.engine.run_once()  # Due: the first check with something to compare.
        checks = self.lists()
        self.assertGreater(checks, 0)
        self.advance(10 * MINUTE)
        self.engine.run_once()
        self.assertEqual(self.lists(), checks)
        self.advance(25 * MINUTE)
        self.engine.run_once()
        self.assertGreater(self.lists(), checks)

    def test_sync_now_asks_for_a_check_at_once(self):
        self.synced("a")
        self.engine.run_once()
        checks = self.lists()
        self.gone_from_the_web("a")
        self.engine.run_once()
        self.assertEqual((self.lists(), self.texts()), (checks, ["a"]))
        clipsync.request_reconcile(self.store)
        self.engine.run_once()
        self.assertEqual(self.texts(), [])
        self.assertEqual(self.store.meta_get(clipstore.META_RECONCILE_NOW), "")

    def test_ids_match_whatever_their_case(self):
        self.synced("a")
        (item,) = self.cloud.items.values()
        self.cloud.listings = [cp.Listing(frozenset({item.id.upper()}), True)]
        self.engine.run_once()
        self.assertEqual(self.texts(), ["a"])

    def test_a_new_account_starts_with_nothing_to_reconcile(self):
        self.synced("a")
        self.store.reset_sync()
        self.assertEqual(self.store.reconcile_candidates(self.now + HOUR), [])


class RetentionIsLocalOnly(Account):
    def test_pruned_items_stay_on_the_account_and_do_not_come_back(self):
        for number in range(4):
            self.store.add_text(f"old {number}", now=self.now - 5000 + number)
        self.store.add_text("recent", now=self.now - 100)
        self.engine.run_once()
        self.advance(40 * MINUTE)
        self.assertEqual(len(self.cloud.items), 5)
        pruned = self.store.prune(keep_items=1, keep_days=365, now=self.now)
        self.assertEqual(pruned, 4)
        self.cloud.calls.clear()
        for _ in range(3):
            self.store.meta_set(clipsync.CURSOR, "")  # Even a full pull must not re-add them.
            self.advance(MINUTE)
            report = self.engine.run_once()
            self.assertEqual((report.pulled, report.removed), (0, 0))
        self.assertEqual(self.texts(), ["recent"])
        self.assertEqual(len(self.cloud.items), 5)  # The account still has all of them.
        self.assertNotIn("delete", self.cloud.names())
        self.assertEqual(self.store.tombstones(), [])

    def test_a_favorite_on_the_account_still_arrives_after_a_prune(self):
        for number in range(3):
            self.store.add_text(f"old {number}", now=self.now - 5000 + number)
        self.engine.run_once()
        self.store.prune(keep_items=0, keep_days=365, now=self.now)
        starred = self.cloud.add("starred long ago", ts=self.now - 9000, favorite=True)
        self.store.meta_set(clipsync.CURSOR, "")
        self.engine.run_once()
        self.assertEqual(self.texts(), ["starred long ago"])
        self.assertEqual(self.local("starred long ago").cloud_id, starred.id)


class UndoKeepsTheAccountConsistent(Account):
    def test_undo_before_the_account_heard_cancels_the_deletion(self):
        self.synced("precious")
        item = self.local("precious")
        self.store.delete(item.id)
        restored = self.store.restore(item)
        assert restored is not None
        self.cloud.calls.clear()
        self.engine.run_once()
        self.assertEqual(self.store.tombstones(), [])
        self.assertEqual(len(self.cloud.items), 1)
        self.assertNotIn("delete", self.cloud.names())
        self.assertNotIn("push", self.cloud.names())
        self.assertEqual(self.local("precious").cloud_id, item.cloud_id)
        self.assertEqual(restored.created_at, item.created_at)

    def test_undo_after_the_account_deleted_it_uploads_it_again(self):
        self.synced("precious")
        item = self.local("precious")
        self.store.delete(item.id)
        self.engine.run_once()
        self.assertEqual(self.cloud.items, {})
        self.assertIsNotNone(self.store.restore(item))
        self.engine.run_once()
        self.assertEqual([i.text for i in self.cloud.items.values()], ["precious"])
        self.assertTrue(self.local("precious").cloud_id)

    def test_undoing_a_deleted_favorite_keeps_its_star_and_label(self):
        self.synced("fav", starred=("fav",))
        self.store.set_label(self.local("fav").id, "pinned")
        self.engine.run_once()
        item = self.local("fav")
        self.store.delete(item.id)
        restored = self.store.restore(item)
        assert restored is not None
        self.engine.run_once()
        self.assertEqual((restored.favorite, restored.label), (True, "pinned"))
        (remote,) = self.cloud.items.values()
        self.assertEqual((remote.favorite, remote.label), (True, "pinned"))

    def test_an_undone_image_comes_back_as_it_was(self):
        from support import make_png

        image = self.store.add_image(make_png(6, 6), now=self.now - 100)
        assert image is not None
        data = self.store.image_path(image).read_bytes()
        self.store.delete(image.id)
        restored = self.store.restore(image, data)
        assert restored is not None
        self.assertEqual((restored.kind, restored.created_at), ("image", image.created_at))


class FavoritesAndLabelsStillSync(Account):
    def test_star_and_label_reach_the_account_and_web_changes_come_back(self):
        self.synced("a")
        item = self.local("a")
        self.store.set_favorite(item.id, True)
        self.store.set_label(item.id, "name")
        self.advance(MINUTE)
        self.engine.run_once()
        (remote,) = self.cloud.items.values()
        self.assertEqual((remote.favorite, remote.label), (True, "name"))
        self.cloud.toggle_favorite(remote.id)  # Unstarred on the web.
        self.advance(MINUTE)
        self.engine.run_once()
        self.assertFalse(self.local("a").favorite)


class ClearingEverywhere(test_app.ServiceCase):
    """`Service.clear_clipboard` against the real engine and a fake account."""

    KEY = "cp_live_" + "a1b2c3d4" * 6

    def setUp(self):
        super().setUp()
        self.now = time.time()
        self.cloud = base.FakeCloud(lambda: self.now)
        self.store = clipstore.Store(self.paths.clipboard)
        self.addCleanup(self.store.close)
        self.engine = clipsync.Engine(
            self.store, self.cloud, clock=lambda: self.now, jitter=lambda: 0.5
        )
        with patch.object(app_service.clipboardplus, "verify", return_value="ok"):
            self.service.connect_clipboard_plus(self.KEY)
        self.store.reset_sync()

    def copy(self, text: str, *, star: bool = False) -> None:
        item = self.store.add_text(text, now=self.now - 500)
        if star:
            self.store.set_favorite(item.id, True)

    def round(self) -> clipsync.Report:
        self.now += MINUTE
        return self.engine.run_once()

    def history(self, *, star: str = "") -> None:
        self.copy("one")
        self.copy("two")
        self.copy("three", star=bool(star))
        self.round()
        self.assertEqual(len(self.cloud.items), 3)
        # Someone on the web added one as well.
        self.cloud.add("from the web", ts=self.now - 400)
        self.round()
        self.assertEqual(self.store.count(), 4)

    def test_everywhere_empties_the_account_and_this_device(self):
        self.history()
        count = self.service.clear_clipboard(self.store, everywhere=True, keep_favorites=False)
        self.assertEqual(count, 4)
        self.round()
        self.assertEqual((self.store.count(), self.cloud.items), (0, {}))
        self.assertEqual(self.store.meta_get(clipstore.META_CLEAR), "")
        self.assertEqual(self.service.clipboard_clear_progress(self.store), "done")
        self.assertNotIn("delete", self.cloud.names())  # One clear, not a request per item.

    def test_everywhere_can_keep_favorites_on_both_sides(self):
        self.history(star="three")
        self.service.clear_clipboard(self.store, everywhere=True, keep_favorites=True)
        self.round()
        self.assertEqual([i.text for i in self.store.list()], ["three"])
        self.assertEqual([i.text for i in self.cloud.items.values()], ["three"])

    def test_everywhere_without_keeping_favorites_removes_them_on_both_sides(self):
        self.history(star="three")
        self.service.clear_clipboard(self.store, everywhere=True, keep_favorites=False)
        self.round()
        self.assertEqual((self.store.count(), self.cloud.items), (0, {}))

    def test_this_device_only_leaves_the_account_alone_and_nothing_returns(self):
        self.history()
        self.service.clear_clipboard(self.store, everywhere=False, keep_favorites=False)
        self.assertEqual(self.store.meta_get(clipstore.META_CLEAR), "")
        for _ in range(3):
            self.store.meta_set(clipsync.CURSOR, "")  # Full pulls, as after a long absence.
            self.round()
        self.assertEqual(self.store.count(), 0)
        self.assertEqual(len(self.cloud.items), 4)
        self.assertNotIn("clear", self.cloud.names())

    def test_the_request_is_saved_before_anything_is_cleared_here(self):
        self.history()
        with patch.object(self.store, "clear", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self.service.clear_clipboard(self.store, everywhere=True, keep_favorites=False)
        self.assertEqual(self.store.meta_get(clipstore.META_CLEAR), "all")
        self.round()  # The next round finishes the account's side.
        self.assertEqual(self.cloud.items, {})
        self.assertEqual(self.store.meta_get(clipstore.META_CLEAR), "")

    def test_a_crash_after_the_local_clear_still_clears_the_account(self):
        self.history()
        self.service.clear_clipboard(self.store, everywhere=True, keep_favorites=False)
        # The process dies here; a new engine starts from the same store.
        fresh = clipsync.Engine(self.store, self.cloud, clock=lambda: self.now + MINUTE)
        fresh.run_once()
        self.assertEqual(self.cloud.items, {})

    def test_offline_then_online_completes_the_clear(self):
        self.history()
        self.service.clear_clipboard(self.store, everywhere=True, keep_favorites=False)
        self.cloud.failures["clear"] = [cp.SyncError("Couldn’t reach Clipboard+.")]
        self.round()
        self.assertEqual(self.engine.state, "offline")
        self.assertEqual(len(self.cloud.items), 4)
        self.assertEqual(self.store.meta_get(clipstore.META_CLEAR), "all")
        self.assertEqual(self.service.clipboard_clear_progress(self.store), "waiting")
        self.now += 10 * MINUTE
        self.round()
        self.assertEqual(self.cloud.items, {})
        self.assertEqual(self.service.clipboard_clear_progress(self.store), "done")

    def test_a_copy_made_during_the_clear_survives_and_syncs(self):
        self.history()
        self.service.clear_clipboard(self.store, everywhere=True, keep_favorites=False)
        fresh = self.store.add_text("copied during the clear", now=self.now + 1)
        self.round()
        self.assertEqual(self.store.count(), 1)
        self.assertEqual([i.text for i in self.cloud.items.values()], [fresh.text])

    def test_a_copy_an_in_flight_round_uploaded_is_sent_again_after_the_clear(self):
        self.history()
        self.service.clear_clipboard(self.store, everywhere=True, keep_favorites=False)
        fresh = self.store.add_text("uploaded mid-clear", now=self.now + 1)
        remote = self.cloud.add(fresh.text, ts=fresh.created_at, source=cp.SOURCE)
        self.store.mark_pushed(
            fresh.id, cp.sync_key("text", cp.created_ms(fresh.created_at), fresh.text)
        )
        self.store.link(fresh.id, remote.id, False)
        self.round()  # The clear deletes the uploaded copy; the local one is sent again.
        self.assertEqual([i.text for i in self.cloud.items.values()], [fresh.text])
        self.assertEqual(self.store.count(), 1)

    def test_progress_follows_the_account_state(self):
        self.history()
        self.service.clear_clipboard(self.store, everywhere=True, keep_favorites=True)
        account = app_service.Account
        with patch.object(self.service, "clipboard_plus_state") as state:
            for reported, expected in (
                (account("connected", "", "syncing"), "working"),
                (account("connected", "", "ok"), "working"),
                (account("connected", "", "offline"), "waiting"),
                (account("connected", "", "error"), "waiting"),
                (account("connected", "", "off"), "waiting"),
                (account("reconnect", "", "auth"), "reconnect"),
            ):
                state.return_value = reported
                self.assertEqual(self.service.clipboard_clear_progress(self.store), expected)

    def test_without_an_account_only_this_device_is_cleared_and_nothing_is_promised(self):
        self.service.disconnect_clipboard_plus()
        self.copy("solo")
        self.service.clear_clipboard(self.store, everywhere=True, keep_favorites=False)
        self.assertEqual((self.store.count(), self.store.meta_get(clipstore.META_CLEAR)), (0, ""))
        self.assertEqual(self.service.clipboard_clear_progress(self.store), "done")

    def test_deleting_data_on_this_device_never_reaches_the_account(self):
        self.history()
        self.service.delete_clipboard_data()
        self.assertEqual(len(self.cloud.items), 4)
        self.assertEqual(self.store.tombstones(), [])
        self.assertEqual(self.store.meta_get(clipstore.META_CLEAR), "")

    def test_sync_now_can_ask_for_a_reconciliation(self):
        self.service.sync_clipboard_now(reconcile=True)
        self.assertEqual(self.store.meta_get(clipstore.META_RECONCILE_NOW), "1")
        self.assertTrue((self.paths.runtime / "clip-sync-now").exists())


class SchemaUpgrade(unittest.TestCase):
    def test_a_version_2_history_gains_synced_at_and_stays_reconcilable(self):
        import contextlib
        import sqlite3
        import tempfile

        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder) / "clipboard"
            directory.mkdir()
            schema = clipstore._SCHEMA.replace(",\n    synced_at REAL NOT NULL DEFAULT 0", "")
            with contextlib.closing(sqlite3.connect(directory / "clips.db")) as raw:
                raw.executescript(schema)
                raw.execute(
                    "INSERT INTO items (kind, text, sha, created_at, updated_at, cloud_id, "
                    "cloud_key, dirty) VALUES ('text', 'old', 'abc', 1, 1, 'id-1', 'k', 0)"
                )
                raw.execute("PRAGMA user_version = 2")
                raw.commit()
            store = clipstore.Store(directory)
            try:
                (item,) = store.reconcile_candidates(1000.0)
                self.assertEqual((item.text, item.synced_at), ("old", 0.0))
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
