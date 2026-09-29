"""Two-way Clipboard+ sync: a real store against a fake account."""

from __future__ import annotations

import dataclasses
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import clipboardplus as cp
import clipstore
import clipsync
from support import make_png

START = 1_790_000_000.0
MINUTE = 60.0


class FakeCloud:
    """The account: keeps items, records every call, and can be told to fail."""

    def __init__(self, clock: Any) -> None:
        self.clock = clock
        self.items: dict[str, cp.CloudItem] = {}
        self.removed: list[cp.Removed] = []
        self.calls: list[tuple[Any, ...]] = []
        self.failures: dict[str, list[Exception]] = {}
        self.pushed: list[list[clipstore.Item]] = []
        # Listings to hand out, in order, instead of the account's real ids (the last one
        # repeats): lets a test make a page fail, be partial, or disagree with itself.
        self.listings: list[cp.Listing] = []

    def _enter(self, name: str, *detail: Any) -> None:
        self.calls.append((name, *detail))
        queue = self.failures.get(name)
        if queue:
            raise queue.pop(0)

    def names(self) -> list[str]:
        return [call[0] for call in self.calls]

    def add(
        self,
        text: str,
        *,
        ts: float,
        source: str = "Chrome",
        favorite: bool = False,
        label: str = "",
        kind: str = "text",
        updated: float | None = None,
    ) -> cp.CloudItem:
        item = cp.CloudItem(
            str(uuid.uuid4()),
            kind,
            text,
            label,
            favorite,
            source,
            cp.created_ms(ts),
            self.clock() if updated is None else updated,
        )
        self.items[item.id] = item
        return item

    def pull(self, since: float | None) -> cp.Pull:
        self._enter("pull", since)
        items = [i for i in self.items.values() if since is None or i.updated_at > since]
        return cp.Pull(items, list(self.removed))

    def push(self, items: list[clipstore.Item]) -> None:
        self._enter("push", len(items))
        self.pushed.append(list(items))
        known = {(i.kind, i.created_ms, i.text[:200]) for i in self.items.values()}
        for item in items:
            if item.kind not in ("text", "url"):
                continue
            if (item.kind, cp.created_ms(item.created_at), item.text[:200]) in known:
                continue
            # The real account skips identical content from another source within 10 minutes.
            if any(
                other.text == item.text
                and other.source != cp.SOURCE
                and abs(other.created_ms - cp.created_ms(item.created_at)) <= 600_000
                for other in self.items.values()
            ):
                continue
            self.add(
                item.text,
                ts=item.created_at,
                source=cp.SOURCE,
                favorite=item.favorite,
                label=item.label,
                kind=item.kind,
            )

    def toggle_favorite(self, cloud_id: str) -> bool | None:
        self._enter("toggle", cloud_id)
        item = self.items.get(cloud_id)
        if item is None:
            return None
        flipped = cp.CloudItem(
            item.id,
            item.kind,
            item.text,
            "",  # The real account drops the label on every toggle.
            not item.favorite,
            item.source,
            item.created_ms,
            self.clock(),
        )
        self.items[cloud_id] = flipped
        return flipped.favorite

    def set_label(self, cloud_id: str, label: str) -> None:
        self._enter("label", cloud_id, label)
        item = self.items.get(cloud_id)
        if item is not None and item.favorite:
            self.items[cloud_id] = dataclasses.replace(item, label=label, updated_at=self.clock())

    def delete(self, cloud_id: str) -> None:
        self._enter("delete", cloud_id)
        self.items.pop(cloud_id, None)

    def clear(self, *, favorites: bool) -> None:
        self._enter("clear", favorites)
        self.items = {k: v for k, v in self.items.items() if v.favorite and not favorites}

    def list_ids(self) -> cp.Listing:
        self._enter("list")
        if self.listings:
            return self.listings.pop(0) if len(self.listings) > 1 else self.listings[0]
        return cp.Listing(frozenset(self.items), True)

    def web_delete(self, item: cp.CloudItem, *, tell: bool = True) -> None:
        """Delete on the web; `tell` also puts it in the pull's deletedItems."""
        self.items.pop(item.id, None)
        if tell:
            self.removed.append(cp.Removed(item.kind, item.created_ms, item.text[:200]))


class SyncCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = clipstore.Store(Path(temporary.name) / "clipboard")
        self.addCleanup(self.store.close)
        self.now = START
        self.cloud = FakeCloud(lambda: self.now)
        self.engine = clipsync.Engine(
            self.store, self.cloud, clock=lambda: self.now, jitter=lambda: 0.5
        )

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def texts(self) -> list[str]:
        return [item.text for item in self.store.list(limit=500)]

    def local(self, text: str) -> clipstore.Item:
        found = self.store.find_text(text)
        assert found is not None, text
        return found


class ClearedTests(SyncCase):
    def test_a_cleared_history_does_not_come_back_from_the_account(self):
        self.cloud.add("from before", ts=START - 30)
        self.store.meta_set(clipstore.META_CLEARED, repr(START - 10))
        self.cloud.add("copied later", ts=START - 5)
        self.engine.run_once()
        self.assertEqual(self.texts(), ["copied later"])

    def test_items_kept_here_still_link_after_a_clear(self):
        kept = self.store.add_text("kept", now=START - 60)
        self.store.set_favorite(kept.id, True)
        self.cloud.add("kept", ts=START - 60, favorite=True)
        self.store.meta_set(clipstore.META_CLEARED, repr(START - 10))
        self.engine.run_once()
        self.assertTrue(self.local("kept").cloud_id)

    def test_a_new_account_is_not_hidden_by_an_old_clear(self):
        self.store.meta_set(clipstore.META_CLEARED, repr(START))
        self.store.reset_sync()
        self.cloud.add("history", ts=START - 500)
        self.engine.run_once()
        self.assertEqual(self.texts(), ["history"])


class PushTests(SyncCase):
    def test_only_dirty_text_and_links_are_sent_never_images(self):
        self.store.add_text("hello", now=START - 50)
        self.store.add_text("https://example.com/", now=START - 40)
        self.store.add_image(make_png(), now=START - 30)
        report = self.engine.run_once()
        self.assertEqual(report.pushed, 2)
        sent = [item.text for batch in self.cloud.pushed for item in batch]
        self.assertEqual(sorted(sent), ["hello", "https://example.com/"])
        self.assertEqual(len(self.cloud.items), 2)

    def test_batches_are_at_most_100_and_repeating_sends_nothing_new(self):
        for number in range(250):
            self.store.add_text(f"item {number}", now=START - 1000 + number)
        self.assertEqual(self.engine.run_once().pushed, 250)
        self.assertEqual([len(batch) for batch in self.cloud.pushed], [100, 100, 50])
        self.cloud.calls.clear()
        self.advance(MINUTE)
        self.assertEqual(self.engine.run_once().pushed, 0)
        self.assertNotIn("push", self.cloud.names())
        self.assertEqual(len(self.cloud.items), 250)

    def test_an_interrupted_push_is_retried_without_duplicating(self):
        self.store.add_text("one", now=START - 20)
        self.cloud.failures["push"] = [cp.SyncError("down")]
        report = self.engine.run_once()
        self.assertEqual(report.pushed, 0)
        self.assertEqual(self.engine.state, "offline")
        self.assertEqual(len(self.store.dirty()), 1)
        self.advance(MINUTE)
        self.assertEqual(self.engine.run_once().pushed, 1)
        self.advance(MINUTE)
        self.engine.run_once()
        self.assertEqual(len(self.cloud.items), 1)
        self.assertEqual(self.store.dirty(), [])

    def test_a_refused_item_is_skipped_for_good_and_the_rest_still_go(self):
        for number in range(3):
            self.store.add_text(f"item {number}", now=START - 30 + number)
        # The batch is refused; sending them one by one finds the culprit.
        self.cloud.failures["push"] = [
            cp.SyncError("too big", 413),
            cp.SyncError("too big", 413),
        ]
        report = self.engine.run_once()
        self.assertEqual(report.pushed, 2)
        skipped = [i for i in self.store.list() if i.sync_skip]
        self.assertEqual([i.text for i in skipped], ["item 0"])
        self.assertEqual(self.store.dirty(), [])
        self.assertEqual(self.engine.state, "ok")

    def test_a_single_refused_item_is_skipped(self):
        self.store.add_text("bad", now=START - 5)
        self.cloud.failures["push"] = [cp.SyncError("nope", 400)]
        self.engine.run_once()
        self.assertTrue(self.local("bad").sync_skip)
        self.advance(MINUTE)
        self.cloud.calls.clear()
        self.engine.run_once()
        self.assertNotIn("push", self.cloud.names())

    def test_text_over_the_account_limit_is_never_sent(self):
        self.store.add_text("x" * (cp.MAX_BYTES + 1), now=START - 5)
        self.store.add_text("fine", now=START - 4)
        self.assertEqual(self.engine.run_once().pushed, 1)
        self.assertTrue(self.local("x" * (cp.MAX_BYTES + 1)).sync_skip)

    def test_a_favorite_toggled_during_the_push_is_not_lost(self):
        item = self.store.add_text("starred later", now=START - 5)
        assert item is not None
        original = self.cloud.push

        def toggled_mid_push(items: list[clipstore.Item]) -> None:
            original(items)
            self.store.set_favorite(item.id, True, now=self.now + 1)

        self.cloud.push = toggled_mid_push  # type: ignore[method-assign]
        self.engine.run_once()
        self.cloud.push = original  # type: ignore[method-assign]
        self.advance(MINUTE)
        self.engine.run_once()
        self.assertTrue(self.local("starred later").cloud_favorite)
        self.assertTrue(next(iter(self.cloud.items.values())).favorite)
        self.assertEqual(self.store.pending_favorites(), [])


class PullTests(SyncCase):
    def test_unknown_cloud_items_are_added_and_never_sent_back(self):
        self.cloud.add("from the web", ts=START - 100)
        report = self.engine.run_once()
        self.assertEqual(report.pulled, 1)
        item = self.local("from the web")
        self.assertEqual((item.source, item.dirty, item.sync_skip), ("cloud", False, False))
        self.assertTrue(item.cloud_id)
        self.assertEqual(item.created_at, cp.created_ms(START - 100) / 1000)
        self.advance(MINUTE)
        self.cloud.calls.clear()
        self.engine.run_once()
        self.assertNotIn("push", self.cloud.names())

    def test_a_pulled_link_is_a_link(self):
        self.cloud.add("https://example.com/x", ts=START - 100, kind="url")
        self.engine.run_once()
        self.assertEqual(self.local("https://example.com/x").kind, "url")

    def test_replaying_the_same_pull_changes_nothing(self):
        self.cloud.add("once", ts=START - 100, favorite=True, label="tag")
        self.engine.run_once()
        before = [(i.id, i.text, i.favorite, i.label, i.updated_at) for i in self.store.list()]
        for _ in range(3):
            self.advance(10)
            self.engine.run_once()
        after = [(i.id, i.text, i.favorite, i.label, i.updated_at) for i in self.store.list()]
        self.assertEqual(before, after)
        self.assertEqual(self.store.count(), 1)

    def test_the_pull_starts_two_minutes_before_the_last_success(self):
        self.engine.run_once()
        self.assertEqual(self.cloud.calls[-1], ("pull", None))
        self.advance(MINUTE)
        self.engine.run_once()
        self.assertEqual(self.cloud.calls[-1], ("pull", START - 120))

    def test_the_cursor_does_not_move_when_the_round_fails(self):
        self.engine.run_once()
        self.advance(MINUTE)
        self.cloud.failures["pull"] = [cp.SyncError("down")]
        self.engine.run_once()
        self.advance(MINUTE)
        self.engine.run_once()
        self.assertEqual(self.cloud.calls[-1], ("pull", START - 120))

    def test_an_item_that_matches_a_pushed_one_by_key_is_linked(self):
        self.store.add_text("shared", now=START - 100)
        self.engine.run_once()
        self.assertEqual(self.store.count(), 1)
        self.assertTrue(self.local("shared").cloud_id)
        self.assertEqual(len(self.cloud.items), 1)

    def test_a_pushed_link_the_server_rewrote_is_still_linked(self):
        # The server normalises addresses (adds a path), so the keys differ.
        item = self.store.add_text("http://example.com", now=START - 100)
        assert item is not None
        self.engine.run_once()
        (cloud_item,) = self.cloud.items.values()
        self.cloud.items[cloud_item.id] = cp.CloudItem(
            cloud_item.id,
            "url",
            "http://example.com/",
            "",
            False,
            cp.SOURCE,
            cloud_item.created_ms,
            self.now + 5,
        )
        self.advance(MINUTE)
        self.engine.run_once()
        self.assertEqual(self.store.count(), 1)
        self.assertEqual(self.store.get(item.id).cloud_id, cloud_item.id)  # type: ignore[union-attr]

    def test_identical_content_from_another_source_is_linked_not_duplicated(self):
        local = self.store.add_text("same copy", now=START - 100)
        assert local is not None
        # The extension captured the same copy 3 minutes earlier, so the account
        # refuses the app's own upload and hands back the extension's item.
        remote = self.cloud.add("same copy", ts=START - 100 - 3 * MINUTE, source="Chrome")
        self.engine.run_once()
        self.assertEqual(len(self.cloud.items), 1)
        self.assertEqual(self.store.count(), 1)
        linked = self.store.get(local.id)
        assert linked is not None
        self.assertEqual(linked.cloud_id, remote.id)
        self.assertFalse(linked.dirty)

    def test_content_already_on_this_device_is_never_added_twice(self):
        self.store.add_text("dup", now=START - 100)
        self.cloud.add("dup", ts=START - 2 * 3600)  # Two hours apart, still one item.
        self.engine.run_once()
        self.assertEqual(self.store.count(), 1)

    def test_a_second_cloud_copy_of_linked_content_is_ignored(self):
        first = self.cloud.add("twice", ts=START - 100)
        self.engine.run_once()
        self.cloud.add("twice", ts=START - 50)
        self.advance(MINUTE)
        self.engine.run_once()
        self.assertEqual(self.store.count(), 1)
        self.assertEqual(self.local("twice").cloud_id, first.id)

    def test_pulling_never_touches_the_os_clipboard(self):
        # Nothing here can write the clipboard: the module has no clipboard code at all.
        self.assertFalse(hasattr(clipsync, "desktop"))
        self.assertFalse(hasattr(clipsync, "d"))
        self.cloud.add("quiet", ts=START - 10)
        self.engine.run_once()
        self.assertEqual(self.texts(), ["quiet"])


class MergeTests(SyncCase):
    def linked(self, text: str = "note", **remote: Any) -> tuple[clipstore.Item, cp.CloudItem]:
        self.store.add_text(text, now=START - 500)
        remote_item = self.cloud.add(
            text, ts=START - 500, source=cp.SOURCE, updated=START - 400, **remote
        )
        self.engine.run_once()
        item = self.local(text)
        self.assertEqual(item.cloud_id, remote_item.id)
        self.advance(MINUTE)
        return item, remote_item

    def test_a_favorite_made_elsewhere_is_adopted(self):
        _, remote = self.linked()
        self.cloud.items[remote.id] = cp.CloudItem(
            remote.id, "text", "note", "pinned", True, remote.source, remote.created_ms, self.now
        )
        self.engine.run_once()
        item = self.local("note")
        self.assertEqual((item.favorite, item.label, item.cloud_favorite), (True, "pinned", True))
        self.assertNotIn("toggle", self.cloud.names())

    def test_a_local_favorite_change_reaches_the_account_once(self):
        item, remote = self.linked()
        self.store.set_favorite(item.id, True, now=self.now)
        self.engine.run_once()
        self.assertEqual(self.cloud.names().count("toggle"), 1)
        self.assertTrue(self.cloud.items[remote.id].favorite)
        self.assertTrue(self.local("note").cloud_favorite)
        self.advance(MINUTE)
        self.cloud.calls.clear()
        self.engine.run_once()
        self.assertNotIn("toggle", self.cloud.names())

    def test_toggle_is_not_called_when_the_account_already_agrees(self):
        item, remote = self.linked()
        self.cloud.items[remote.id] = cp.CloudItem(
            remote.id, "text", "note", "", True, remote.source, remote.created_ms, self.now - 30
        )
        self.store.set_favorite(item.id, True, now=self.now - 60)
        self.engine.run_once()
        self.assertNotIn("toggle", self.cloud.names())
        self.assertTrue(self.local("note").cloud_favorite)

    def test_the_newer_change_wins_a_conflict(self):
        item, remote = self.linked()
        # Elsewhere: starred at now+10. Here: starred then unstarred later.
        self.store.set_favorite(item.id, True, now=self.now)
        self.store.set_favorite(item.id, False, now=self.now)
        self.store.set_favorite(item.id, True, now=self.now + 5)
        self.cloud.items[remote.id] = cp.CloudItem(
            remote.id, "text", "note", "", False, remote.source, remote.created_ms, self.now + 10
        )
        self.engine.run_once()
        self.assertFalse(self.local("note").favorite)
        self.assertNotIn("toggle", self.cloud.names())

    def test_a_recopy_does_not_undo_a_favorite_made_elsewhere(self):
        _, remote = self.linked()
        self.cloud.items[remote.id] = cp.CloudItem(
            remote.id, "text", "note", "", True, remote.source, remote.created_ms, self.now - 5
        )
        self.store.add_text("note", now=self.now)  # Moves to the top, updated just now.
        self.engine.run_once()
        self.assertTrue(self.local("note").favorite)
        self.assertNotIn("toggle", self.cloud.names())

    def test_a_label_made_here_reaches_the_account(self):
        item, remote = self.linked(favorite=True)
        self.store.set_label(item.id, "Wifi password", now=self.now)
        self.engine.run_once()
        self.assertEqual(self.cloud.items[remote.id].label, "Wifi password")
        self.assertEqual(self.local("note").cloud_label, "Wifi password")
        self.advance(MINUTE)
        self.cloud.calls.clear()
        self.engine.run_once()
        self.assertNotIn("label", self.cloud.names())

    def test_a_removed_label_is_removed_on_the_account(self):
        item, remote = self.linked(favorite=True, label="old")
        self.assertEqual(self.local("note").label, "old")
        self.store.set_label(item.id, "", now=self.now)
        self.engine.run_once()
        self.assertEqual(self.cloud.items[remote.id].label, "")

    def test_a_label_on_a_newly_starred_item_follows_the_star(self):
        item, remote = self.linked()
        self.store.set_favorite(item.id, True, now=self.now)
        self.store.set_label(item.id, "keep", now=self.now)
        self.engine.run_once()
        self.assertEqual(self.cloud.names()[-2:], ["toggle", "label"])
        self.assertEqual(
            (self.cloud.items[remote.id].favorite, self.cloud.items[remote.id].label),
            (True, "keep"),
        )

    def test_a_label_changed_elsewhere_is_adopted(self):
        item, remote = self.linked(favorite=True, label="old")
        self.cloud.items[remote.id] = dataclasses.replace(
            self.cloud.items[remote.id], label="new", updated_at=self.now
        )
        self.engine.run_once()
        self.assertEqual(self.local("note").label, "new")
        self.assertNotIn("label", self.cloud.names())

    def test_a_pulled_older_copy_never_overwrites_longer_local_text(self):
        long_text = "y" * 9000
        self.store.add_text(long_text, now=START - 100)
        self.engine.run_once()
        (remote,) = self.cloud.items.values()
        self.cloud.items[remote.id] = cp.CloudItem(
            remote.id,
            "text",
            long_text[:7000],
            "x",
            False,
            remote.source,
            remote.created_ms,
            self.now + 5,
        )
        self.advance(MINUTE)
        self.engine.run_once()
        self.assertEqual(self.store.count(), 1)
        self.assertEqual(self.store.list()[0].text, long_text)


class DeleteTests(SyncCase):
    def test_a_deletion_made_elsewhere_removes_the_local_item(self):
        self.store.add_text("bye", now=START - 100)
        self.engine.run_once()
        (remote,) = self.cloud.items.values()
        del self.cloud.items[remote.id]
        self.cloud.removed.append(cp.Removed("text", remote.created_ms, "bye"))
        self.advance(MINUTE)
        report = self.engine.run_once()
        self.assertEqual(self.texts(), [])
        self.assertEqual(report.deleted, 1)
        self.assertEqual(self.store.tombstones(), [])

    def test_a_deletion_of_the_accounts_own_copy_removes_the_linked_local_item(self):
        # The account kept the extension's copy (older time); this device's own upload was
        # refused as a duplicate. Deleting the account's copy must still find our item.
        self.store.add_text("same copy", now=START - 100)
        remote = self.cloud.add("same copy", ts=START - 100 - 3 * MINUTE, source="Chrome")
        self.engine.run_once()
        self.assertEqual(self.local("same copy").cloud_id, remote.id)
        del self.cloud.items[remote.id]
        self.cloud.removed.append(cp.Removed("text", remote.created_ms, "same copy"))
        self.advance(MINUTE)
        self.engine.run_once()
        self.assertEqual(self.texts(), [])

    def test_a_remote_deletion_of_an_unknown_item_is_ignored(self):
        self.store.add_text("keep", now=START - 100)
        self.cloud.removed.append(cp.Removed("text", cp.created_ms(START - 5), "other"))
        self.engine.run_once()
        self.assertEqual(self.texts(), ["keep"])

    def test_deleting_a_linked_item_deletes_it_from_the_account(self):
        self.store.add_text("mine", now=START - 100)
        self.engine.run_once()
        item = self.local("mine")
        (remote,) = self.cloud.items.values()
        self.store.delete(item.id)
        self.advance(MINUTE)
        self.engine.run_once()
        self.assertIn(("delete", remote.id), self.cloud.calls)
        self.assertEqual(self.cloud.items, {})
        self.assertEqual(self.store.tombstones(), [])

    def test_a_delete_the_account_no_longer_knows_still_counts(self):
        self.store.add_text("gone already", now=START - 100)
        self.engine.run_once()
        item = self.local("gone already")
        self.cloud.items.clear()
        self.store.delete(item.id)
        self.advance(MINUTE)
        self.engine.run_once()
        self.assertEqual(self.store.tombstones(), [])

    def test_a_failed_delete_is_retried_and_the_item_is_not_resurrected(self):
        self.store.add_text("stubborn", now=START - 100)
        self.engine.run_once()
        item = self.local("stubborn")
        self.store.delete(item.id)
        self.cloud.failures["delete"] = [cp.SyncError("down")]
        self.advance(MINUTE)
        self.engine.run_once()
        self.assertEqual(len(self.store.tombstones()), 1)
        self.assertEqual(self.texts(), [])  # Pulled back? No: the tombstone protects it.
        self.advance(MINUTE)
        self.engine.run_once()
        self.assertEqual(self.store.tombstones(), [])
        self.assertEqual(self.texts(), [])

    def test_a_pushed_but_unlinked_item_is_found_by_a_full_pull(self):
        self.engine.run_once()  # Sets the cursor.
        original = self.cloud.push

        def push_old(items: list[clipstore.Item]) -> None:
            original(items)
            for key, value in list(self.cloud.items.items()):
                self.cloud.items[key] = cp.CloudItem(
                    value.id, value.kind, value.text, "", False, value.source,
                    value.created_ms, START - 9999,
                )  # fmt: skip

        self.cloud.push = push_old  # type: ignore[method-assign]
        self.advance(MINUTE)
        self.store.add_text("unlinked", now=self.now - 10)
        self.engine.run_once()
        item = self.local("unlinked")
        self.assertEqual(item.cloud_id, "")  # The account's copy predates the cursor.
        self.assertTrue(item.cloud_key)
        self.store.delete(item.id)
        self.advance(MINUTE)
        self.engine.run_once()
        self.assertEqual(self.cloud.items, {})
        self.assertEqual(self.store.tombstones(), [])
        self.assertIn(("pull", None), self.cloud.calls)

    def test_a_tombstone_with_no_matching_account_item_is_dropped(self):
        self.store.add_text("never arrived", now=START - 100)
        item = self.local("never arrived")
        self.store.mark_pushed(
            item.id, cp.sync_key("text", cp.created_ms(START - 100), "never arrived")
        )
        self.store.delete(item.id)
        self.assertEqual(len(self.store.tombstones()), 1)
        self.engine.run_once()
        self.assertEqual(self.store.tombstones(), [])
        self.assertNotIn("delete", self.cloud.names())


class ClearTests(SyncCase):
    def test_clear_everywhere_asks_the_account_to_clear(self):
        self.store.add_text("a", now=START - 100)
        self.engine.run_once()
        self.store.clear(keep_favorites=True)
        clipsync.request_clear(self.store, favorites=False)
        self.advance(MINUTE)
        self.engine.run_once()
        self.assertIn(("clear", False), self.cloud.calls)
        self.assertEqual(self.cloud.items, {})
        self.assertEqual(self.store.tombstones(), [])
        self.assertNotIn("delete", self.cloud.names())  # Not one request per item.
        self.cloud.calls.clear()
        self.advance(MINUTE)
        self.engine.run_once()
        self.assertNotIn("clear", self.cloud.names())

    def test_including_favorites_is_passed_through(self):
        clipsync.request_clear(self.store, favorites=True)
        self.engine.run_once()
        self.assertIn(("clear", True), self.cloud.calls)

    def test_a_failed_clear_is_remembered(self):
        clipsync.request_clear(self.store, favorites=False)
        self.cloud.failures["clear"] = [cp.SyncError("down")]
        self.engine.run_once()
        self.advance(MINUTE)
        self.engine.run_once()
        self.assertEqual(self.cloud.names().count("clear"), 2)

    def test_clear_runs_before_new_items_are_sent(self):
        clipsync.request_clear(self.store, favorites=False)
        self.store.add_text("after", now=START - 1)
        self.engine.run_once()
        names = self.cloud.names()
        self.assertLess(names.index("clear"), names.index("push"))


class FailureTests(SyncCase):
    def test_a_refused_key_stops_all_calls_until_reconnected(self):
        self.store.add_text("a", now=START - 5)
        self.cloud.failures["pull"] = [cp.AuthError("no")]
        self.engine.run_once()
        self.assertEqual(self.engine.state, "auth")
        self.cloud.calls.clear()
        self.advance(10 * MINUTE)
        report = self.engine.run_once()
        self.assertEqual(self.cloud.calls, [])
        self.assertEqual((report.pushed, report.pulled), (0, 0))
        self.assertEqual(self.engine.state, "auth")

    def test_a_refusal_while_pushing_also_stops(self):
        self.store.add_text("a", now=START - 5)
        self.cloud.failures["push"] = [cp.AuthError("no")]
        self.engine.run_once()
        self.assertEqual(self.engine.state, "auth")
        self.assertEqual(self.cloud.names(), ["push"])

    def test_network_failures_back_off_from_30_seconds_to_10_minutes(self):
        self.assertIsNone(self.engine.retry_delay())
        self.cloud.failures["pull"] = [cp.SyncError("down") for _ in range(8)]
        delays = []
        for _ in range(8):
            self.engine.run_once()
            delays.append(self.engine.retry_delay())
        self.assertEqual(delays, [30.0, 60.0, 120.0, 240.0, 480.0, 600.0, 600.0, 600.0])
        self.assertEqual(self.engine.state, "offline")
        self.engine.run_once()
        self.assertIsNone(self.engine.retry_delay())
        self.assertEqual(self.engine.state, "ok")

    def test_the_backoff_is_jittered(self):
        engine = clipsync.Engine(self.store, self.cloud, clock=lambda: self.now, jitter=lambda: 0.0)
        self.cloud.failures["pull"] = [cp.SyncError("down")]
        engine.run_once()
        self.assertAlmostEqual(engine.retry_delay() or 0, 24.0)
        engine = clipsync.Engine(self.store, self.cloud, clock=lambda: self.now, jitter=lambda: 1.0)
        self.cloud.failures["pull"] = [cp.SyncError("down")]
        engine.run_once()
        self.assertAlmostEqual(engine.retry_delay() or 0, 36.0)

    def test_a_server_error_is_reported_but_not_fatal(self):
        self.cloud.failures["pull"] = [cp.SyncError("oops", 500)]
        report = self.engine.run_once()
        self.assertEqual(self.engine.state, "error")
        self.assertEqual(len(report.errors), 1)
        self.assertIsNotNone(self.engine.retry_delay())


class ReportTests(SyncCase):
    def test_the_report_counts_what_happened(self):
        self.store.add_text("out", now=START - 10)
        self.cloud.add("in", ts=START - 20)
        report = self.engine.run_once()
        self.assertEqual(
            (report.pushed, report.pulled, report.deleted, report.errors), (1, 1, 0, [])
        )
        self.assertEqual(self.engine.state, "ok")


if __name__ == "__main__":
    unittest.main()
