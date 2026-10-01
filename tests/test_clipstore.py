"""Clipboard store: capture rules, search, retention and safe concurrent use."""

from __future__ import annotations

import contextlib
import os
import sqlite3
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import clipstore
from support import make_png

try:
    import PIL  # noqa: F401

    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False


class StoreCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name) / "clipboard"
        self.store = clipstore.Store(self.directory)
        self.addCleanup(self.store.close)


class PermissionTests(unittest.TestCase):
    @unittest.skipIf(sys.platform == "win32", "POSIX permission bits")
    def test_everything_is_owner_only_from_creation_even_with_a_permissive_umask(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder) / "clipboard"
            previous = os.umask(0)
            try:
                store = clipstore.Store(directory)
                store.add_text("hello", now=1.0)
                store.add_image(make_png(), now=2.0)
            finally:
                os.umask(previous)
            try:
                for path in (directory, directory / "images", directory / "thumbs"):
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700, path)
                names = {p.name for p in directory.iterdir() if p.is_file()}
                self.assertIn("clips.db", names)
                for path in [p for p in directory.rglob("*") if p.is_file()]:
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600, path)
            finally:
                store.close()

    @unittest.skipIf(sys.platform == "win32", "POSIX permission bits")
    def test_the_umask_is_restored_after_opening(self):
        with tempfile.TemporaryDirectory() as folder:
            previous = os.umask(0o022)
            try:
                store = clipstore.Store(Path(folder) / "clipboard")
                current = os.umask(0o022)
            finally:
                os.umask(previous)
            store.close()
            self.assertEqual(current, 0o022)

    def test_a_symbolic_link_database_is_refused(self):
        if not hasattr(os, "symlink"):
            self.skipTest("no symlinks")
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder) / "clipboard"
            directory.mkdir()
            try:
                os.symlink(Path(folder) / "elsewhere.db", directory / "clips.db")
            except OSError:
                self.skipTest("cannot create symlinks here")
            with self.assertRaises(clipstore.StoreError):
                clipstore.Store(directory)

    def test_windows_gets_an_owner_only_acl_on_the_data_folder(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(sys, "platform", "win32"):
            with patch.object(clipstore.desktop, "restrict_to_owner") as restrict:
                with patch.object(clipstore.os, "getuid", create=True, return_value=0):
                    directory = clipstore._private_dir(Path(folder) / "clipboard")
            restrict.assert_called_once_with(directory)

    def test_module_wipe_removes_the_whole_folder_including_side_files(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder) / "clipboard"
            store = clipstore.Store(directory)
            store.add_text("secret words", now=1.0)
            store.add_image(make_png(), now=2.0)
            store.close()
            (directory / "clips.db.damaged-1").write_text("old")
            clipstore.wipe(directory)
            self.assertFalse(directory.exists())
            clipstore.wipe(directory)  # Already gone is fine.

    def test_module_wipe_never_follows_a_link(self):
        if not hasattr(os, "symlink"):
            self.skipTest("no symlinks")
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "precious"
            target.mkdir()
            (target / "keep.txt").write_text("keep")
            link = Path(folder) / "clipboard"
            try:
                link.symlink_to(target, target_is_directory=True)
            except OSError:
                self.skipTest("cannot create symlinks here")
            clipstore.wipe(link)
            self.assertFalse(link.exists() or link.is_symlink())
            self.assertTrue((target / "keep.txt").exists())


class CaptureTests(StoreCase):
    def test_items_list_newest_first(self):
        self.store.add_text("first", now=100.0)
        self.store.add_text("second", now=200.0)
        self.assertEqual([item.text for item in self.store.list()], ["second", "first"])
        self.assertEqual(self.store.count(), 2)

    def test_identical_text_moves_to_the_top_without_duplicating(self):
        first = self.store.add_text("hello", now=100.0)
        self.store.add_text("other", now=150.0)
        again = self.store.add_text("hello", now=200.0)
        self.assertEqual(again.id, first.id)
        self.assertEqual([item.text for item in self.store.list()], ["hello", "other"])
        self.assertEqual(self.store.count(), 2)
        self.assertEqual(self.store.get(first.id).created_at, 200.0)

    def test_recopy_moves_up_but_is_not_sent_to_the_cloud_again(self):
        item = self.store.add_text("hello", now=100.0)
        self.store.mark_pushed(item.id, "text|100000|hello")
        self.store.add_text("hello", now=500.0)
        self.assertEqual(self.store.dirty(), [])

    def test_empty_whitespace_and_oversize_text_are_rejected(self):
        for text in ("", "   \n\t", "x" * (clipstore.MAX_TEXT_BYTES + 1)):
            self.assertIsNone(self.store.add_text(text))
        self.assertEqual(self.store.count(), 0)
        self.assertIsNotNone(self.store.add_text("é" * 1000))
        # The limit is on bytes, not characters.
        self.assertIsNone(self.store.add_text("é" * clipstore.MAX_TEXT_BYTES))

    def test_a_single_link_is_a_url_and_other_text_is_text(self):
        link = self.store.add_text("https://example.com/a?b=c")
        self.assertEqual(link.kind, "url")
        for text in (
            "see https://example.com",
            "https://a.com https://b.com",
            "ftp://x.org",
            "example.com",
        ):
            self.assertEqual(self.store.add_text(text).kind, "text", text)

    def test_source_is_kept(self):
        self.assertEqual(self.store.add_text("spoken", source="dictation").source, "dictation")
        self.assertEqual(self.store.add_text("copied").source, "desktop")

    def test_dictation_source_survives_deduplication_and_later_clipboard_copies(self):
        copied = self.store.add_text("same words", now=1.0)
        spoken = self.store.add_text("same words", source="dictation", now=2.0)
        self.assertEqual(spoken.id, copied.id)
        self.assertEqual(spoken.source, "dictation")
        self.assertEqual(self.store.add_text("same words", now=3.0).source, "dictation")
        self.assertEqual(self.store.count(), 1)

    def test_source_filter_combines_with_search_favorites_and_pagination(self):
        first = self.store.add_text("spoken invoice", source="dictation", now=1.0)
        self.store.set_favorite(first.id, True)
        self.store.add_text("spoken notes", source="dictation", now=2.0)
        self.store.add_text("copied invoice", now=3.0)
        self.store.add_text("cloud invoice", source="cloud", now=4.0)
        self.assertEqual(
            [item.text for item in self.store.list(source="dictation", limit=1)],
            ["spoken notes"],
        )
        self.assertEqual(
            self.store.list(source="dictation", query="INVOICE", favorites=True, before=2.0),
            [self.store.get(first.id)],
        )
        with self.assertRaises(ValueError):
            self.store.list(source="dictation' OR 1=1 --")

    def test_images_are_stored_privately_and_deduplicated(self):
        png = make_png(4, 3)
        item = self.store.add_image(png, now=100.0)
        self.assertEqual(
            (item.kind, item.width, item.height, item.bytes), ("image", 4, 3, len(png))
        )
        stored = self.store.image_path(item)
        self.assertEqual(stored.read_bytes(), png)
        if sys.platform != "win32":
            self.assertEqual(stat.S_IMODE(stored.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
        again = self.store.add_image(png, now=200.0)
        self.assertEqual(again.id, item.id)
        self.assertEqual(self.store.count(), 1)
        self.assertEqual(len(list((self.directory / "images").iterdir())), 1)

    @unittest.skipUnless(HAS_PILLOW, "Pillow not installed")
    def test_images_get_a_small_thumbnail(self):
        item = self.store.add_image(make_png(600, 400))
        thumb = self.store.thumb_path(item)
        self.assertTrue(thumb.is_file())
        from PIL import Image

        with Image.open(thumb) as image:
            self.assertLessEqual(max(image.size), clipstore.THUMBNAIL_PIXELS)

    def test_without_pillow_the_image_is_kept_and_has_no_thumbnail(self):
        with patch.object(clipstore, "_thumbnail", return_value=b""):
            item = self.store.add_image(make_png(20, 20))
        self.assertEqual(item.thumb_file, "")
        self.assertTrue(self.store.image_path(item).is_file())
        self.assertIsNone(self.store.thumb_path(item))

    def test_oversize_and_non_png_images_are_rejected(self):
        for data in (b"", b"not a png", make_png()[:20], b"\x89PNG\r\n\x1a\n" + b"x" * 40):
            self.assertIsNone(self.store.add_image(data))
        with patch.object(clipstore, "MAX_IMAGE_BYTES", 100):
            self.assertIsNone(self.store.add_image(make_png(50, 50)))
        self.assertEqual(self.store.count(), 0)
        self.assertEqual(list((self.directory / "images").iterdir()), [])


class QueryTests(StoreCase):
    def test_search_is_case_insensitive_and_covers_non_ascii(self):
        self.store.add_text("Grüße from Berlin", now=1.0)
        self.store.add_text("invoice 4711", now=2.0)
        # Case folding: "ß" matches "SS" in either direction.
        self.assertEqual([i.text for i in self.store.list(query="GRÜSSE")], ["Grüße from Berlin"])
        self.assertEqual([i.text for i in self.store.list(query="grüße")], ["Grüße from Berlin"])
        self.assertEqual([i.text for i in self.store.list(query="INVOICE")], ["invoice 4711"])

    def test_search_treats_wildcards_literally(self):
        self.store.add_text("100% sure", now=1.0)
        self.store.add_text("100 percent", now=2.0)
        self.store.add_text("a_b", now=3.0)
        self.store.add_text("axb", now=4.0)
        self.assertEqual([i.text for i in self.store.list(query="100%")], ["100% sure"])
        self.assertEqual([i.text for i in self.store.list(query="a_b")], ["a_b"])

    def test_kind_and_favorite_filters(self):
        text = self.store.add_text("plain", now=1.0)
        self.store.add_text("https://example.com", now=2.0)
        self.store.add_image(make_png(), now=3.0)
        self.store.set_favorite(text.id, True)
        self.assertEqual(len(self.store.list(kind="text")), 2)  # Text and links.
        self.assertEqual([i.kind for i in self.store.list(kind="url")], ["url"])
        self.assertEqual([i.kind for i in self.store.list(kind="image")], ["image"])
        self.assertEqual([i.text for i in self.store.list(favorites=True)], ["plain"])
        self.assertEqual(self.store.list(kind="image", favorites=True), [])

    def test_paging_walks_the_whole_history_once(self):
        for number in range(25):
            self.store.add_text(f"item {number}", now=float(number))
        seen, before = [], None
        while True:
            page = self.store.list(limit=10, before=before)
            if not page:
                break
            seen += [item.text for item in page]
            before = page[-1].created_at
        self.assertEqual(seen, [f"item {n}" for n in range(24, -1, -1)])

    def test_favorite_toggles_and_marks_the_item_for_sync(self):
        item = self.store.add_text("keep", now=1.0)
        self.store.mark_pushed(item.id, "text|1000|keep")
        self.assertEqual(self.store.dirty(), [])
        self.store.set_favorite(item.id, True)
        self.assertTrue(self.store.get(item.id).favorite)
        self.assertEqual([i.id for i in self.store.dirty()], [item.id])
        self.store.set_favorite(item.id, False)
        self.assertFalse(self.store.get(item.id).favorite)

    def test_search_finds_every_word_in_any_order(self):
        wanted = self.store.add_text("Acme Corp invoice #4711", now=1.0)
        self.store.add_text("Invoice for someone else", now=2.0)
        self.assertEqual([i.id for i in self.store.list(query="invoice acme")], [wanted.id])
        self.assertEqual([i.id for i in self.store.list(query="  ACME   4711 ")], [wanted.id])
        self.assertEqual(len(self.store.list(query="invoice")), 2)
        self.assertEqual(self.store.list(query="acme unrelated"), [])

    def test_only_favorites_take_a_label_and_unstarring_drops_it(self):
        item = self.store.add_text("wifi", now=1.0)
        self.store.set_label(item.id, "home")
        self.assertEqual(self.store.get(item.id).label, "")
        self.store.set_favorite(item.id, True)
        self.store.set_label(item.id, "  Home   wifi  ")
        self.assertEqual(self.store.get(item.id).label, "Home wifi")
        self.assertEqual([i.id for i in self.store.list(query="home")], [item.id])
        self.store.set_label(item.id, "x" * 500)
        self.assertEqual(len(self.store.get(item.id).label), clipstore.MAX_LABEL_CHARS)
        self.store.set_label(item.id, "")
        self.assertEqual(self.store.get(item.id).label, "")
        self.store.set_label(item.id, "again")
        self.store.set_favorite(item.id, False)
        self.assertEqual(self.store.get(item.id).label, "")

    def test_clearing_only_this_device_leaves_no_tombstones(self):
        keep = self.store.add_text("star", now=1.0)
        gone = self.store.add_text("gone", now=2.0)
        self.store.set_favorite(keep.id, True)
        for item in (keep, gone):
            self.store.link(item.id, f"cloud-{item.id}", False)
        self.assertEqual(self.store.clear(keep_favorites=True, tombstones=False), 1)
        self.assertEqual(self.store.tombstones(), [])
        self.assertEqual([i.text for i in self.store.list()], ["star"])
        self.store.clear(keep_favorites=False, tombstones=False)
        self.assertEqual((self.store.count(), self.store.tombstones()), (0, []))

    def test_forgetting_the_account_makes_every_item_new_again(self):
        first = self.store.add_text("one", now=1.0)
        second = self.store.add_text("two", now=2.0)
        self.store.add_image(make_png(), now=3.0)
        self.store.set_favorite(first.id, True)
        self.store.mark_pushed(first.id, "text|a|one", cloud_favorite=True)
        self.store.link(first.id, "cloud-1", True)
        self.store.mark_pushed(second.id, "text|b|two")
        self.store.set_skip(second.id)
        self.store.delete(self.store.add_text("three", now=4.0).id)
        self.store.meta_set("sync_cursor", "123.0")
        self.store.meta_set("clear_pending", "keep")
        self.store.reset_sync()
        for item in self.store.list():
            self.assertEqual((item.cloud_id, item.cloud_key, item.cloud_favorite), ("", "", False))
            self.assertFalse(item.sync_skip)
        self.assertEqual(sorted(i.text for i in self.store.dirty()), ["one", "two"])
        self.assertTrue(self.store.get(first.id).favorite)  # Local choices stay.
        self.assertEqual(self.store.tombstones(), [])
        self.assertEqual(
            (self.store.meta_get("sync_cursor"), self.store.meta_get("clear_pending")), ("", "")
        )

    def test_missing_item_operations_are_harmless(self):
        self.assertIsNone(self.store.get(999))
        self.store.set_favorite(999, True)
        self.store.delete(999)

    def test_dirty_items_are_text_and_links_only_oldest_first(self):
        self.store.add_text("later", now=20.0)
        self.store.add_text("earlier", now=10.0)
        self.store.add_image(make_png(), now=15.0)
        self.assertEqual([i.text for i in self.store.dirty()], ["earlier", "later"])
        self.assertEqual(len(self.store.dirty(limit=1)), 1)


class DeleteAndRetentionTests(StoreCase):
    def test_delete_removes_the_image_files(self):
        item = self.store.add_image(make_png())
        path = self.store.image_path(item)
        self.store.delete(item.id)
        self.assertFalse(path.exists())
        self.assertEqual(self.store.count(), 0)

    def test_deleting_a_synced_item_leaves_a_tombstone_and_an_unsynced_one_does_not(self):
        synced = self.store.add_text("synced", now=1.0)
        self.store.link(synced.id, "cloud-1", False)
        pending = self.store.add_text("pushed but not linked", now=2.0)
        self.store.mark_pushed(pending.id, "text|2000|pushed but not linked")
        local = self.store.add_text("never sent", now=3.0)
        for item in (synced, pending, local):
            self.store.delete(item.id)
        keys = sorted((t.cloud_id, t.cloud_key) for t in self.store.tombstones())
        self.assertEqual(keys, [("", "text|2000|pushed but not linked"), ("cloud-1", "")])

    def test_clear_keeps_favorites_by_default_and_reports_the_count(self):
        keep = self.store.add_text("keep", now=1.0)
        self.store.add_text("drop one", now=2.0)
        self.store.add_image(make_png(), now=3.0)
        self.store.set_favorite(keep.id, True)
        self.assertEqual(self.store.clear(), 2)
        self.assertEqual([i.text for i in self.store.list()], ["keep"])
        self.assertEqual(self.store.clear(keep_favorites=False), 1)
        self.assertEqual(self.store.count(), 0)
        self.assertEqual(list((self.directory / "images").iterdir()), [])

    def test_wipe_removes_everything_locally_without_telling_the_cloud(self):
        item = self.store.add_text("linked", now=1.0)
        self.store.link(item.id, "cloud-1", False)
        self.store.add_image(make_png(), now=2.0)
        self.store.meta_set("cursor", "5")
        self.store.wipe()
        self.assertEqual(self.store.count(), 0)
        self.assertEqual(self.store.tombstones(), [])
        self.assertEqual(self.store.meta_get("cursor"), "")
        self.assertEqual(list((self.directory / "images").iterdir()), [])
        self.assertIsNotNone(self.store.add_text("still works"))

    def test_prune_by_age_and_count_spares_favorites_and_leaves_no_tombstones(self):
        day = 86400.0
        old = self.store.add_text("old", now=0.0)
        self.store.set_favorite(old.id, True)
        self.store.link(old.id, "cloud-old", False)
        for number in range(1, 6):
            self.store.add_text(f"recent {number}", now=100 * day + number)
        self.store.add_text("ancient", now=1.0)
        removed = self.store.prune(keep_items=3, keep_days=30, now=100 * day + 10)
        self.assertEqual(removed, 3)  # "ancient" by age, two oldest recents by count.
        self.assertEqual(
            sorted(i.text for i in self.store.list()), ["old", "recent 3", "recent 4", "recent 5"]
        )
        self.assertEqual(self.store.tombstones(), [])

    def test_prune_evicts_the_oldest_non_favorite_images_when_over_the_cache_limit(self):
        first = self.store.add_image(make_png(30, 30, (1, 2, 3)), now=1.0)
        second = self.store.add_image(make_png(30, 30, (4, 5, 6)), now=2.0)
        starred = self.store.add_image(make_png(30, 30, (7, 8, 9)), now=0.5)
        self.store.set_favorite(starred.id, True)
        # The cap budgets only non-favorite bytes; a favorites collection must not
        # eat into it (nor the other way around — see the favorites cap test below).
        limit_mb = (second.bytes + 10) / 1_000_000
        self.store.prune(keep_items=1000, keep_days=3650, image_cache_mb=limit_mb, now=10.0)
        self.assertIsNone(self.store.get(first.id))
        self.assertIsNotNone(self.store.get(second.id))
        self.assertIsNotNone(self.store.get(starred.id))
        # Favorites are exempt from the *regular* image cache cap.
        self.store.prune(keep_items=1000, keep_days=3650, image_cache_mb=0.0, now=10.0)
        self.assertIsNone(self.store.get(second.id))
        self.assertIsNotNone(self.store.get(starred.id))

    def test_prune_evicts_the_oldest_favorite_images_once_their_own_cap_is_exceeded(self):
        old_star = self.store.add_image(make_png(30, 30, (1, 2, 3)), now=1.0)
        new_star = self.store.add_image(make_png(30, 30, (4, 5, 6)), now=2.0)
        self.store.set_favorite(old_star.id, True)
        self.store.set_favorite(new_star.id, True)
        # A regular-history cap of 0 must not touch favorites; only their own cap does.
        limit_mb = (new_star.bytes + 10) / 1_000_000
        self.store.prune(
            keep_items=1000,
            keep_days=3650,
            image_cache_mb=0.0,
            favorite_cache_mb=limit_mb,
            now=10.0,
        )
        self.assertIsNone(self.store.get(old_star.id))
        self.assertIsNotNone(self.store.get(new_star.id))

    def test_prune_removes_stray_image_files_but_not_ones_being_written(self):
        item = self.store.add_image(make_png())
        stale = self.directory / "images" / ("0" * 64 + ".png")
        stale.write_bytes(b"stray")
        os.utime(stale, (0, 0))
        fresh = self.directory / "images" / ("1" * 64 + ".png")
        fresh.write_bytes(b"another process is still saving this one")
        other = self.directory / "images" / "notes.txt"
        other.write_text("leave me")
        self.store.prune(keep_items=1000, keep_days=30, now=item.created_at)
        self.assertFalse(stale.exists())
        self.assertTrue(fresh.exists())
        self.assertTrue(other.exists())
        self.assertTrue(self.store.image_path(item).exists())


class ConcurrencyAndSchemaTests(StoreCase):
    def test_two_stores_can_write_at_the_same_time(self):
        errors: list[BaseException] = []
        other = clipstore.Store(self.directory)
        self.addCleanup(other.close)

        def writer(store: clipstore.Store, label: str) -> None:
            try:
                for number in range(60):
                    store.add_text(f"{label} {number}")
            except BaseException as exc:  # noqa: BLE001 - reported below
                errors.append(exc)

        threads = [
            threading.Thread(target=writer, args=(self.store, "a")),
            threading.Thread(target=writer, args=(other, "b")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(self.store.count(), 120)

    def test_one_store_is_safe_to_share_between_threads(self):
        errors: list[BaseException] = []

        def writer(label: str) -> None:
            try:
                for number in range(50):
                    self.store.add_text(f"{label} {number}")
                    self.store.list(limit=5)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(name,)) for name in "abc"]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(self.store.count(), 150)

    def test_a_new_database_is_versioned_and_reopens_with_its_data(self):
        self.store.add_text("persisted")
        self.store.close()
        reopened = clipstore.Store(self.directory)
        self.addCleanup(reopened.close)
        self.assertEqual([i.text for i in reopened.list()], ["persisted"])
        with contextlib.closing(sqlite3.connect(self.directory / "clips.db")) as raw:
            self.assertEqual(
                raw.execute("PRAGMA user_version").fetchone()[0], clipstore.SCHEMA_VERSION
            )

    def test_an_empty_unversioned_database_is_upgraded(self):
        directory = self.directory.parent / "older"
        directory.mkdir()
        sqlite3.connect(directory / "clips.db").close()  # user_version 0, no tables.
        store = clipstore.Store(directory)
        self.addCleanup(store.close)
        self.assertIsNotNone(store.add_text("works"))

    def test_a_version_1_database_gains_the_account_label_column(self):
        directory = self.directory.parent / "v1"
        directory.mkdir()
        schema = clipstore._SCHEMA.replace(
            ",\n    cloud_label TEXT NOT NULL DEFAULT ''", ""
        ).replace(",\n    synced_at REAL NOT NULL DEFAULT 0", "")
        with contextlib.closing(sqlite3.connect(directory / "clips.db")) as raw:
            raw.executescript(schema)
            raw.execute(
                "INSERT INTO items (kind, text, sha, created_at, updated_at, favorite, label, "
                "cloud_id) VALUES ('text', 'old', 'abc', 1, 1, 1, 'pinned', 'id-1')"
            )
            raw.execute("PRAGMA user_version = 1")
            raw.commit()
        store = clipstore.Store(directory)
        self.addCleanup(store.close)
        (item,) = store.list()
        self.assertEqual((item.label, item.cloud_label), ("pinned", "pinned"))
        self.assertEqual(store.pending_labels(), [])

    def test_a_database_from_a_newer_app_is_refused_not_damaged(self):
        directory = self.directory.parent / "newer"
        directory.mkdir()
        raw = sqlite3.connect(directory / "clips.db")
        raw.execute(f"PRAGMA user_version = {clipstore.SCHEMA_VERSION + 1}")
        raw.commit()
        raw.close()
        with self.assertRaises(clipstore.StoreError):
            clipstore.Store(directory)

    def test_meta_values_round_trip(self):
        self.assertEqual(self.store.meta_get("cursor"), "")
        self.assertEqual(self.store.meta_get("cursor", "0"), "0")
        self.store.meta_set("cursor", "123.5")
        self.assertEqual(self.store.meta_get("cursor"), "123.5")

    def test_sql_metacharacters_in_content_are_data(self):
        nasty = "'; DROP TABLE items; --"
        self.store.add_text(nasty)
        self.assertEqual([i.text for i in self.store.list(query=nasty)], [nasty])
        self.assertEqual(self.store.count(), 1)


if __name__ == "__main__":
    unittest.main()
