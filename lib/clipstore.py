"""Local clipboard history: text, links and images.

Rows live in SQLite (write-ahead logging, so the clipboard service, the window and
the dictation engine can use the same database at once); images are files named
after their content hash. This module is the only code that touches either. It uses
the standard library only, and Pillow when available to make thumbnails.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
import sqlite3
import struct
import sys
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import desktop

if TYPE_CHECKING:
    from clipboardplus import CloudItem

SCHEMA_VERSION = 3
META_CURSOR = "sync_cursor"  # When the account last synced fine.
META_CLEAR = "clear_pending"  # A clear-everywhere the account has not been told about.
META_CLEARED = "cleared_at"  # When the history was last cleared: older account items stay out.
# Retention removed synced items up to this time (account copies are left alone, and the
# older non-favorite ones must not come back as new items).
META_PRUNED = "pruned_until"
META_RECONCILED = "reconciled_at"  # When the account listing last checked for web deletions.
META_RECONCILE_NOW = "reconcile_now"  # "1": check the account listing on the next round.
MAX_TEXT_BYTES = 1_000_000
MAX_LABEL_CHARS = 100
MAX_IMAGE_BYTES = 10_000_000
MAX_IMAGE_PIXELS = 50_000_000  # Refuses decompression bombs before any decoding.
THUMBNAIL_PIXELS = 256
# A file this young may belong to a writer that has not inserted its row yet.
STRAY_FILE_GRACE_SECONDS = 60.0

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PNG_END = b"IEND\xaeB`\x82"
_URL = re.compile(r"https?://\S+")
_FILE_NAME = re.compile(r"[0-9a-f]{64}\.png")
_KINDS = ("", "text", "url", "image")
_BATCH = 400

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN ('text', 'url', 'image')),
    text TEXT NOT NULL DEFAULT '',
    image_file TEXT NOT NULL DEFAULT '',
    thumb_file TEXT NOT NULL DEFAULT '',
    width INTEGER NOT NULL DEFAULT 0,
    height INTEGER NOT NULL DEFAULT 0,
    bytes INTEGER NOT NULL DEFAULT 0,
    sha TEXT NOT NULL UNIQUE,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    favorite INTEGER NOT NULL DEFAULT 0,
    label TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'desktop',
    cloud_id TEXT NOT NULL DEFAULT '',
    cloud_key TEXT NOT NULL DEFAULT '',
    cloud_favorite INTEGER NOT NULL DEFAULT 0,
    dirty INTEGER NOT NULL DEFAULT 1,
    sync_skip INTEGER NOT NULL DEFAULT 0,
    cloud_label TEXT NOT NULL DEFAULT '',
    synced_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS items_created ON items (created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS items_cloud_key ON items (cloud_key) WHERE cloud_key != '';
CREATE INDEX IF NOT EXISTS items_cloud_id ON items (cloud_id) WHERE cloud_id != '';
CREATE TABLE IF NOT EXISTS tombstones (
    cloud_id TEXT NOT NULL,
    cloud_key TEXT NOT NULL,
    deleted_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class StoreError(Exception):
    """The history cannot be opened safely."""


@dataclass(frozen=True)
class Item:
    id: int
    kind: str  # text, url or image
    text: str
    image_file: str
    thumb_file: str
    width: int
    height: int
    bytes: int
    created_at: float
    updated_at: float
    favorite: bool
    label: str
    source: str  # desktop, dictation or cloud
    cloud_id: str
    cloud_key: str
    cloud_favorite: bool
    dirty: bool
    sync_skip: bool
    cloud_label: str = ""  # The label the account holds.
    synced_at: float = 0.0  # When the account last confirmed this item (0: before tracking).


@dataclass(frozen=True)
class Tombstone:
    """A deletion the cloud has not been told about yet."""

    cloud_id: str
    cloud_key: str
    deleted_at: float


# Aliases: inside Store, the `list` method hides the builtin from annotations.
Items = list[Item]
Tombstones = list[Tombstone]
Ids = list[int]


@contextmanager
def _owner_only_umask() -> Iterator[None]:
    """Files made inside this block (SQLite's database, -wal and -shm) are owner-only
    from their first byte. The umask is process-wide, so the block is kept short."""
    previous = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(previous)


def _private_dir(path: Path) -> Path:
    if path.is_symlink():
        raise StoreError(f"{path.name} must not be a symbolic link.")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise StoreError(f"{path.name} must be a plain folder.")
    if sys.platform != "win32":
        if path.stat().st_uid != os.getuid():
            raise StoreError(f"{path.name} must belong to you.")
        path.chmod(0o700)
    else:
        desktop.restrict_to_owner(path)  # Owner only; new files inside inherit it.
    return path


def _create_private_file(path: Path) -> None:
    """Make an empty owner-only file first, so SQLite opens ours instead of creating one."""
    if path.is_symlink():
        raise StoreError(f"{path.name} must not be a symbolic link.")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    os.close(os.open(path, flags, 0o600))
    if sys.platform != "win32":
        path.chmod(0o600)


def _write_private(path: Path, data: bytes) -> None:
    """Write a new owner-only file; an existing file with this content-derived name is kept."""
    if path.exists():
        return
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.part")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _thumbnail(png: bytes, size: int) -> bytes:
    """A small PNG preview, or nothing when Pillow is missing or cannot read the image."""
    try:
        from PIL import Image  # type: ignore[import-not-found, unused-ignore]
    except ImportError:
        return b""
    try:
        with Image.open(io.BytesIO(png)) as image:
            image.thumbnail((size, size))
            output = io.BytesIO()
            image.save(output, "PNG")
            return output.getvalue()
    except Exception:  # noqa: BLE001 - any decoder failure just means no preview
        return b""


def _png_size(png: bytes) -> tuple[int, int] | None:
    """Width and height from a complete, plausible PNG, otherwise None."""
    if not png.startswith(_PNG_SIGNATURE) or not png.endswith(_PNG_END) or len(png) < 33:
        return None
    if png[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", png[16:24])
    if width < 1 or height < 1 or width * height > MAX_IMAGE_PIXELS:
        return None
    return width, height


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class Store:
    def __init__(self, directory: Path) -> None:
        self.directory = _private_dir(directory)
        self._images = _private_dir(self.directory / "images")
        self._thumbs = _private_dir(self.directory / "thumbs")
        self._lock = threading.RLock()
        self._stamp_cache: tuple[int, float] | None = None
        self._stamp_version = -1
        database = self.directory / "clips.db"
        _create_private_file(database)
        with _owner_only_umask():
            self._db = sqlite3.connect(database, timeout=5.0, check_same_thread=False)
            self._db.isolation_level = None  # Transactions are explicit (see _transaction).
            self._db.row_factory = sqlite3.Row
            self._db.create_function("fold", 1, _fold, deterministic=True)
            try:
                self._db.execute("PRAGMA busy_timeout = 5000")
                self._db.execute("PRAGMA journal_mode = WAL")
                # Deleted clips are overwritten, not left readable in free pages.
                self._db.execute("PRAGMA secure_delete = ON")
                self._upgrade()
            except BaseException:
                self._db.close()
                raise
        if sys.platform != "win32":
            for suffix in ("", "-wal", "-shm"):
                candidate = database.with_name(database.name + suffix)
                if candidate.exists():
                    candidate.chmod(0o600)

    # -- lifecycle ---------------------------------------------------------
    def _upgrade(self) -> None:
        with self._transaction() as db:
            version = int(db.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise StoreError(
                    "This clipboard history was made by a newer version of the app. "
                    "Update the app to open it."
                )
            if version < SCHEMA_VERSION:
                # executescript() would commit our transaction, so run the statements.
                for statement in _SCHEMA.split(";"):
                    if statement.strip():
                        db.execute(statement)
                columns = {row["name"] for row in db.execute("PRAGMA table_info(items)")}
                if "cloud_label" not in columns:
                    # Version 1 only ever held the account's own labels.
                    db.execute("ALTER TABLE items ADD COLUMN cloud_label TEXT NOT NULL DEFAULT ''")
                    db.execute("UPDATE items SET cloud_label = label")
                if "synced_at" not in columns:
                    # Version 3 records when the account last confirmed an item; existing
                    # ones count as old (0).
                    db.execute("ALTER TABLE items ADD COLUMN synced_at REAL NOT NULL DEFAULT 0")
                db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")
            # PRAGMA data_version changes for commits by other connections, not this one.
            # Invalidate explicitly so local UI edits refresh the cached history stamp too.
            self._stamp_cache = None

    # -- reading -----------------------------------------------------------
    @staticmethod
    def _item(row: sqlite3.Row) -> Item:
        return Item(
            id=row["id"],
            kind=row["kind"],
            text=row["text"],
            image_file=row["image_file"],
            thumb_file=row["thumb_file"],
            width=row["width"],
            height=row["height"],
            bytes=row["bytes"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            favorite=bool(row["favorite"]),
            label=row["label"],
            source=row["source"],
            cloud_id=row["cloud_id"],
            cloud_key=row["cloud_key"],
            cloud_favorite=bool(row["cloud_favorite"]),
            dirty=bool(row["dirty"]),
            sync_skip=bool(row["sync_skip"]),
            cloud_label=row["cloud_label"],
            synced_at=row["synced_at"],
        )

    def get(self, item_id: int) -> Item | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return self._item(row) if row else None

    def count(self) -> int:
        with self._lock:
            return int(self._db.execute("SELECT COUNT(*) FROM items").fetchone()[0])

    def stamp(self) -> tuple[int, float]:
        """Return the history size and newest update, querying only after a database change."""
        with self._lock:
            version = int(self._db.execute("PRAGMA data_version").fetchone()[0])
            if self._stamp_cache is not None and version == self._stamp_version:
                return self._stamp_cache
            row = self._db.execute(
                "SELECT COUNT(*), COALESCE(MAX(updated_at), 0) FROM items"
            ).fetchone()
            self._stamp_cache = int(row[0]), float(row[1])
            self._stamp_version = version
            return self._stamp_cache

    def list(
        self,
        *,
        query: str = "",
        kind: str = "",
        favorites: bool = False,
        limit: int = 50,
        before: float | None = None,
    ) -> Items:
        """Newest first. `kind` "text" includes links; `before` pages by creation time."""
        if kind not in _KINDS:
            raise ValueError(f"Unknown kind: {kind!r}")
        where: list[str] = []
        args: list[object] = []
        if kind == "text":
            where.append("kind IN ('text', 'url')")
        elif kind:
            where.append("kind = ?")
            args.append(kind)
        if favorites:
            where.append("favorite = 1")
        # Every word must appear, in any order ("invoice acme" finds "Acme Corp invoice").
        for word in query.casefold().split():
            pattern = "%" + _escape_like(word) + "%"
            where.append("(fold(text) LIKE ? ESCAPE '\\' OR fold(label) LIKE ? ESCAPE '\\')")
            args += [pattern, pattern]
        if before is not None:
            where.append("created_at < ?")
            args.append(before)
        sql = "SELECT * FROM items"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        args.append(max(1, limit))
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        return [self._item(row) for row in rows]

    def dirty(self, limit: int = 100) -> Items:
        """Text and links waiting to be sent to the account, oldest first.

        Items the account already has (linked) are not listed: a changed favorite
        reaches it through `pending_favorites`.
        """
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM items WHERE dirty = 1 AND sync_skip = 0 AND cloud_id = '' "
                "AND kind IN ('text', 'url') ORDER BY created_at ASC, id ASC LIMIT ?",
                (max(1, limit),),
            ).fetchall()
        return [self._item(row) for row in rows]

    def image_path(self, item: Item) -> Path:
        return self._images / item.image_file

    def thumb_path(self, item: Item) -> Path | None:
        if not item.thumb_file:
            return None
        path = self._thumbs / item.thumb_file
        return path if path.is_file() else None

    # -- capturing ---------------------------------------------------------
    def add_text(self, text: str, source: str = "desktop", now: float | None = None) -> Item | None:
        """Store copied text or a link. Blank or oversize text is refused."""
        # Lone surrogates from a bad clipboard decode cannot be stored; replace them.
        raw = text.encode("utf-8", "replace")
        if not raw.strip() or len(raw) > MAX_TEXT_BYTES:
            return None
        clean = raw.decode("utf-8")
        kind = "url" if _URL.fullmatch(clean.strip()) else "text"
        sha = _text_sha(raw)
        stamp = time.time() if now is None else now
        with self._transaction() as db:
            existing = db.execute("SELECT id FROM items WHERE sha = ?", (sha,)).fetchone()
            if existing:
                return self._touch(db, int(existing["id"]), stamp)
            cursor = db.execute(
                "INSERT INTO items (kind, text, bytes, sha, created_at, updated_at, source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (kind, clean, len(raw), sha, stamp, stamp, source),
            )
            return self._fetch(db, int(cursor.lastrowid or 0))

    def add_image(
        self, png: bytes, source: str = "desktop", now: float | None = None
    ) -> Item | None:
        """Store a copied image (PNG bytes). Images stay on this device."""
        if not png or len(png) > MAX_IMAGE_BYTES:
            return None
        size = _png_size(png)
        if size is None:
            return None
        sha = hashlib.sha256(b"image\0" + png).hexdigest()
        stamp = time.time() if now is None else now
        with self._lock:
            known = self._db.execute("SELECT 1 FROM items WHERE sha = ?", (sha,)).fetchone()
        # Decoding a large image is slow: done before taking the write lock, so saving a
        # transcript at the same moment never waits on it.
        thumb = b"" if known else _thumbnail(png, THUMBNAIL_PIXELS)
        with self._transaction() as db:
            existing = db.execute("SELECT id FROM items WHERE sha = ?", (sha,)).fetchone()
            if existing:
                return self._touch(db, int(existing["id"]), stamp)
            name = f"{sha}.png"
            _write_private(self._images / name, png)
            if thumb:
                _write_private(self._thumbs / name, thumb)
            cursor = db.execute(
                "INSERT INTO items (kind, image_file, thumb_file, width, height, bytes, sha, "
                "created_at, updated_at, source, dirty) VALUES ('image', ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
                (
                    name,
                    name if thumb else "",
                    size[0],
                    size[1],
                    len(png),
                    sha,
                    stamp,
                    stamp,
                    source,
                ),
            )
            return self._fetch(db, int(cursor.lastrowid or 0))

    def _touch(self, db: sqlite3.Connection, item_id: int, stamp: float) -> Item:
        """Re-copied content moves to the top. It is not sent to the account again."""
        db.execute(
            "UPDATE items SET created_at = MAX(created_at, ?), updated_at = MAX(updated_at, ?) "
            "WHERE id = ?",
            (stamp, stamp, item_id),
        )
        return self._fetch(db, item_id)

    def _fetch(self, db: sqlite3.Connection, item_id: int) -> Item:
        return self._item(db.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone())

    # -- changing ----------------------------------------------------------
    def set_favorite(self, item_id: int, value: bool, now: float | None = None) -> None:
        """Only favorites carry a label (as on the account): unstarring drops it."""
        stamp = time.time() if now is None else now
        with self._transaction() as db:
            db.execute(
                "UPDATE items SET favorite = ?, updated_at = ?, dirty = 1, "
                "label = CASE WHEN ? THEN label ELSE '' END "
                "WHERE id = ? AND favorite != ?",
                (int(value), stamp, int(value), item_id, int(value)),
            )

    def set_label(self, item_id: int, label: str, now: float | None = None) -> None:
        """Name a favorite; an empty label removes the name. Other items are left alone."""
        stamp = time.time() if now is None else now
        clean = " ".join(label.split())[:MAX_LABEL_CHARS]
        with self._transaction() as db:
            db.execute(
                "UPDATE items SET label = ?, updated_at = ?, dirty = 1 "
                "WHERE id = ? AND favorite = 1 AND label != ?",
                (clean, stamp, item_id, clean),
            )

    def delete(self, item_id: int) -> None:
        self._remove(self._ids("WHERE id = ?", (item_id,)), tombstones=True)

    def clear(self, *, keep_favorites: bool = True, tombstones: bool = True) -> int:
        """Delete the whole history.

        By default the account is told too (each deletion leaves a tombstone); with
        `tombstones=False` only this device is cleared.
        """
        ids = self._ids("WHERE favorite = 0" if keep_favorites else "", ())
        self._remove(ids, tombstones=tombstones)
        return len(ids)

    def wipe(self) -> None:
        """Remove all clipboard data on this device. The account is not told."""
        self._remove(self._ids("", ()), tombstones=False)
        with self._transaction() as db:
            db.execute("DELETE FROM tombstones")
            db.execute("DELETE FROM meta")
        with self._lock:
            # Nothing erased may linger in the write-ahead log or the file's free space.
            self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._db.execute("VACUUM")
        for folder in (self._images, self._thumbs):
            for entry in folder.iterdir():
                if _FILE_NAME.fullmatch(entry.name):
                    entry.unlink(missing_ok=True)

    def prune(
        self,
        keep_items: int,
        keep_days: int,
        image_cache_mb: float = 500.0,
        favorite_cache_mb: float = 2000.0,
        now: float | None = None,
    ) -> int:
        """Apply the retention settings.

        Favorites are exempt from the item-count and age caps (they were starred on
        purpose), but their images still sit under their own, much larger disk cap —
        oldest favorited images first — so years of favoriting screenshots cannot grow
        the cache without bound. The account is left alone either way.
        """
        stamp = time.time() if now is None else now
        doomed = set(
            self._ids("WHERE favorite = 0 AND created_at < ?", (stamp - keep_days * 86400.0,))
        )
        with self._lock:
            excess = self._db.execute(
                "SELECT id FROM items WHERE favorite = 0 ORDER BY created_at DESC, id DESC "
                "LIMIT -1 OFFSET ?",
                (max(0, keep_items),),
            ).fetchall()
        doomed.update(int(row["id"]) for row in excess)
        with self._lock:
            images = self._db.execute(
                "SELECT id, bytes, favorite FROM items WHERE kind = 'image' "
                "ORDER BY created_at ASC, id ASC"
            ).fetchall()
        self._evict_over_cap(images, doomed, favorite=False, cap_mb=image_cache_mb)
        self._evict_over_cap(images, doomed, favorite=True, cap_mb=favorite_cache_mb)
        self._note_pruned(doomed)
        # Local only, by design: retention never deletes anything from the account.
        self._remove(sorted(doomed), tombstones=False)
        self._remove_stray_files(stamp)
        return len(doomed)

    def _note_pruned(self, doomed: set[int]) -> None:
        """Remember how far retention reached into the synced history.

        The account still holds those copies, so a later pull must not add them back
        here as new items (the sync engine skips older non-favorite account items).
        """
        if not doomed:
            return
        marks = ",".join("?" * len(doomed))
        with self._lock:
            row = self._db.execute(
                f"SELECT MAX(created_at) AS newest FROM items WHERE id IN ({marks}) "
                "AND cloud_id != '' AND favorite = 0",
                tuple(doomed),
            ).fetchone()
        if row is None or row["newest"] is None:
            return
        try:
            before = float(self.meta_get(META_PRUNED) or 0.0)
        except ValueError:
            before = 0.0
        self.meta_set(META_PRUNED, repr(max(before, float(row["newest"]))))

    @staticmethod
    def _evict_over_cap(
        images: Sequence[sqlite3.Row], doomed: set[int], *, favorite: bool, cap_mb: float
    ) -> None:
        """Add the oldest images of one favorite-ness to `doomed` until their own total
        bytes fits `cap_mb`. Kept separate from the other group so a large favorites
        collection cannot eat into the regular history's own budget, or vice versa."""
        cap = int(cap_mb * 1_000_000)
        total = sum(
            int(row["bytes"])
            for row in images
            if int(row["id"]) not in doomed and bool(row["favorite"]) == favorite
        )
        for row in images:
            if total <= cap:
                break
            if int(row["id"]) in doomed or bool(row["favorite"]) != favorite:
                continue
            doomed.add(int(row["id"]))
            total -= int(row["bytes"])

    def _ids(self, where: str, args: Sequence[object]) -> Ids:
        with self._lock:
            rows = self._db.execute(f"SELECT id FROM items {where}", tuple(args)).fetchall()
        return [int(row["id"]) for row in rows]

    def _remove(self, ids: Sequence[int], *, tombstones: bool) -> None:
        for start in range(0, len(ids), _BATCH):
            batch = list(ids[start : start + _BATCH])
            marks = ",".join("?" * len(batch))
            with self._transaction() as db:
                rows = db.execute(f"SELECT * FROM items WHERE id IN ({marks})", batch).fetchall()
                if tombstones:
                    now = time.time()
                    db.executemany(
                        "INSERT INTO tombstones (cloud_id, cloud_key, deleted_at) VALUES (?, ?, ?)",
                        [
                            (row["cloud_id"], row["cloud_key"], now)
                            for row in rows
                            if row["cloud_id"] or row["cloud_key"]
                        ],
                    )
                db.execute(f"DELETE FROM items WHERE id IN ({marks})", batch)
            for row in rows:
                self._unlink(self._images, row["image_file"])
                self._unlink(self._thumbs, row["thumb_file"])

    @staticmethod
    def _unlink(folder: Path, name: str) -> None:
        # Names come from the database, but only ever remove our own hash-named files.
        if name and _FILE_NAME.fullmatch(name):
            (folder / name).unlink(missing_ok=True)

    def _remove_stray_files(self, now: float) -> None:
        with self._lock:
            rows = self._db.execute(
                "SELECT image_file, thumb_file FROM items WHERE kind = 'image'"
            ).fetchall()
        used = {row["image_file"] for row in rows} | {row["thumb_file"] for row in rows}
        for folder in (self._images, self._thumbs):
            for entry in folder.iterdir():
                if not _FILE_NAME.fullmatch(entry.name) or entry.name in used:
                    continue
                try:
                    if now - entry.stat().st_mtime > STRAY_FILE_GRACE_SECONDS:
                        entry.unlink(missing_ok=True)
                except OSError:
                    continue

    # -- cloud bookkeeping (used by the sync engine) -----------------------
    def _find(self, where: str, args: Sequence[object]) -> Item | None:
        with self._lock:
            row = self._db.execute(
                f"SELECT * FROM items WHERE {where} ORDER BY id LIMIT 1", tuple(args)
            ).fetchone()
        return self._item(row) if row else None

    def find_by_cloud_id(self, cloud_id: str) -> Item | None:
        return self._find("cloud_id = ?", (cloud_id,)) if cloud_id else None

    def find_by_key(self, cloud_key: str) -> Item | None:
        return self._find("cloud_key = ?", (cloud_key,)) if cloud_key else None

    def find_by_key_prefix(self, prefix: str) -> Item | None:
        """An item whose cloud key starts with `prefix` (`type|time|`)."""
        if not prefix:
            return None
        return self._find("cloud_key != '' AND substr(cloud_key, 1, ?) = ?", (len(prefix), prefix))

    def find_text(self, text: str) -> Item | None:
        """The item holding exactly this text or link, if any."""
        raw = text.encode("utf-8", "replace")
        sha = _text_sha(raw)
        return self._find("sha = ?", (sha,))

    def mark_pushed(
        self,
        item_id: int,
        cloud_key: str,
        cloud_favorite: bool | None = None,
        updated_at: float | None = None,
        cloud_label: str | None = None,
        now: float | None = None,
    ) -> None:
        """Record that the account has this item (holding `cloud_label`, when given).

        `cloud_favorite` is the favorite state the account now holds (the current one
        when omitted). The item stays dirty when it changed after `updated_at` or its
        favorite still differs from the account's, so no change is ever lost.
        """
        with self._transaction() as db:
            row = db.execute(
                "SELECT favorite, updated_at FROM items WHERE id = ?", (item_id,)
            ).fetchone()
            if row is None:
                return
            held = bool(row["favorite"]) if cloud_favorite is None else cloud_favorite
            current = (updated_at is None or row["updated_at"] == updated_at) and bool(
                row["favorite"]
            ) == held
            db.execute(
                "UPDATE items SET cloud_key = ?, cloud_favorite = ?, dirty = ?, "
                "cloud_label = COALESCE(?, cloud_label), synced_at = ? WHERE id = ?",
                (
                    cloud_key,
                    int(held),
                    0 if current else 1,
                    cloud_label,
                    time.time() if now is None else now,
                    item_id,
                ),
            )

    def link(
        self,
        item_id: int,
        cloud_id: str,
        cloud_favorite: bool,
        cloud_key: str | None = None,
        now: float | None = None,
    ) -> None:
        """Tie an item to the account's copy. `cloud_key` is that copy's key (its time
        and text as the account holds them), which may differ from the local item's."""
        stamp = time.time() if now is None else now
        with self._transaction() as db:
            if cloud_key is None:
                db.execute(
                    "UPDATE items SET cloud_id = ?, cloud_favorite = ?, synced_at = ? WHERE id = ?",
                    (cloud_id, int(cloud_favorite), stamp, item_id),
                )
            else:
                db.execute(
                    "UPDATE items SET cloud_id = ?, cloud_favorite = ?, cloud_key = ?, "
                    "synced_at = ? WHERE id = ?",
                    (cloud_id, int(cloud_favorite), cloud_key, stamp, item_id),
                )

    def add_cloud(self, item: CloudItem, cloud_key: str, now: float | None = None) -> Item | None:
        """Insert an item the account has and this device does not (never sent back)."""
        raw = item.text.encode("utf-8", "replace")
        if not raw.strip() or len(raw) > MAX_TEXT_BYTES:
            return None
        clean = raw.decode("utf-8")
        kind = "url" if item.kind == "url" else "text"
        sha = _text_sha(raw)
        with self._transaction() as db:
            if db.execute("SELECT 1 FROM items WHERE sha = ?", (sha,)).fetchone():
                return None
            cursor = db.execute(
                "INSERT INTO items (kind, text, bytes, sha, created_at, updated_at, favorite, "
                "label, source, cloud_id, cloud_key, cloud_favorite, dirty, cloud_label, synced_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'cloud', ?, ?, ?, 0, ?, ?)",
                (
                    kind,
                    clean,
                    len(raw),
                    sha,
                    item.created_ms / 1000,
                    item.updated_at,
                    int(item.favorite),
                    item.label,
                    item.id,
                    cloud_key,
                    int(item.favorite),
                    item.label,
                    time.time() if now is None else now,
                ),
            )
            return self._fetch(db, int(cursor.lastrowid or 0))

    def apply_cloud(self, item_id: int, *, favorite: bool, label: str, updated_at: float) -> None:
        """Adopt the account's favorite and label (a change made elsewhere)."""
        with self._transaction() as db:
            db.execute(
                "UPDATE items SET favorite = ?, cloud_favorite = ?, label = ?, cloud_label = ?, "
                "updated_at = ?, dirty = 0 WHERE id = ?",
                (int(favorite), int(favorite), label, label, updated_at, item_id),
            )

    def pending_favorites(self, limit: int = 100) -> Items:
        """Linked items whose favorite differs from the account's."""
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM items WHERE cloud_id != '' AND favorite != cloud_favorite "
                "ORDER BY updated_at ASC, id ASC LIMIT ?",
                (max(1, limit),),
            ).fetchall()
        return [self._item(row) for row in rows]

    def pending_labels(self, limit: int = 100) -> Items:
        """Linked favorites (favorites on the account too) whose label differs from its."""
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM items WHERE cloud_id != '' AND favorite = 1 AND cloud_favorite = 1 "
                "AND label != cloud_label ORDER BY updated_at ASC, id ASC LIMIT ?",
                (max(1, limit),),
            ).fetchall()
        return [self._item(row) for row in rows]

    def mark_label(self, item_id: int, cloud_label: str) -> None:
        """Record the label the account now holds."""
        with self._transaction() as db:
            db.execute("UPDATE items SET cloud_label = ? WHERE id = ?", (cloud_label, item_id))

    def set_skip(self, item_id: int) -> None:
        """Never send this item (the account refused it or it is too large)."""
        with self._transaction() as db:
            db.execute("UPDATE items SET sync_skip = 1 WHERE id = ?", (item_id,))

    def delete_local(self, item_id: int) -> None:
        """Remove an item deleted elsewhere. The account is not told again."""
        self._remove([item_id], tombstones=False)

    def tombstones(self) -> Tombstones:
        with self._lock:
            rows = self._db.execute(
                "SELECT cloud_id, cloud_key, deleted_at FROM tombstones ORDER BY deleted_at"
            ).fetchall()
        return [Tombstone(row["cloud_id"], row["cloud_key"], row["deleted_at"]) for row in rows]

    def clear_tombstone(self, tombstone: Tombstone) -> None:
        with self._transaction() as db:
            db.execute(
                "DELETE FROM tombstones WHERE cloud_id = ? AND cloud_key = ? AND deleted_at = ?",
                (tombstone.cloud_id, tombstone.cloud_key, tombstone.deleted_at),
            )

    def reconcile_candidates(self, cutoff: float) -> Items:
        """Synced items with nothing unsent that the account confirmed at or before `cutoff`.

        These are the only items a missing account copy may remove here: text and links
        with an account id, no pending favorite, label or upload, no deletion waiting.
        """
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM items WHERE cloud_id != '' AND kind IN ('text', 'url') "
                "AND dirty = 0 AND favorite = cloud_favorite AND label = cloud_label "
                "AND synced_at <= ? AND cloud_id NOT IN (SELECT cloud_id FROM tombstones) "
                "ORDER BY id",
                (cutoff,),
            ).fetchall()
        return [self._item(row) for row in rows]

    def synced_count(self) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) FROM items WHERE cloud_id != '' AND kind IN ('text', 'url')"
            ).fetchone()
        return int(row[0])

    def relink_since(self, stamp: float) -> int:
        """Send again what was copied here after `stamp` and may have been cleared away.

        A copy made while an account clear was on its way can be uploaded and then
        deleted by that clear. It stays in this history, so it is sent as new.
        """
        with self._transaction() as db:
            cursor = db.execute(
                "UPDATE items SET cloud_id = '', cloud_key = '', cloud_favorite = 0, "
                "cloud_label = '', synced_at = 0, dirty = 1 WHERE cloud_id != '' "
                "AND source != 'cloud' AND kind IN ('text', 'url') AND created_at > ?",
                (stamp,),
            )
            return int(cursor.rowcount)

    def restore(self, item: Item, image: bytes = b"") -> Item | None:
        """Put back an item that was just deleted here (Undo), exactly as it was.

        While the account has not yet been told about the deletion, the pending
        tombstone is withdrawn and the item keeps its account link, so nothing is sent
        or deleted. Once the account was told, it comes back as a new item and uploads.
        """
        if item.kind == "image":
            restored = self.add_image(image, source=item.source, now=item.created_at)
        else:
            if self.find_text(item.text) is not None:
                return self.add_text(item.text, source=item.source)  # Copied again since.
            restored = self.add_text(item.text, source=item.source, now=item.created_at)
        if restored is None:
            return None
        with self._transaction() as db:
            held = False
            if item.cloud_id or item.cloud_key:
                cursor = db.execute(
                    "DELETE FROM tombstones WHERE rowid = (SELECT rowid FROM tombstones "
                    "WHERE cloud_id = ? AND cloud_key = ? LIMIT 1)",
                    (item.cloud_id, item.cloud_key),
                )
                held = cursor.rowcount > 0
            if held:
                db.execute(
                    "UPDATE items SET favorite = ?, label = ?, updated_at = ?, cloud_id = ?, "
                    "cloud_key = ?, cloud_favorite = ?, cloud_label = ?, synced_at = ?, "
                    "dirty = ? WHERE id = ?",
                    (
                        int(item.favorite),
                        item.label,
                        item.updated_at,
                        item.cloud_id,
                        item.cloud_key,
                        int(item.cloud_favorite),
                        item.cloud_label,
                        item.synced_at,
                        int(item.dirty),
                        restored.id,
                    ),
                )
            else:
                db.execute(
                    "UPDATE items SET favorite = ?, label = ?, updated_at = ? WHERE id = ?",
                    (
                        int(item.favorite),
                        item.label if item.favorite else "",
                        max(item.updated_at, restored.updated_at),
                        restored.id,
                    ),
                )
            return self._fetch(db, restored.id)

    def reset_sync(self) -> None:
        """Forget the account: every text and link is new to the next one you connect."""
        with self._transaction() as db:
            db.execute(
                "UPDATE items SET cloud_id = '', cloud_key = '', cloud_favorite = 0, "
                "cloud_label = '', synced_at = 0, sync_skip = 0, dirty = CASE WHEN kind IN ('text', 'url') THEN 1 ELSE 0 END"
            )
            db.execute("DELETE FROM tombstones")
            db.execute(
                "DELETE FROM meta WHERE key IN (?, ?, ?, ?, ?, ?)",
                (
                    META_CURSOR,
                    META_CLEAR,
                    META_CLEARED,
                    META_PRUNED,
                    META_RECONCILED,
                    META_RECONCILE_NOW,
                ),
            )

    def drop_tombstones(self) -> None:
        with self._transaction() as db:
            db.execute("DELETE FROM tombstones")

    def meta_get(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def meta_set(self, key: str, value: str) -> None:
        with self._transaction() as db:
            db.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )


def wipe(directory: Path) -> None:
    """Erase the whole history folder: the database (and its -wal and -shm), sets aside
    damaged copies, every image and thumbnail, then the folder itself. Used by
    `uninstall --purge`; nothing is left to recover, and the account is not told.

    Database files are overwritten with zeros before they are deleted (best effort on
    journaling file systems and SSDs). A missing folder is fine; a symbolic link is
    removed as the link it is, never followed.
    """
    if directory.is_symlink():
        directory.unlink()
        return
    if not directory.is_dir():
        return
    for entry in directory.iterdir():
        if entry.is_symlink() or entry.is_file():
            try:
                if entry.is_file() and not entry.is_symlink() and entry.name.startswith("clips.db"):
                    size = entry.stat().st_size
                    with entry.open("r+b") as stream:
                        stream.write(b"\0" * size)
                        stream.flush()
                        os.fsync(stream.fileno())
            except OSError:
                pass
            entry.unlink(missing_ok=True)
        elif entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
    try:
        directory.rmdir()
    except OSError:
        shutil.rmtree(directory, ignore_errors=True)


def _text_sha(raw: bytes) -> str:
    return hashlib.sha256(b"text\0" + raw).hexdigest()


def _fold(text: object) -> str:
    return text.casefold() if isinstance(text, str) else ""
