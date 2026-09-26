"""Clipboard store: capture rules, search, retention and safe concurrent use."""

from __future__ import annotations

import os
import sqlite3
import stat
import struct
import sys
import tempfile
import threading
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import clipstore

try:
    import PIL  # noqa: F401

    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False


def make_png(width: int = 2, height: int = 2, color: tuple[int, int, int] = (255, 0, 0)) -> bytes:
    """A valid PNG without needing Pillow."""
    raw = b"".join(b"\x00" + bytes(color) * width for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


class StoreCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name) / "clipboard"
        self.store = clipstore.Store(self.directory)
        self.addCleanup(self.store.close)


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
        limit_mb = (second.bytes + starred.bytes + 10) / 1_000_000
        self.store.prune(keep_items=1000, keep_days=3650, image_cache_mb=limit_mb, now=10.0)
        self.assertIsNone(self.store.get(first.id))
        self.assertIsNotNone(self.store.get(second.id))
        self.assertIsNotNone(self.store.get(starred.id))
        # Favorites are never evicted even when they alone exceed the cap.
        self.store.prune(keep_items=1000, keep_days=3650, image_cache_mb=0.0, now=10.0)
        self.assertIsNone(self.store.get(second.id))
        self.assertIsNotNone(self.store.get(starred.id))

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
        with sqlite3.connect(self.directory / "clips.db") as raw:
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
