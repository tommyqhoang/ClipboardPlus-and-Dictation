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
import clipstore
import dictation as d
import hotkeys

PAGE_SIZE = 50
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
    frame: tk.Misc  # The row; destroying it removes the whole item from the list.
    star: tk.Label


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
        entry.bind("<Return>", lambda _: self.copy_first())
        entry.bind("<Escape>", lambda _: self.set_query(""))
        # A placeholder (ttk has none): shown while the box is empty.
        self.placeholder = ttk.Label(
            search,
            text="Search your clipboard   ·   Enter copies the top result",
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
        self.count_label = ttk.Label(chips, style="Hint.TLabel")
        self.count_label.pack(side="right", padx=(0, 8))
        frame = self.app.frame
        self.banner = ttk.Frame(frame)
        self.banner.pack(fill="x")
        self.list_frame = ttk.Frame(frame)
        self.list_frame.pack(fill="x")
        self.card: tk.Frame | None = None
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
        self.rows = []
        self.card = None
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
        shown = {item.image_file for item in items if item.kind == "image"}
        self._thumbs = {name: photo for name, photo in self._thumbs.items() if name in shown}
        for item in items:
            self._add_row(item)
        self._finish(len(items))

    def _finish(self, shown: int) -> None:
        """The empty-list hint and the footer, which depend on how many rows there are."""
        if not shown:
            searching = bool(self.query.get().strip()) or self.filter != "all"
            text = (
                "No matches."
                if searching
                else "Copy something and it will appear here. Images stay on this computer."
            )
            ttk.Label(
                self.list_frame, text=text, style="Hint.TLabel", wraplength=self.app.wraplength
            ).pack(anchor="w", pady=20)
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

    def load_more(self) -> None:
        """Add the next page below the rows already drawn instead of redrawing them all."""
        self.limit += PAGE_SIZE
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
        meta = f"{relative_time(item.created_at, self._clock())} · {SOURCES.get(item.source, item.source)}"
        hint = tk.Label(
            info,
            text=meta,
            background=surface,
            foreground=colors["muted"],
            font=fonts["small"],
            cursor="hand2",
        )
        hint.pack(anchor="w")
        painted += [actions, info, label, hint]
        clickable += [info, label, hint]
        for widget in clickable:
            widget.bind("<Button-1>", lambda _: self.copy(item.id))
        star = self._action(actions, "★" if item.favorite else "☆", colors["star"], "icon")
        star.bind("<Button-1>", lambda _: self.toggle_favorite(item.id))
        copy = self._action(actions, "Copy", colors["accent"])
        copy.bind("<Button-1>", lambda _: self.copy(item.id))
        delete = self._action(actions, "Delete", colors["muted"], hover=colors["danger"])
        delete.bind("<Button-1>", lambda _: self.delete(item.id))
        painted += [star, copy, delete]

        def paint(color: str) -> None:
            for widget in painted:
                widget.configure(background=color)  # type: ignore[call-arg]

        def left(_: object) -> None:
            # Moving onto a child also "leaves" the row: only repaint once really out.
            under = self.app.root.winfo_containing(*self.app.root.winfo_pointerxy())
            if under is None or not str(under).startswith(str(row)):
                paint(surface)

        row.bind("<Enter>", lambda _: paint(colors["hover"]))
        row.bind("<Leave>", left)
        self.rows.append(Row(item, row, star))

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
        self._signature = self._current_signature()  # Our own change needs no redraw.
        if row is None:
            return
        row.item = dataclasses.replace(row.item, favorite=not item.favorite)
        if self.filter == "favorites" and not row.item.favorite:
            self._remove_row(row)
        else:
            row.star.configure(text="★" if row.item.favorite else "☆")

    def copy_first(self) -> None:
        if self.pending_search is not None:
            self.run_pending_search()
        if self.rows:
            self.copy(self.rows[0].item.id)

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
        self._signature = self._current_signature()
        row = self._row(item_id)
        if row is not None:
            self._remove_row(row)

    def ask_clear(self, linked: bool) -> tuple[bool, bool] | None:
        """(everywhere, keep favorites), or None when the user cancels."""
        return ClearDialog(self.app.root, linked=linked).show()

    def clear(self) -> None:
        choice = self.ask_clear(self.app.service.clipboard_plus_linked())
        if choice is None:
            return
        everywhere, keep_favorites = choice
        count = self.app.service.clear_clipboard(
            self.store, everywhere=everywhere, keep_favorites=keep_favorites
        )
        self.reload()
        where = " here and on your Clipboard+ account" if everywhere else ""
        self.app.status.set(f"Cleared {count} item{'s' if count != 1 else ''}{where}.")

    def resume(self) -> None:
        prefs = hotkeys.Preferences(self.app.service.paths)
        prefs.save(clipboard=dataclasses.replace(prefs.clipboard(), paused_until=0.0))
        self.reload()


class ClearDialog:
    """Asks what "Clear history" should clear: where, and whether favorites stay."""

    def __init__(self, parent: tk.Misc, *, linked: bool) -> None:
        self.result: tuple[bool, bool] | None = None
        window = self.window = tk.Toplevel(parent)
        window.title("Clear clipboard history?")
        window.resizable(False, False)
        window.transient(parent)  # type: ignore[call-overload]
        window.configure(background=ttk.Style(window).lookup("TFrame", "background"))
        # Linked: clearing here clears the account too, unless the user says otherwise.
        self.scope = tk.StringVar(master=window, value="everywhere" if linked else "device")
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
        ttk.Button(row, text="Cancel", command=self.cancel).pack(side="right", padx=(0, 8))
        window.protocol("WM_DELETE_WINDOW", self.cancel)
        window.bind("<Escape>", lambda _: self.cancel())

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
                "Click Get a key: on your Clipboard+ Account page, choose "
                "Create a key for Clipboard+ Desktop, then paste the key here.",
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

    def _attempt(self, work: Callable[[], None], message: str, success: str) -> None:
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
        )

    def connect_key(self) -> None:
        key, service = self.key.get(), self.app.service
        self._attempt(
            lambda: service.connect_clipboard_plus(key),
            "Checking your Clipboard+ key…",
            "Connected to Clipboard+.",
        )

    def sync_now(self) -> None:
        self.app.service.sync_clipboard_now()
        self.app.status.set("Syncing…")

    def disconnect(self) -> None:
        keep = messagebox.askyesnocancel(
            "Disconnect Clipboard+?",
            "Keep your clipboard history on this computer?\n\n"
            "Yes: keep it here.  No: delete it from this computer.\n"
            "Your Clipboard+ account keeps its own copy either way.",
            parent=self.app.root,
        )
        if keep is None:
            return
        service = self.app.service
        self._attempt(
            lambda: service.disconnect_clipboard_plus(keep_history=bool(keep)),
            "Disconnecting…",
            "Disconnected from Clipboard+.",
        )
