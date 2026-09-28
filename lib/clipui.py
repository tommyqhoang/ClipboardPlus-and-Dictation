"""The Clipboard tab of the window: search, filters, and the history list."""

from __future__ import annotations

import dataclasses
import functools
import time
import tkinter as tk
from collections.abc import Callable
from tkinter import messagebox, ttk
from typing import Any

import clipboardplus
import clipservice
import clipstore
import dictation as d
import hotkeys
import telemetry

PAGE_SIZE = 50
# Rows are drawn this many at a time: the first batch fills the window at once, the
# rest follow between frames, so opening or searching never waits on the whole list.
ROW_BATCH = 12
PREVIEW_CHARS = 140
SEARCH_DELAY_MS = 150
THUMB_PIXELS = 96
PAD = 20  # The window's side padding (app.PAD).
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
    frame: tk.Frame  # The row; destroying it removes the whole item from the list.
    star: tk.Label
    when: tk.Label  # "5 min ago · Desktop": the only part that changes with time.
    title: tk.Label  # A favorite's label, above its text; hidden when it has none.
    rename: tk.Label  # The "Add label" / "Edit label" action, offered on favorites only.
    body: tk.Label  # The item's text (or "Image W×H").
    paint: Callable[[str], None]  # Recolors the whole row.


def _same_look(old: clipstore.Item, new: clipstore.Item) -> bool:
    """Whether a drawn row still shows `new` correctly, apart from its time."""
    fields = ("kind", "text", "thumb_file", "favorite", "label", "source")
    return all(getattr(old, name) == getattr(new, name) for name in fields)


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
        self.selected = 0  # The row Enter copies; the arrow keys move it.
        self.deleted: tuple[clipstore.Item, bytes] | None = None
        # Decoded once per image and kept while shown (Tk drops unreferenced images).
        self._thumbs: dict[str, tk.PhotoImage | None] = {}
        self._signature: tuple[Any, ...] | None = None
        self._chips: dict[str, ttk.Button] = {}
        self.query.trace_add("write", lambda *_: self._query_changed())

    # -- layout ------------------------------------------------------------
    def render(self) -> None:
        """Build the controls (the caller has reset the page); then the list.

        Search and filters live in the window's fixed toolbar, so they stay in view
        while the list scrolls under them.
        """
        bar = self.app.show_toolbar((PAD, 12, PAD, 8))
        search = ttk.Frame(bar, style="Toolbar.TFrame")
        search.pack(fill="x")
        entry = self.entry = ttk.Entry(search, textvariable=self.query)
        entry.pack(fill="x")
        entry.bind("<Return>", lambda _: self.copy_selected())
        entry.bind("<Escape>", lambda _: self.escape())
        entry.bind("<Down>", lambda _: self.move(1))
        entry.bind("<Up>", lambda _: self.move(-1))
        # A placeholder (ttk has none): shown while the box is empty.
        self.placeholder = ttk.Label(
            search,
            text="Search your clipboard   ·   ↑ ↓ to choose, Enter to copy",
            style="Placeholder.TLabel",
        )
        self.placeholder.bind("<Button-1>", lambda _: entry.focus_set())
        self._show_placeholder()
        entry.focus_set()
        chips = ttk.Frame(bar, style="Toolbar.TFrame")
        chips.pack(fill="x", pady=(8, 0))
        self._chips = {}
        for name, label in FILTERS:
            chip = ttk.Button(
                chips,
                text=label,
                style="Small.TButton",
                takefocus=False,
                command=functools.partial(self.set_filter, name),
            )
            chip.pack(side="left", padx=(0, 4))
            self._chips[name] = chip
        # Beside the filters, so it is never below a long list.
        self.clear_button = ttk.Button(
            chips, text="Clear history", style="Small.TButton", command=self.clear
        )
        self.undo_button = ttk.Button(
            chips, text="Undo delete", style="Small.TButton", command=self.undo_delete
        )
        self.count_label = ttk.Label(chips, style="Hint.TLabel")
        self.count_label.pack(side="right", padx=(0, 8))
        telemetry.event("clipboard_open", picker=bool(getattr(self.app, "quick", False)))
        frame = self.app.frame
        self.banner = ttk.Frame(frame)
        self.banner.pack(fill="x")
        self.list_frame = ttk.Frame(frame)
        self.list_frame.pack(fill="x")
        self.card: tk.Frame | None = None
        self.rows = []  # Any earlier rows went with the page.
        self._queue: list[clipstore.Item] = []  # Rows still to draw, in order.
        self._spare: dict[int, Row] = {}  # Drawn rows kept for reuse by those.
        self._drawing: str | None = None
        self.footer = ttk.Frame(frame)
        self.footer.pack(fill="x", pady=(8, 0))
        self.reload()

    def fit(self, width: int) -> None:
        """Below this width the item count would push the filters off the row."""
        if width < 600:
            self.count_label.pack_forget()
        elif not self.count_label.winfo_manager():
            if self.clear_button.winfo_manager():
                self.count_label.pack(side="right", padx=(0, 8), after=self.clear_button)
            else:
                self.count_label.pack(side="right", padx=(0, 8))

    def _show_placeholder(self) -> None:
        if self.query.get():
            self.placeholder.place_forget()
        else:
            self.placeholder.place(in_=self.entry, x=8, rely=0.5, anchor="w")

    def focus_search(self) -> None:
        """Ready to type a search, with any earlier one selected so typing replaces it."""
        self.entry.focus_set()
        self.entry.select_range(0, "end")
        self.entry.icursor("end")

    # -- searching ---------------------------------------------------------
    def _query_changed(self) -> None:
        if hasattr(self, "placeholder"):
            self._show_placeholder()
        self._schedule_search()

    def _schedule_search(self) -> None:
        if self.pending_search is not None:
            self.app.root.after_cancel(self.pending_search)
        self.pending_search = self.app.root.after(SEARCH_DELAY_MS, self.run_pending_search)

    def run_pending_search(self) -> None:
        if self.pending_search is not None:
            self.app.root.after_cancel(self.pending_search)
            self.pending_search = None
        self.limit = PAGE_SIZE
        self.selected = 0
        self.reload()

    def set_query(self, text: str) -> None:
        self.query.set(text)
        self.run_pending_search()

    def set_filter(self, name: str) -> None:
        self.filter = name
        self.limit = PAGE_SIZE
        self.selected = 0
        self.reload()

    def show_all(self) -> None:
        """Leave a search or filter that found nothing."""
        self.query.set("")
        self.set_filter("all")

    # -- keyboard ----------------------------------------------------------
    def move(self, step: int) -> str:
        """Arrow keys: move the selection through the list, keeping it in view."""
        if self.pending_search is not None:
            self.run_pending_search()
        if self.rows:
            self.selected = max(0, min(len(self.rows) - 1, self.selected + step))
            self._paint_selection()
            self._reveal(self.rows[self.selected])
        return "break"

    def escape(self) -> str:
        """Escape clears a search; with nothing typed it closes a picker window."""
        if self.query.get():
            self.set_query("")
        elif getattr(self.app, "quick", False):
            self.app.close()
        return "break"

    def _paint_selection(self) -> None:
        for index, row in enumerate(self.rows):
            row.paint(self._resting(index))

    def _resting(self, index: int) -> str:
        colors = self.app.colors
        return str(colors["selected"] if index == self.selected else colors["surface"])

    def _reveal(self, row: Row) -> None:
        canvas, frame = self.app.canvas, self.app.frame
        self.app.root.update_idletasks()
        top = row.frame.winfo_rooty() - canvas.winfo_rooty()
        bottom = top + row.frame.winfo_height()
        if top >= 0 and bottom <= canvas.winfo_height():
            return
        offset = row.frame.winfo_rooty() - frame.winfo_rooty()
        height = max(1, frame.winfo_height())
        if top < 0:
            canvas.yview_moveto(max(0.0, (offset - 8) / height))
        else:
            spare = canvas.winfo_height() - row.frame.winfo_height() - 8
            canvas.yview_moveto(max(0.0, (offset - spare) / height))

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

    def _capture_problem(self) -> str:
        """What stops capture from working, as the clipboard service reported it."""
        status = clipservice.read_status(self.app.service.paths, self._clock)
        if status.get("state") != "error":
            return ""
        return str(status.get("message") or "Copies aren’t being saved right now.")

    def _current_signature(self) -> tuple[Any, ...]:
        newest = self.store.list(limit=1)
        marker = (newest[0].id, newest[0].updated_at) if newest else (0, 0.0)
        # The minute makes "5 min ago" keep up; unchanged rows are only relabeled.
        minute = int(self._clock() // 60)
        return (self.store.count(), marker, self._paused(), self._capture_problem(), minute)

    def refresh(self) -> None:
        """Redraw only when the history or the pause state changed."""
        if self._current_signature() != self._signature:
            self.reload()

    def reload(self) -> None:
        """Show the current history.

        Rows that still show the same item are kept and only moved into place, so a
        redraw never blanks the list: copying an item puts it back on top without the
        whole list flashing.
        """
        self._signature = self._current_signature()
        for child in self.banner.winfo_children() + self.list_frame.winfo_children():
            if child is not self.card:
                child.destroy()  # The paused banner and the empty-list hint.
        for name, chip in self._chips.items():
            chip.configure(
                style="Small.Primary.TButton" if name == self.filter else "Small.TButton"
            )
        problem = self._capture_problem()
        if problem:
            ttk.Label(
                self.banner,
                text=f"Clipboard capture isn’t working: {problem}",
                style="Error.TLabel",
                wraplength=self.app.wraplength,
            ).pack(anchor="w", pady=(0, 8))
        elif self._paused():
            ttk.Label(self.banner, text=self._pause_text(), style="Hint.TLabel").pack(
                side="left", pady=(0, 8)
            )
            ttk.Button(self.banner, text="Resume", style="Small.TButton", command=self.resume).pack(
                side="left", padx=8
            )
        items = self._items()
        shown = {item.image_file for item in items if item.kind == "image"}
        self._thumbs = {name: photo for name, photo in self._thumbs.items() if name in shown}
        self._stop_drawing()
        drawn = {row.item.id: row for row in self.rows} | self._spare
        self.rows = []
        made = 0
        for index, item in enumerate(items):
            row = drawn.get(item.id)
            if (row is None or not _same_look(row.item, item)) and made >= ROW_BATCH:
                # The rest are drawn after this frame; their old rows wait to be reused.
                for later in items[index:]:
                    old = drawn.get(later.id)
                    if old is not None:
                        old.frame.pack_forget()
                self._queue = list(items[index:])
                break
            drawn.pop(item.id, None)
            made += self._place_row(row, item)
        self._spare = {item.id: drawn.pop(item.id) for item in self._queue if item.id in drawn}
        for row in drawn.values():
            row.frame.destroy()
        self._order()
        if self._queue:
            self._drawing = self.app.root.after(1, self._draw_more)
        if not self.rows and self.card is not None:
            self.card.destroy()  # An empty outline would sit above the hint.
            self.card = None
        self.selected = max(0, min(self.selected, len(self.rows) - 1))
        self._paint_selection()
        self._finish(len(items))

    def _place_row(self, row: Row | None, item: clipstore.Item) -> int:
        """Keep a row that still looks right, or draw it anew (1 when drawn)."""
        if row is not None and _same_look(row.item, item):
            row.item = item
            row.when.configure(text=self._meta(item))
            self.rows.append(row)
            return 0
        if row is not None:
            row.frame.destroy()
        self._add_row(item)
        return 1

    def _draw_more(self) -> None:
        """Draw the next batch of rows below those already shown."""
        self._drawing = None
        if not self.list_frame.winfo_exists():
            return
        batch, self._queue = self._queue[:ROW_BATCH], self._queue[ROW_BATCH:]
        for item in batch:
            row = self._spare.pop(item.id, None)
            self._place_row(row, item)
            if row is not None and row in self.rows:
                row.frame.pack(fill="x", pady=(1, 0))  # Back in place, at the end.
        if self._queue:
            self._drawing = self.app.root.after(1, self._draw_more)
        else:
            for row in self._spare.values():
                row.frame.destroy()
            self._spare = {}

    def _flush(self) -> None:
        """Draw every waiting row now (before work that needs the whole list)."""
        while self._queue:
            self._draw_more()
        self._stop_drawing()

    def _stop_drawing(self) -> None:
        if self._drawing is not None:
            self.app.root.after_cancel(self._drawing)
            self._drawing = None
        self._queue = []

    def _pause_text(self) -> str:
        until = hotkeys.Preferences(self.app.service.paths).clipboard().paused_until
        if until and until < self._clock() + 86400:
            return f"Capture is paused until {time.strftime('%H:%M', time.localtime(until))}."
        return "Capture is paused until you resume it."

    def _order(self) -> None:
        """Pack the rows in list order, touching the layout only when it changed."""
        if self.card is None or not self.rows:
            return
        wanted = [str(row.frame) for row in self.rows]
        packed = [str(widget) for widget in self.card.pack_slaves()]
        if [name for name in packed if name in set(wanted)] == wanted:
            return
        for row in self.rows:
            row.frame.pack_forget()
        for row in self.rows:
            row.frame.pack(fill="x", pady=(1, 0))

    def _finish(self, shown: int) -> None:
        """The empty-list hint and the footer, which depend on how many rows there are."""
        if not shown:
            self._empty()
        for child in self.footer.winfo_children():
            child.destroy()
        if shown >= self.limit:
            ttk.Button(
                self.footer, text="Load more", style="Small.TButton", command=self.load_more
            ).pack()
        total = self.store.count()
        self.count_label.configure(text=f"{total} item{'s' if total != 1 else ''}" if total else "")
        if total:
            if not self.clear_button.winfo_manager():
                # Rightmost: packed ahead of the count when the count is shown.
                if self.count_label.winfo_manager():
                    self.clear_button.pack(side="right", before=self.count_label)
                else:
                    self.clear_button.pack(side="right")
        else:
            self.clear_button.pack_forget()

    def _empty(self) -> None:
        """What an empty list says: a search that found nothing, or how to begin."""
        empty = ttk.Frame(self.list_frame)
        empty.pack(fill="x", pady=(28, 8))
        if self.query.get().strip() or self.filter != "all":
            what = f"“{self.query.get().strip()}”" if self.query.get().strip() else "this filter"
            ttk.Label(empty, text=f"Nothing matches {what}.", style="Subtitle.TLabel").pack()
            ttk.Button(
                empty, text="Show everything", style="Small.TButton", command=self.show_all
            ).pack(pady=(10, 0))
            return
        images = hotkeys.Preferences(self.app.service.paths).clipboard().images
        ttk.Label(empty, text="Nothing copied yet", style="Title.TLabel").pack()
        ttk.Label(
            empty,
            text="Copy some text, a link"
            + (" or an image" if images else "")
            + " in any app and it shows up here, ready to search and paste again."
            + (" Images stay on this computer." if images else ""),
            style="Subtitle.TLabel",
            wraplength=min(460, self.app.wraplength),
            justify="center",
        ).pack(pady=(6, 0))

    def load_more(self) -> None:
        """Add the next page below the rows already drawn instead of redrawing them all."""
        self.limit += PAGE_SIZE
        self._flush()
        have = {row.item.id for row in self.rows}
        items = self._items()
        for item in items:
            if item.id not in have:
                self._add_row(item)
        self._finish(len(items))

    # -- rows --------------------------------------------------------------
    def _list_card(self) -> tk.Frame:
        """One outlined white panel holding every row, with hairlines between them."""
        if self.card is None or not self.card.winfo_exists():
            colors = self.app.colors
            self.card = tk.Frame(self.list_frame, background=colors["border"], padx=1)
            self.card.pack(fill="x")
            tk.Frame(self.card, height=1, background=colors["border"]).pack(side="bottom", fill="x")
        return self.card

    def _add_row(self, item: clipstore.Item) -> None:
        colors, fonts = self.app.colors, self.app.fonts
        surface = colors["surface"]
        row = tk.Frame(self._list_card(), background=surface, padx=12, pady=7, cursor="hand2")
        row.pack(fill="x", pady=(1, 0))
        painted: list[tk.Misc] = [row]  # Recolored on hover.
        clickable: list[tk.Misc] = [row]  # A click copies the item.
        if item.kind == "image":
            photo = self._thumbnail(item)
            if photo is not None:
                picture = tk.Label(row, image=photo, background=surface, cursor="hand2")
                picture.pack(side="left", padx=(0, 10))
                painted.append(picture)
                clickable.append(picture)
            text = f"Image {item.width}×{item.height}"
        else:
            text = preview(item.text)
        actions = tk.Frame(row, background=surface)
        actions.pack(side="right", padx=(10, 0))
        info = tk.Frame(row, background=surface, cursor="hand2")
        info.pack(side="left", fill="x", expand=True)
        label = tk.Label(
            info,
            text=text,
            background=surface,
            foreground=colors["text"],
            font=fonts["body"],
            justify="left",
            anchor="w",
            wraplength=self.app.wraplength - 170,
            cursor="hand2",
        )
        label.pack(anchor="w", fill="x")
        # Made after the text (so it is not taken for it) but shown above it.
        title = tk.Label(
            info,
            background=surface,
            foreground=colors["text"],
            font=fonts["heading"],
            justify="left",
            anchor="w",
            wraplength=self.app.wraplength - 170,
            cursor="hand2",
        )
        hint = tk.Label(
            info,
            text=self._meta(item),
            background=surface,
            foreground=colors["muted"],
            font=fonts["small"],
            cursor="hand2",
        )
        hint.pack(anchor="w")
        painted += [actions, info, label, title, hint]
        clickable += [info, label, title, hint]
        for widget in clickable:
            widget.bind("<Button-1>", lambda _: self.copy(item.id))
        star = self._action(actions, "★" if item.favorite else "☆", colors["star"], "icon")
        star.bind("<Button-1>", lambda _: self.toggle_favorite(item.id))
        rename = self._action(actions, "Label", colors["muted"])
        rename.bind("<Button-1>", lambda _: self.edit_label(item.id))
        copy = self._action(actions, "Copy", colors["accent"])
        copy.bind("<Button-1>", lambda _: self.copy(item.id))
        if item.kind != "image":
            view = self._action(actions, "View", colors["muted"])
            view.bind("<Button-1>", lambda _: self.view(item.id))
            painted.append(view)
        delete = self._action(actions, "Delete", colors["muted"], hover=colors["danger"])
        delete.bind("<Button-1>", lambda _: self.delete(item.id))
        painted += [star, rename, copy, delete]

        def paint(color: str) -> None:
            for widget in painted:
                widget.configure(background=color)  # type: ignore[call-arg]

        def left(_: object) -> None:
            # Moving onto a child also "leaves" the row: only repaint once really out.
            under = self.app.root.winfo_containing(*self.app.root.winfo_pointerxy())
            if under is None or not str(under).startswith(str(row)):
                index = self.rows.index(drawn) if drawn in self.rows else -1
                paint(self._resting(index))

        row.bind("<Enter>", lambda _: paint(colors["hover"]))
        row.bind("<Leave>", left)
        drawn = Row(item, row, star, hint, title, rename, label, paint)
        self._show_label(drawn)
        self.rows.append(drawn)

    def _meta(self, item: clipstore.Item) -> str:
        when = relative_time(item.created_at, self._clock())
        return f"{when} · {SOURCES.get(item.source, item.source)}"

    def _show_label(self, row: Row) -> None:
        """A favorite's label above its text, and the Label action on favorites only."""
        item = row.item
        if item.favorite and item.label:
            row.title.configure(text=item.label)
            if not row.title.winfo_manager():
                row.title.pack(anchor="w", fill="x", before=row.body)
        else:
            row.title.pack_forget()
        if item.favorite:
            if not row.rename.winfo_manager():
                row.rename.pack(side="left", after=row.star)
            row.rename.configure(text="Edit label" if item.label else "Add label")
        else:
            row.rename.pack_forget()

    def _action(
        self, parent: tk.Misc, text: str, color: str, font: str = "small", hover: str = ""
    ) -> tk.Label:
        """A light text button for a row; darker (or `hover`) under the pointer."""
        colors = self.app.colors
        link = tk.Label(
            parent,
            text=text,
            foreground=color,
            background=colors["surface"],
            font=self.app.fonts[font],
            cursor="hand2",
            padx=6,
        )
        link.pack(side="left")
        link.bind("<Enter>", lambda _: link.configure(foreground=hover or colors["text"]))
        link.bind("<Leave>", lambda _: link.configure(foreground=color))
        return link

    def _thumbnail(self, item: clipstore.Item) -> tk.PhotoImage | None:
        if item.image_file in self._thumbs:
            return self._thumbs[item.image_file]
        path = self.store.thumb_path(item) or self.store.image_path(item)
        photo: tk.PhotoImage | None
        try:
            photo = tk.PhotoImage(master=self.app.root, file=str(path))
        except tk.TclError:
            photo = None
        else:
            shrink = max(1, max(photo.width(), photo.height()) // THUMB_PIXELS)
            if shrink > 1:
                photo = photo.subsample(shrink)
        self._thumbs[item.image_file] = photo
        return photo

    def _row(self, item_id: int) -> Row | None:
        return next((row for row in self.rows if row.item.id == item_id), None)

    def _remove_row(self, row: Row) -> None:
        row.frame.destroy()
        self.rows.remove(row)
        if not self.rows:
            self.reload()  # Older items, or the empty-history hint, take its place.

    # -- actions -----------------------------------------------------------
    def toggle_favorite(self, item_id: int) -> None:
        """Flip the star in place; only the favorites filter loses the row."""
        item = self.store.get(item_id)
        row = self._row(item_id)
        if item is None:
            if row is not None:
                self._remove_row(row)
            return
        self.store.set_favorite(item_id, not item.favorite)
        telemetry.event("clipboard_favorite", on=not item.favorite)
        self._signature = self._current_signature()  # Our own change needs no redraw.
        if row is None:
            return
        # Unstarring also drops the label (only favorites have one).
        row.item = self.store.get(item_id) or row.item
        if self.filter == "favorites" and not row.item.favorite:
            self._remove_row(row)
        else:
            row.star.configure(text="★" if row.item.favorite else "☆")
            self._show_label(row)

    def ask_label(self, current: str) -> str | None:
        """The new label ("" removes it), or None when the user cancels."""
        return LabelDialog(self.app.root, current).show()

    def edit_label(self, item_id: int) -> None:
        item = self.store.get(item_id)
        if item is None or not item.favorite:
            return
        label = self.ask_label(item.label)
        if label is None:
            return
        self.store.set_label(item_id, label)
        action = "remove" if not label else "edit" if item.label else "add"
        telemetry.event("clipboard_label", action=action)
        self._signature = self._current_signature()
        row = self._row(item_id)
        if row is not None:
            row.item = self.store.get(item_id) or row.item
            self._show_label(row)

    def copy_selected(self) -> str:
        """Enter: copy the chosen row (the top one until the arrow keys move)."""
        if self.pending_search is not None:
            self.run_pending_search()
        if self.rows:
            self.copy(self.rows[min(self.selected, len(self.rows) - 1)].item.id)
        return "break"

    def copy(self, item_id: int) -> None:
        item = self.store.get(item_id)
        if item is None:
            return
        try:
            self.app.service.copy_item(item, self.store)
        except d.DictationError as exc:
            self.app.status.set(str(exc))
            return
        telemetry.event("clipboard_copy", kind=item.kind, favorite=item.favorite)
        self.app.status.set("Copied. Paste it anywhere.")

    def delete(self, item_id: int) -> None:
        item = self.store.get(item_id)
        if item is None:
            return
        if item is not None and item.favorite:
            what = f"“{item.label}”" if item.label else "this favorite"
            if not messagebox.askyesno(
                "Delete favorite?",
                f"Delete {what}? You can undo the last deletion while this page is open.",
                parent=self.app.root,
            ):
                return
        data = b""
        if item.kind == "image":
            path = self.store.image_path(item)
            if path is not None:
                try:
                    data = path.read_bytes()
                except OSError as exc:
                    self.app.status.set(f"Couldn’t delete this image safely: {exc}")
                    return
        self.store.delete(item_id)
        self.deleted = (item, data)
        self.undo_button.pack(side="left", padx=(4, 0))
        self.app.status.set(
            "Deleted. Undo is available until you leave this page or delete another item."
        )
        telemetry.event("clipboard_delete", favorite=bool(item and item.favorite))
        self._signature = self._current_signature()
        row = self._row(item_id)
        if row is not None:
            self._remove_row(row)

    def undo_delete(self) -> None:
        if self.deleted is None:
            return
        item, data = self.deleted
        restored = (
            self.store.add_image(data, source=item.source)
            if item.kind == "image"
            else self.store.add_text(item.text, source=item.source)
        )
        if restored is None:
            self.app.status.set("Couldn’t restore this item.")
            return
        if item.favorite:
            self.store.set_favorite(restored.id, True)
            self.store.set_label(restored.id, item.label)
        self.deleted = None
        self.undo_button.pack_forget()
        self.reload()
        self.app.status.set("Item restored.")

    def view(self, item_id: int) -> None:
        item = self.store.get(item_id)
        if item is not None and item.kind != "image":
            TextPreview(
                self.app.root, item.label or "Clipboard text", item.text, lambda: self.copy(item_id)
            )

    def ask_clear(self, linked: bool) -> tuple[bool, bool] | None:
        """(everywhere, keep favorites), or None when the user cancels."""
        return ClearDialog(self.app.root, linked=linked).show()

    def clear(self) -> None:
        choice = self.ask_clear(self.app.service.clipboard_plus_linked())
        if choice is None:
            return
        everywhere, keep_favorites = choice
        self.deleted = None
        self.undo_button.pack_forget()
        count = self.app.service.clear_clipboard(
            self.store, everywhere=everywhere, keep_favorites=keep_favorites
        )
        telemetry.event(
            "clipboard_clear", everywhere=everywhere, keep_favorites=keep_favorites, count=count
        )
        self.reload()
        where = " here and on your Clipboard+ account" if everywhere else ""
        self.app.status.set(f"Cleared {count} item{'s' if count != 1 else ''}{where}.")

    def resume(self) -> None:
        prefs = hotkeys.Preferences(self.app.service.paths)
        prefs.save(clipboard=dataclasses.replace(prefs.clipboard(), paused_until=0.0))
        self.reload()


class TextPreview:
    """A selectable, scrollable full-text preview without changing the clipboard."""

    def __init__(
        self,
        parent: tk.Misc,
        title: str,
        text: str,
        copy: Callable[[], None],
        copy_label: str = "Copy",
    ) -> None:
        window = self.window = tk.Toplevel(parent)
        window.title(title)
        window.transient(parent)  # type: ignore[call-overload]
        window.geometry("640x460")
        body = ttk.Frame(window, padding=16)
        body.pack(fill="both", expand=True)
        area = ttk.Frame(body)
        area.pack(fill="both", expand=True)
        content = self.text = tk.Text(area, wrap="word", padx=10, pady=10)
        scroll = ttk.Scrollbar(area, orient="vertical", command=content.yview)
        content.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        content.pack(side="left", fill="both", expand=True)
        content.insert("1.0", text)
        content.configure(state="disabled")
        row = ttk.Frame(body)
        row.pack(fill="x", pady=(12, 0))
        self.copy_button = ttk.Button(row, text=copy_label, command=lambda: self.copied(copy))
        self.copy_button.pack(side="left")
        ttk.Button(row, text="Close", command=window.destroy).pack(side="right")
        self.copy_label = copy_label
        window.bind("<Escape>", lambda _: window.destroy())

    def copied(self, copy: Callable[[], None]) -> None:
        """Copy, and say so here: the main window's status is hidden behind this one."""
        copy()
        self.copy_button.configure(text="Copied")
        self.window.after(1500, self.restore)

    def restore(self) -> None:
        if self.copy_button.winfo_exists():
            self.copy_button.configure(text=self.copy_label)


class DisconnectDialog:
    """Explicit choices for local history; Cancel is the initial keyboard action."""

    def __init__(self, parent: tk.Misc) -> None:
        self.result: bool | None = None
        window = self.window = tk.Toplevel(parent)
        window.title("Disconnect Clipboard+?")
        window.transient(parent)  # type: ignore[call-overload]
        body = ttk.Frame(window, padding=22)
        body.pack(fill="both", expand=True)
        ttk.Label(
            body,
            text="Your Clipboard+ account keeps its copy. Choose what happens "
            "to history on this computer. Deleting it can’t be undone.",
            wraplength=400,
        ).pack(pady=(0, 16))
        for label, result in (
            ("Disconnect and keep local history", True),
            ("Disconnect and delete local history", False),
            ("Cancel", None),
        ):
            button = ttk.Button(
                body,
                text=label,
                style="Danger.TButton" if result is False else "TButton",
                command=functools.partial(self.choose, result),
            )
            button.pack(fill="x", pady=3)
        button.focus_set()
        window.protocol("WM_DELETE_WINDOW", lambda: self.choose(None))
        window.bind("<Escape>", lambda _: self.choose(None))

    def choose(self, result: bool | None) -> None:
        self.result = result
        self.window.destroy()

    def show(self) -> bool | None:
        self.window.grab_set()
        self.window.wait_window()
        return self.result


class ClearDialog:
    """Asks what "Clear history" should clear: where, and whether favorites stay."""

    def __init__(self, parent: tk.Misc, *, linked: bool) -> None:
        self.result: tuple[bool, bool] | None = None
        window = self.window = tk.Toplevel(parent)
        window.title("Clear clipboard history?")
        window.resizable(False, False)
        window.transient(parent)  # type: ignore[call-overload]
        window.configure(background=ttk.Style(window).lookup("TFrame", "background"))
        # The narrower choice is the default: clearing the account too is a deliberate pick.
        self.scope = tk.StringVar(master=window, value="device")
        self.keep_favorites = tk.BooleanVar(master=window, value=True)
        body = ttk.Frame(window, padding=22)
        body.pack(fill="both", expand=True)
        ttk.Label(
            body,
            text="Text, links and copied images are deleted. This can’t be undone.",
            wraplength=380,
        ).pack(anchor="w", pady=(0, 10))
        if linked:
            ttk.Radiobutton(
                body,
                text="Everywhere: this device and your Clipboard+ account",
                value="everywhere",
                variable=self.scope,
            ).pack(anchor="w")
            ttk.Radiobutton(
                body, text="This device only", value="device", variable=self.scope
            ).pack(anchor="w", pady=(2, 8))
        ttk.Checkbutton(body, text="Keep favorites", variable=self.keep_favorites).pack(
            anchor="w", pady=(0, 14)
        )
        row = ttk.Frame(body)
        row.pack(fill="x")
        ttk.Button(row, text="Clear history", style="Danger.TButton", command=self.confirm).pack(
            side="right"
        )
        cancel = ttk.Button(row, text="Cancel", command=self.cancel)
        cancel.pack(side="right", padx=(0, 8))
        cancel.focus_set()  # Enter on its own never deletes anything.
        window.protocol("WM_DELETE_WINDOW", self.cancel)
        window.bind("<Escape>", lambda _: self.cancel())
        window.bind("<Return>", lambda _: self._press())

    def _press(self) -> None:
        focused = self.window.focus_get()
        if isinstance(focused, ttk.Button):
            focused.invoke()

    def confirm(self) -> None:
        self.result = (self.scope.get() == "everywhere", bool(self.keep_favorites.get()))
        self.window.destroy()

    def cancel(self) -> None:
        self.result = None
        self.window.destroy()

    def show(self) -> tuple[bool, bool] | None:
        self.window.grab_set()
        self.window.wait_window()
        return self.result


class LabelDialog:
    """Asks for a favorite's label: add, change or remove it."""

    def __init__(self, parent: tk.Misc, current: str) -> None:
        self.result: str | None = None
        window = self.window = tk.Toplevel(parent)
        window.title("Edit label" if current else "Add label")
        window.resizable(False, False)
        window.transient(parent)  # type: ignore[call-overload]
        window.configure(background=ttk.Style(window).lookup("TFrame", "background"))
        self.text = tk.StringVar(master=window, value=current)
        body = ttk.Frame(window, padding=22)
        body.pack(fill="both", expand=True)
        ttk.Label(
            body,
            text="A short name for this favorite. Search finds it by its label too.",
            wraplength=360,
        ).pack(anchor="w", pady=(0, 8))
        entry = ttk.Entry(body, textvariable=self.text, width=44)
        entry.pack(fill="x", pady=(0, 14))
        entry.select_range(0, "end")
        entry.focus_set()
        entry.bind("<Return>", lambda _: self.save())
        row = ttk.Frame(body)
        row.pack(fill="x")
        ttk.Button(row, text="Save", style="Primary.TButton", command=self.save).pack(side="right")
        ttk.Button(row, text="Cancel", command=self.cancel).pack(side="right", padx=(0, 8))
        if current:
            ttk.Button(row, text="Remove label", command=self.remove).pack(side="left")
        window.protocol("WM_DELETE_WINDOW", self.cancel)
        window.bind("<Escape>", lambda _: self.cancel())

    def save(self) -> None:
        self.result = self.text.get().strip()
        self.window.destroy()

    def remove(self) -> None:
        self.result = ""
        self.window.destroy()

    def cancel(self) -> None:
        self.result = None
        self.window.destroy()

    def show(self) -> str | None:
        self.window.grab_set()
        self.window.wait_window()
        return self.result


class AccountCard:
    """The Clipboard+ account card: sign in, sign up, use a key, sync, disconnect."""

    HINT = (
        "Keep your clipboard history on your Clipboard+ account too, so it is also on the "
        "website and in the browser extension. Optional: only text and links are sent, "
        "never images or audio."
    )

    def __init__(self, app: Any, clock: Callable[[], float] = time.time) -> None:
        self.app, self._clock = app, clock
        self.email = tk.StringVar(master=app.root)
        self.password = tk.StringVar(master=app.root)
        self.key = tk.StringVar(master=app.root)
        self.mode = "account"  # or "key": paste an API key instead
        self.error = ""
        self._buttons: list[ttk.Button] = []
        self._signature: tuple[Any, ...] | None = None
        self.body = app.card("Clipboard+ account", self.HINT)
        self.area = ttk.Frame(self.body, style="Card.TFrame")
        self.area.pack(fill="x")
        self.render()

    # -- drawing -----------------------------------------------------------
    def _signature_now(self) -> tuple[Any, ...]:
        account = self.app.service.clipboard_plus_state()
        # The "synced 5 min ago" wording changes with time, not only with state.
        bucket = int(self._clock() // 20) if account.sync == "ok" else 0
        return (account, self.app.features().clipboard, bucket)

    def refresh(self) -> None:
        """Redraw only when what the card shows has changed."""
        if self.area.winfo_exists() and self._signature_now() != self._signature:
            self.render()

    def render(self) -> None:
        self._signature = self._signature_now()
        account = self.app.service.clipboard_plus_state()
        self.app.buttons = [b for b in self.app.buttons if b not in self._buttons]
        self._buttons = []
        for child in self.area.winfo_children():
            child.destroy()
        if account.kind != "disconnected" and not self.app.features().clipboard:
            self._idle(account)
        elif account.kind == "connected":
            self._connected(account)
        else:
            self._disconnected(account.kind == "reconnect")
        if self.error:
            self._label(self.error, "CardError.TLabel", pady=(8, 0))

    def _label(self, text: str, style: str = "Card.TLabel", pady: tuple[int, int] = (0, 0)) -> None:
        ttk.Label(self.area, text=text, style=style, wraplength=self.app.wraplength - 50).pack(
            anchor="w", pady=pady
        )

    def _row(self, *rows: tuple[tuple[str, Callable[[], None]], ...], pady: int = 10) -> None:
        """One or more rows of buttons (a long row would run off the card)."""
        for actions in rows:
            row = ttk.Frame(self.area, style="Card.TFrame")
            row.pack(fill="x", pady=(pady, 0))
            pady = 6
            for text, command in actions:
                self._buttons.append(self.app.button(text, command, parent=row, side="left"))

    def _entry(self, caption: str, variable: tk.StringVar, secret: bool = False) -> ttk.Entry:
        self._label(caption, "CardHint.TLabel", pady=(6, 2))
        entry = ttk.Entry(self.area, textvariable=variable, show="•" if secret else "")
        entry.pack(fill="x")
        return entry

    def _disconnected(self, reconnect: bool) -> None:
        if reconnect:
            self._label("Reconnect needed", "CardHeading.TLabel")
            self._label(
                "Clipboard+ no longer accepts the saved key. Sign in again to keep syncing.",
                pady=(2, 4),
            )
        else:
            self._label("Not connected", "CardHeading.TLabel")
        if self.mode == "key":
            self._label(
                "Click Get a key, create a key for the desktop app on your "
                "Clipboard+ Account page, then paste the key here.",
                "CardHint.TLabel",
                pady=(4, 4),
            )
            entry = ttk.Entry(self.area, textvariable=self.key, show="•")
            entry.pack(fill="x")
            entry.bind("<Return>", lambda _: self.connect_key())
            rows = [
                (("Connect", self.connect_key),),
                (
                    ("Use email and password instead", lambda: self.switch("account")),
                    ("Get a key", lambda: hotkeys.open_link(clipboardplus.ACCOUNT_URL)),
                ),
            ]
        else:
            email = self._entry("Email", self.email)
            password = self._entry("Password", self.password, secret=True)
            email.bind("<Return>", lambda _: password.focus_set())
            password.bind("<Return>", lambda _: self.sign_in(create=False))
            self._label(
                "Signed up with Google, or have no password? Use an API key instead.",
                "CardHint.TLabel",
                pady=(6, 0),
            )
            rows = [
                (
                    *(
                        ()
                        if reconnect
                        else (("Create account", lambda: self.sign_in(create=True)),)
                    ),
                    ("Sign in", lambda: self.sign_in(create=False)),
                ),
                (
                    ("Use an API key instead", lambda: self.switch("key")),
                    ("Get Clipboard+", lambda: hotkeys.open_link()),
                ),
            ]
        if reconnect:
            rows.append((("Disconnect", self.disconnect),))
        self._row(*rows)

    def _connected(self, account: Any) -> None:
        who = f"Connected as {account.email}" if account.email else "Connected"
        self._label(who, "CardHeading.TLabel")
        self._label(self._sync_text(account), pady=(2, 0))
        self._row(
            (
                ("Sync now", self.sync_now),
                ("Open history", lambda: hotkeys.open_link(clipboardplus.DASHBOARD_URL)),
                ("Disconnect", self.disconnect),
            )
        )

    def _idle(self, account: Any) -> None:
        """Linked while Clipboard history is off: nothing syncs until it is turned on."""
        who = f"Connected as {account.email}" if account.email else "Connected"
        self._label(who, "CardHeading.TLabel")
        self._label(
            "Turn on Clipboard history above to keep syncing. Transcripts are no longer "
            "uploaded on their own.",
            pady=(2, 0),
        )
        self._row((("Disconnect", self.disconnect),))

    def _sync_text(self, account: Any) -> str:
        if account.sync == "ok" and account.synced:
            return "Synced " + relative_time(account.synced, self._clock())
        return {
            "syncing": "Syncing…",
            "offline": "Offline. Will retry automatically.",
            "error": "Clipboard+ is having trouble. Will retry automatically.",
        }.get(account.sync, "Waiting for the clipboard service to start.")

    # -- actions -----------------------------------------------------------
    def switch(self, mode: str) -> None:
        self.mode, self.error = mode, ""
        self.render()

    def _attempt(
        self, work: Callable[[], None], message: str, success: str, what: str = ""
    ) -> None:
        """Run `work` off the UI thread; a refusal is shown inside the card."""
        self.error = ""

        def run() -> str:
            try:
                work()
            except d.DictationError as exc:
                return str(exc)
            return ""

        def done(problem: str) -> None:
            self.password.set("")  # Never left on screen, whatever happened.
            if not self.area.winfo_exists():
                return
            self.error = problem
            if problem == clipboardplus.GOOGLE_ONLY:
                self.mode = "key"  # Google accounts have no password here: show the key form.
            telemetry.event(f"account_{what or 'change'}", ok=not problem)
            if not problem:
                self.key.set("")  # The key is saved privately.
                self.mode = "account"
                self.app.status.set(success)
            self.render()

        self.app.submit(run, done, message)

    def sign_in(self, *, create: bool) -> None:
        email, password = self.email.get(), self.password.get()
        service = self.app.service
        self._attempt(
            lambda: service.sign_in_clipboard_plus(email, password, create=create),
            "Creating your account…" if create else "Signing in…",
            "Connected to Clipboard+.",
            "created" if create else "signed_in",
        )

    def connect_key(self) -> None:
        key, service = self.key.get(), self.app.service
        self._attempt(
            lambda: service.connect_clipboard_plus(key),
            "Checking your Clipboard+ key…",
            "Connected to Clipboard+.",
            "key_connected",
        )

    def sync_now(self) -> None:
        self.app.service.sync_clipboard_now()
        self.app.status.set("Syncing…")

    def disconnect(self) -> None:
        keep = DisconnectDialog(self.app.root).show()
        if keep is None:
            return
        service = self.app.service
        self._attempt(
            lambda: service.disconnect_clipboard_plus(keep_history=bool(keep)),
            "Disconnecting…",
            "Disconnected from Clipboard+.",
            "disconnected",
        )
