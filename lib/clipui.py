"""The Clipboard tab of the window: search, filters, and the history list."""

from __future__ import annotations

import dataclasses
import functools
import time
import tkinter as tk
from collections.abc import Callable
from tkinter import messagebox, ttk
from typing import Any

import clipstore
import dictation as d
import hotkeys

PAGE_SIZE = 50
PREVIEW_CHARS = 140
SEARCH_DELAY_MS = 300
THUMB_PIXELS = 160
SOURCES = {"desktop": "Desktop", "dictation": "Dictation", "cloud": "Cloud"}
FILTERS = (("all", "All"), ("favorites", "Favorites"), ("images", "Images"), ("text", "Text"))


def relative_time(created: float, now: float) -> str:
    age = now - created
    if age < 45:
        return "just now"
    if age < 3600:
        return f"{max(1, round(age / 60))} min ago"
    if age < 86400:
        return f"{max(1, round(age / 3600))} h ago"
    if age < 172800:
        return "yesterday"
    moment = time.localtime(created)
    return f"{time.strftime('%b', moment)} {moment.tm_mday}"


def preview(text: str) -> str:
    """The text on one line, cut to a readable length."""
    flat = " ".join(text.split())
    return flat if len(flat) <= PREVIEW_CHARS else flat[:PREVIEW_CHARS] + "…"


@dataclasses.dataclass
class Row:
    item: clipstore.Item
    frame: tk.Misc


class ClipboardPage:
    """Owns the widgets of the Clipboard tab. `app` supplies the window and its helpers."""

    def __init__(
        self,
        app: Any,
        store: clipstore.Store,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.app, self.store, self._clock = app, store, clock
        self.rows: list[Row] = []
        self.query = tk.StringVar(master=app.root)
        self.filter = "all"
        self.limit = PAGE_SIZE
        self.pending_search: str | None = None
        self._photos: list[tk.PhotoImage] = []  # Tk drops images that are not referenced.
        self._signature: tuple[Any, ...] | None = None
        self._chips: dict[str, ttk.Button] = {}
        self.query.trace_add("write", lambda *_: self._schedule_search())

    # -- layout ------------------------------------------------------------
    def render(self) -> None:
        """Build the controls (the caller has reset the page); then the list."""
        frame = self.app.frame
        self.banner = ttk.Frame(frame)
        self.banner.pack(fill="x")
        ttk.Label(frame, text="Search your clipboard history", style="Hint.TLabel").pack(
            anchor="w", pady=(0, 4)
        )
        entry = ttk.Entry(frame, textvariable=self.query)
        entry.pack(fill="x", pady=(0, 8))
        chips = ttk.Frame(frame)
        chips.pack(fill="x", pady=(0, 10))
        self._chips = {}
        for name, label in FILTERS:
            chip = ttk.Button(
                chips,
                text=label,
                style="Small.TButton",
                command=functools.partial(self.set_filter, name),
            )
            chip.pack(side="left", padx=(0, 6))
            self._chips[name] = chip
        self.list_frame = ttk.Frame(frame)
        self.list_frame.pack(fill="x")
        self.footer = ttk.Frame(frame)
        self.footer.pack(fill="x", pady=(10, 0))
        self.reload()

    # -- searching ---------------------------------------------------------
    def _schedule_search(self) -> None:
        if self.pending_search is not None:
            self.app.root.after_cancel(self.pending_search)
        self.pending_search = self.app.root.after(SEARCH_DELAY_MS, self.run_pending_search)

    def run_pending_search(self) -> None:
        if self.pending_search is not None:
            self.app.root.after_cancel(self.pending_search)
            self.pending_search = None
        self.limit = PAGE_SIZE
        self.reload()

    def set_query(self, text: str) -> None:
        self.query.set(text)
        self.run_pending_search()

    def set_filter(self, name: str) -> None:
        self.filter = name
        self.limit = PAGE_SIZE
        self.reload()

    # -- data --------------------------------------------------------------
    def _items(self) -> list[clipstore.Item]:
        kind = {"images": "image", "text": "text"}.get(self.filter, "")
        return self.store.list(
            query=self.query.get(),
            kind=kind,
            favorites=self.filter == "favorites",
            limit=self.limit,
        )

    def _paused(self) -> bool:
        settings = hotkeys.Preferences(self.app.service.paths).clipboard()
        return settings.paused(self._clock())

    def _current_signature(self) -> tuple[Any, ...]:
        newest = self.store.list(limit=1)
        marker = (newest[0].id, newest[0].updated_at) if newest else (0, 0.0)
        return (self.store.count(), marker, self._paused())

    def refresh(self) -> None:
        """Redraw only when the history or the pause state changed."""
        if self._current_signature() != self._signature:
            self.reload()

    def reload(self) -> None:
        self._signature = self._current_signature()
        for child in self.banner.winfo_children() + self.list_frame.winfo_children():
            child.destroy()
        for child in self.footer.winfo_children():
            child.destroy()
        self._photos = []
        self.rows = []
        for name, chip in self._chips.items():
            chip.configure(
                style="Small.Primary.TButton" if name == self.filter else "Small.TButton"
            )
        if self._paused():
            ttk.Label(self.banner, text="Capture is paused.", style="Hint.TLabel").pack(
                side="left", pady=(0, 8)
            )
            ttk.Button(self.banner, text="Resume", style="Small.TButton", command=self.resume).pack(
                side="left", padx=8
            )
        items = self._items()
        for item in items:
            self._add_row(item)
        if not items:
            searching = bool(self.query.get().strip()) or self.filter != "all"
            text = (
                "No matches."
                if searching
                else "Copy something and it will appear here. Images stay on this computer."
            )
            ttk.Label(
                self.list_frame, text=text, style="Hint.TLabel", wraplength=self.app.wraplength
            ).pack(anchor="w", pady=20)
        if len(items) >= self.limit:
            ttk.Button(
                self.footer, text="Load more", style="Small.TButton", command=self.load_more
            ).pack(side="left")
        if self.store.count():
            ttk.Button(
                self.footer, text="Clear history", style="Small.TButton", command=self.clear
            ).pack(side="right")

    def load_more(self) -> None:
        self.limit += PAGE_SIZE
        self.reload()

    # -- rows --------------------------------------------------------------
    def _add_row(self, item: clipstore.Item) -> None:
        body = self.app.bordered(self.list_frame)
        info = ttk.Frame(body, style="Card.TFrame")
        info.pack(side="left", fill="x", expand=True)
        if item.kind == "image":
            photo = self._thumbnail(item)
            if photo is not None:
                ttk.Label(info, image=photo, style="Card.TLabel").pack(anchor="w")
            text = f"Image {item.width}×{item.height}"
        else:
            text = preview(item.text)
        ttk.Label(info, text=text, style="Card.TLabel", wraplength=self.app.wraplength - 250).pack(
            anchor="w"
        )
        meta = f"{relative_time(item.created_at, self._clock())} · {SOURCES.get(item.source, item.source)}"
        ttk.Label(info, text=meta, style="CardHint.TLabel").pack(anchor="w")
        actions = ttk.Frame(body, style="Card.TFrame")
        actions.pack(side="right")
        star = "★" if item.favorite else "☆"
        ttk.Button(
            actions,
            text=star,
            width=3,
            style="Small.TButton",
            command=lambda: self.toggle_favorite(item.id),
        ).pack(side="left", padx=2)
        ttk.Button(
            actions, text="Copy", width=6, style="Small.TButton", command=lambda: self.copy(item.id)
        ).pack(side="left", padx=2)
        ttk.Button(
            actions,
            text="Delete",
            width=7,
            style="Small.TButton",
            command=lambda: self.delete(item.id),
        ).pack(side="left", padx=2)
        self.rows.append(Row(item, body))

    def _thumbnail(self, item: clipstore.Item) -> tk.PhotoImage | None:
        path = self.store.thumb_path(item) or self.store.image_path(item)
        try:
            photo = tk.PhotoImage(master=self.app.root, file=str(path))
        except tk.TclError:
            return None
        shrink = max(1, max(photo.width(), photo.height()) // THUMB_PIXELS)
        if shrink > 1:
            photo = photo.subsample(shrink)
        self._photos.append(photo)
        return photo

    # -- actions -----------------------------------------------------------
    def toggle_favorite(self, item_id: int) -> None:
        item = self.store.get(item_id)
        if item is not None:
            self.store.set_favorite(item_id, not item.favorite)
        self.reload()

    def copy(self, item_id: int) -> None:
        item = self.store.get(item_id)
        if item is None:
            return
        try:
            self.app.service.copy_item(item, self.store)
        except d.DictationError as exc:
            self.app.status.set(str(exc))
        else:
            self.app.status.set("Copied to the clipboard.")

    def delete(self, item_id: int) -> None:
        self.store.delete(item_id)
        self.reload()

    def clear(self) -> None:
        if messagebox.askyesno(
            "Clear clipboard history?",
            "Everything is removed except your favorites. This can’t be undone.",
            parent=self.app.root,
        ):
            self.store.clear(keep_favorites=True)
            self.reload()

    def resume(self) -> None:
        prefs = hotkeys.Preferences(self.app.service.paths)
        prefs.save(clipboard=dataclasses.replace(prefs.clipboard(), paused_until=0.0))
        self.reload()
