"""Two-way sync between the local clipboard history and a Clipboard+ account.

Pure logic over a `Store` and a cloud client, so it runs (and is tested) without a
network. One round: apply a pending "clear everywhere", send new text and links, pull
what the account has (adding, linking and applying changes and deletions), tell the
account about local deletions, then mirror changed favorites. Every step is safe to
repeat, so an interrupted round is simply run again.
"""

from __future__ import annotations

import random
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

import clipboardplus as cp
import clipstore

INTERVAL_SECONDS = 60.0  # Between rounds while everything is fine.
OVERLAP_SECONDS = 120.0  # A pull starts this long before the last success.
BACKOFF_START_SECONDS = 30.0
BACKOFF_MAX_SECONDS = 600.0
MAX_PUSH_BATCHES = 10  # Per round: up to 1000 items, then the next round continues.
CURSOR = clipstore.META_CURSOR
CLEAR_PENDING = clipstore.META_CLEAR


class CloudClient(Protocol):
    def pull(self, since: float | None) -> cp.Pull: ...
    def push(self, items: list[clipstore.Item]) -> None: ...
    def toggle_favorite(self, cloud_id: str) -> bool | None: ...
    def delete(self, cloud_id: str) -> None: ...
    def clear(self, *, favorites: bool) -> None: ...


@dataclass
class Report:
    pushed: int = 0
    pulled: int = 0
    deleted: int = 0
    errors: list[str] = field(default_factory=list)


def request_clear(store: clipstore.Store, *, favorites: bool) -> None:
    """Ask for the account's history to be cleared too (done on the next round)."""
    store.meta_set(CLEAR_PENDING, "all" if favorites else "keep")


def _stamp(key: str) -> str:
    """`type|time|` of a sync key: enough to find an item the server rewrote."""
    return "|".join(key.split("|", 2)[:2]) + "|"


class Engine:
    def __init__(
        self,
        store: clipstore.Store,
        cloud: CloudClient,
        clock: Callable[[], float] = time.time,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self._store = store
        self._cloud = cloud
        self._clock = clock
        self._jitter = jitter
        self._failures = 0
        self._delay: float | None = None
        # idle (not run yet), ok, offline, error, or auth (the key was refused).
        self.state = "idle"

    def retry_delay(self) -> float | None:
        """Seconds to wait before trying again after a failure; None when healthy."""
        return self._delay

    def run_once(self) -> Report:
        report = Report()
        if self.state == "auth":
            return report  # Stopped until the user reconnects (a new engine).
        started = self._clock()
        try:
            self._round(report)
        except cp.AuthError:
            self.state = "auth"
            report.errors.append("Clipboard+ no longer accepts this key.")
            return report
        except (cp.SyncError, sqlite3.Error) as exc:
            self._failures += 1
            base = min(BACKOFF_MAX_SECONDS, BACKOFF_START_SECONDS * 2 ** (self._failures - 1))
            self._delay = base * (0.8 + 0.4 * self._jitter())
            offline = isinstance(exc, cp.SyncError) and exc.status is None
            self.state = "offline" if offline else "error"
            report.errors.append(str(exc))
            return report
        self._failures, self._delay, self.state = 0, None, "ok"
        self._store.meta_set(CURSOR, repr(started))
        return report

    # -- one round ---------------------------------------------------------
    def _round(self, report: Report) -> None:
        self._clear()
        self._push(report)
        pulled = self._pull(report)
        self._deletions(report, pulled)
        self._favorites()

    def _clear(self) -> None:
        mode = self._store.meta_get(CLEAR_PENDING)
        if not mode:
            return
        self._cloud.clear(favorites=mode == "all")
        # The account is now empty of everything deleted here, so those deletions
        # need not be sent one by one.
        self._store.drop_tombstones()
        self._store.meta_set(CLEAR_PENDING, "")

    def _push(self, report: Report) -> None:
        for _ in range(MAX_PUSH_BATCHES):
            batch = self._store.dirty(cp.BATCH)
            sendable = []
            for item in batch:
                if len(item.text.encode("utf-8")) > cp.MAX_BYTES:
                    self._store.set_skip(item.id)
                else:
                    sendable.append(item)
            for item in self._send(sendable):
                self._store.mark_pushed(
                    item.id,
                    cp.sync_key(item.kind, cp.created_ms(item.created_at), item.text),
                    # An upload can switch a favorite on but never off.
                    cloud_favorite=item.cloud_favorite or item.favorite,
                    updated_at=item.updated_at,
                )
                report.pushed += 1
            if len(batch) < cp.BATCH:
                return

    def _send(self, items: list[clipstore.Item]) -> list[clipstore.Item]:
        """The items the account accepted. Refused ones are skipped for good."""
        if not items:
            return []
        try:
            self._cloud.push(items)
        except cp.SyncError as exc:
            if exc.status not in (400, 413):
                raise
            if len(items) == 1:
                self._store.set_skip(items[0].id)
                return []
            # One bad item refuses the whole batch: find it by sending them singly.
            return [accepted for item in items for accepted in self._send([item])]
        return items

    def _pull(self, report: Report) -> cp.Pull:
        cursor = self._store.meta_get(CURSOR)
        since = float(cursor) - OVERLAP_SECONDS if cursor else None
        pulled = self._cloud.pull(since)
        # Items the user deleted here but the account has not heard about yet must
        # not come back.
        doomed = self._store.tombstones()
        ids = {t.cloud_id for t in doomed if t.cloud_id}
        stamps = {_stamp(t.cloud_key) for t in doomed if t.cloud_key}
        for cloud_item in sorted(pulled.items, key=lambda i: i.created_ms):
            key = cp.sync_key(cloud_item.kind, cloud_item.created_ms, cloud_item.text)
            if cloud_item.id in ids or _stamp(key) in stamps:
                continue
            if self._merge(cloud_item, key):
                report.pulled += 1
        for removed in pulled.deleted:
            gone = self._store.find_by_key(
                cp.sync_key(removed.kind, removed.created_ms, removed.prefix)
            ) or self._store.find_by_key_prefix(cp.sync_key(removed.kind, removed.created_ms, ""))
            if gone is not None:
                self._store.delete_local(gone.id)
                report.deleted += 1
        return pulled

    def _merge(self, cloud_item: cp.CloudItem, key: str) -> bool:
        """Fold one account item into the history. True when it was added."""
        local = self._store.find_by_cloud_id(cloud_item.id)
        if local is None:
            local = self._store.find_by_key(key) or self._store.find_by_key_prefix(_stamp(key))
            if local is None:
                # The same copy saved by another client has a different time; on this
                # device identical content is one item, whatever the time apart.
                local = self._store.find_text(cloud_item.text)
            if local is not None and local.cloud_id not in ("", cloud_item.id):
                return False  # The account holds this content twice; one is enough here.
        if local is None:
            return self._store.add_cloud(cloud_item, key) is not None

        pending = local.favorite != local.cloud_favorite
        if not local.cloud_key:
            # Never sent: the account already has it, so there is nothing to send.
            self._store.mark_pushed(
                local.id, key, cloud_favorite=cloud_item.favorite, updated_at=local.updated_at
            )
        if local.cloud_id != cloud_item.id:
            # The account's copy may carry another time or text than this device's
            # (an upload refused as a duplicate, a rewritten link): its key is the
            # one a later deletion will name.
            self._store.link(local.id, cloud_item.id, cloud_item.favorite, key)
        elif local.cloud_favorite != cloud_item.favorite:
            self._store.link(local.id, cloud_item.id, cloud_item.favorite)
        # A pending local change wins only when it is the newer one.
        keep_local = pending and local.updated_at >= cloud_item.updated_at
        if not keep_local and (
            local.favorite != cloud_item.favorite or local.label != cloud_item.label
        ):
            self._store.apply_cloud(
                local.id,
                favorite=cloud_item.favorite,
                label=cloud_item.label,
                updated_at=cloud_item.updated_at,
            )
        return False

    def _deletions(self, report: Report, pulled: cp.Pull) -> None:
        unresolved: list[clipstore.Tombstone] = []
        for tombstone in self._store.tombstones():
            cloud_id = tombstone.cloud_id or self._resolve(tombstone, pulled)
            if cloud_id:
                self._cloud.delete(cloud_id)  # 404 counts as done.
                self._store.clear_tombstone(tombstone)
                report.deleted += 1
            else:
                unresolved.append(tombstone)
        if not unresolved:
            return
        # Sent but never linked (the account's copy is older than the pull window):
        # look at everything once. Whatever is still not found is not there.
        everything = self._cloud.pull(None)
        for tombstone in unresolved:
            cloud_id = self._resolve(tombstone, everything)
            if cloud_id:
                self._cloud.delete(cloud_id)
                report.deleted += 1
            self._store.clear_tombstone(tombstone)

    @staticmethod
    def _resolve(tombstone: clipstore.Tombstone, pulled: cp.Pull) -> str:
        wanted = _stamp(tombstone.cloud_key) if tombstone.cloud_key else ""
        for cloud_item in pulled.items:
            key = cp.sync_key(cloud_item.kind, cloud_item.created_ms, cloud_item.text)
            if key == tombstone.cloud_key or (wanted and _stamp(key) == wanted):
                return cloud_item.id
        return ""

    def _favorites(self) -> None:
        for item in self._store.pending_favorites():
            state = self._cloud.toggle_favorite(item.cloud_id)
            # None: the account no longer has it; a later pull removes it here.
            self._store.link(item.id, item.cloud_id, item.favorite if state is None else state)
