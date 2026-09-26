# Whisper Dictation & Clipboard+ — design

Status: approved in conversation (sections 1–2 by the owner; sections 3–8 written
from the owner's decisions and the Clipboard+ backend contract, implemented on
their instruction to "implement in full").

## 1. Purpose

One desktop app, three ways to use it, identical on Linux, macOS and Windows:

- **Dictation** — press a shortcut, speak, press again, paste (exists today).
- **Clipboard history** — a local clipboard manager: text, links and images,
  searchable, with favorites and clear-history.
- **Both** — the default for people who want the whole thing.

The user picks during setup and can change it later in Settings. Cloud sync with a
Clipboard+ account is optional in every mode, so the desktop app and the Clipboard+
browser extension are two views of one history without duplicates.

Success means: a user can install, choose a mode, and reach a working state without
the terminal; nothing is captured until they opt in; a copy is never stored twice
(locally, in the cloud, or between the extension and the app); images stay on the
device; every platform offers the same features and wording.

## 2. Decisions

| Topic | Decision |
| --- | --- |
| Name | "Whisper Dictation & Clipboard+" as the *display* name. Bundle id, folders, config paths, WM class stay (upgrades keep working). |
| Modes | `dictation`, `clipboard`, `both`; chosen in setup, changeable in Settings. Existing installs keep dictation only and are offered clipboard later. |
| Shortcut | Dictation default Super+Shift+D (Windows: Win+Shift+D, macOS: ⌃⌥⇧D). Verified against Chrome's published list (Alt/Ctrl/Ctrl+Shift/⌘/⌘⇧ + D are taken; no Win/Super or ⌃⌥ combos exist). Google Docs' own in-page combos are not covered by that list. |
| Architecture | A separate headless clipboard service owns capture, the database, retention and sync. Tray, window and dictation only use the store API. |
| Storage | SQLite (stdlib, WAL) plus PNG image files. Images never leave the device. |
| Duplicates | Local: identical content moves to the top. Dictation: the engine records the transcript itself and the watcher ignores the resulting clipboard write. Cross-client: server-side window (see §7). |
| Sync | Two-way: push local text/links, pull the account's items and deletions, mirror favorites, deletes and clear. |
| Account | In-app "Create account / Sign in" (email + password) that creates a scoped API key and then discards the password and session token; or paste a key (Google-only accounts). |

## 3. What users see

Same on every platform.

**Menu** (top bar / menu bar / tray): status line; dictation items (when enabled);
*Clipboard History…*; *Pause Clipboard Capture* (with *Pause for 1 hour*);
Shortcut; Open at Login; Clipboard+ account; Settings; Quit.

**Window** tabs (only the enabled features appear):

- **Clipboard** — search; filters All / Favorites / Images / Text; newest-first list
  with preview or thumbnail, relative time, source label (Desktop, Dictation, Cloud)
  and ★ / copy / delete. Clicking a row copies it. *Clear history* offers *This
  device* or, when linked, *Everywhere*, and *Keep favorites* (default on). Rows load
  50 at a time.
- **Dictation** — the "Press <shortcut> to dictate" page.
- **Settings** — Features (Dictation / Clipboard / Both), dictation options,
  Clipboard options (capture on/off, keep N items / N days, images on/off), Account.

**Setup** starts with *What do you want to use?* (Dictation, Clipboard history, Both).
Only the chosen features' steps follow: dictation setup (language, microphone, AI),
the clipboard opt-in ("Saves what you copy so you can search and re-copy it. Stays on
this computer. Skips anything a password manager marks secret. Pause any time."), then
the optional account step, then a short "how it works".

## 4. Structure

New modules under `lib/` (all listed in `setup-desktop.py` `MODULES`, enforced by an
existing test):

| Module | Responsibility |
| --- | --- |
| `clipstore.py` | SQLite store: items, favorites, search, retention, image files, sync bookkeeping. The only code that touches the database. |
| `clipwatch.py` | `Clip` value type, `Watcher` interface, `create_watcher()`, concealed-content detection shared logic. |
| `clipwatch_linux.py`, `clipwatch_macos.py`, `clipwatch_windows.py` | One watcher per OS (see §5). |
| `clipservice.py` | Process entry point: lock, capture loop, retention, sync scheduling, status file, quit request. |
| `clipsync.py` | Cloud protocol: push, pull, tombstones, favorites, deletes, clear. Pure logic over a `Store` and a `Cloud` client, so it is testable without a network. |
| `clipui.py` | Tk pages for the Clipboard tab and account flow, used by `app.py`. |
| `clipboardplus.py` | (extended) API client: register / login / create key / verify / requests; key storage. |

Process model: the tray or menu bar starts `clipservice.py` when clipboard is enabled
and restarts it if it exits; a `clip-quit` runtime file stops it. The window and tray
talk to it through the database, `clip-status.json` (capturing / paused / sync state)
and small runtime signal files (`clip-sync-now`). Preferences live in the existing
`menubar.json` (`features`, `clipboard`).

## 5. Capture

`Watcher.next_change(timeout) -> Clip | None` returns text, an image (PNG bytes) or
nothing, plus `concealed`.

- **Linux** — `wl-paste --watch` where the compositor supports data-control (wlroots,
  KDE); otherwise X11 `XFIXES` selection-owner events over XWayland/X11 (verified on
  GNOME Wayland: events arrive while unfocused and content is readable). Content is
  read with `XConvertSelection` (UTF8_STRING, `image/png`). Concealed = target
  `x-kde-passwordManagerHint`.
- **macOS** — `NSPasteboard.changeCount` polled every 0.5 s; text, `public.png` /
  `public.tiff` (converted to PNG). Concealed = `org.nspasteboard.ConcealedType`,
  `TransientType`, `AutoGeneratedType`.
- **Windows** — `AddClipboardFormatListener` and `WM_CLIPBOARDUPDATE` on a message
  window; `CF_UNICODETEXT`, `PNG` / `CF_DIB` (converted to PNG). Concealed =
  `ExcludeClipboardContentFromMonitorProcessing`, or `CanIncludeInClipboardHistory` = 0.

Limits: text ≤ 1 MB locally (cloud ≤ 50,000 bytes); images ≤ 10 MB each and 500 MB in
total (oldest non-favorite images pruned first); thumbnails 256 px. Files and other
formats are ignored.

## 6. Data model

`items(id, kind text|url|image, text, image_file, thumb_file, width, height, bytes,
sha, created_at, updated_at, favorite, label, source desktop|dictation|cloud,
cloud_id, cloud_key, cloud_favorite, dirty, sync_skip)`;
`tombstones(cloud_id, cloud_key, deleted_at)`; `meta(key, value)` (sync cursor,
pending-clear flag). Times are epoch seconds. `sha` is SHA-256 of the content;
re-copying an existing hash updates `created_at` and `updated_at` (moves to top).
A single `http(s)` URL is `url`, anything else text. Retention: keep the newest N
(default 1000) and at most D days (default 30); favorites are exempt.

## 7. Cloud

**Account.** In-app: email + password → `POST /api/auth/register` or `/login` (returns
a bearer token) → `POST /api/keys` with scopes `clipboard:read` and `clipboard:write`
→ store only the key (`clipboard-plus-key`, owner-only); the password and token are
never stored. Alternative: paste a key (`cp_live_…`); it is verified for both scopes
(read via `GET /api/clipboard?limit=1`, write via a probe `POST` that must answer 400).

**Matching.** A cloud item and a local item are the same when their sync keys are equal:
`type|createdAt(ms)|first 200 characters` (the server's own `makeSyncKey`). `/sync`
returns no ids, so ids are learned from the next pull.

**Round** (every 60 s while linked, 5 s after a local change, or on *Sync now*):

1. *Push* dirty text/url items (≤ 100 per request) to `POST /api/clipboard/sync`
   with `ts` in ms and `isFavorite`. It is an idempotent upsert, so retries are safe.
2. *Pull* `GET /api/clipboard/pull?since=<last success − 120 s>`; the overlap makes
   replays harmless. New cloud items are inserted with source `cloud` (never written
   to the OS clipboard, so nothing is re-captured). Matching items get `cloud_id`.
   Favorite/label follow the newer `updated_at`. `deletedItems` tombstones delete the
   local item with the same key.
3. *Deletes*: local deletions of linked items → `DELETE /api/clipboard/:id` (404 is
   success); tombstone row removed.
4. *Favorites*: the server route toggles, so it is called only when the local
   favorite differs from the last known cloud state.
5. *Clear everywhere*: `DELETE /api/clipboard` (keeps favorites, like the app's
   *Keep favorites* default); including favorites also calls `DELETE /api/clipboard/bulk`.

Errors: network failure backs off (30 s → 10 min, jittered); 401/403 stops syncing and
asks the user to reconnect; a 400/413 marks that item `sync_skip` so it is not retried.

**Server changes** (repo `clipboardplus`): reject an incoming item whose exact
content already exists for that user within ±10 minutes **from a different source**
(POST and `/sync`). The extension and the app capture the same copy with different
timestamps (the extension reads the clipboard when Chrome regains focus), so the
existing per-timestamp key cannot see them as equal. Same-source re-copies are still
allowed. The extension's pull-merge must also treat a pulled item with identical
content within ±10 minutes as already present.

## 8. Privacy and safety

Nothing is captured until the user enables Clipboard. Concealed content is skipped;
capture can be paused (indefinitely or for an hour); images are local only; the
Clipboard+ key is owner-only and only ever sent over HTTPS with redirects disabled;
the account password is used for one request and discarded. Uninstall keeps history
and settings (consistent with dictation data) but the settings page offers *Delete
all clipboard data*.

## 9. Rename and shortcut

Display strings (window title, tray title, notifications, desktop entry, macOS
`CFBundleDisplayName`, Windows Start Menu shortcut, README, docs) become "Whisper
Dictation & Clipboard+". The Windows shortcut file is renamed with the old one removed
on install. Defaults: Super+Shift+D; macOS ⌃⌥⇧D; Ctrl+Alt+D remains a preset.

## 10. Phases and verification

1. **Foundation** — rename, per-platform default shortcut, `features` preference,
   setup mode choice, mode-aware readiness/menus.
2. **Local clipboard manager** — store, watchers, service, tray/menu integration,
   dictation writes transcripts to the store.
3. **Window** — Clipboard tab, settings, opt-in and pause.
4. **Cloud** — account flow, client, sync engine, backend dedupe change, extension merge.
5. **Hardening** — docs, CI, install/uninstall coverage, review.

Each phase ships with unit tests. Store, sync and UI are tested on Linux (Xvfb for
Tk). The Linux watcher is exercised against a real X server. macOS and Windows
watchers are written to the same tested interface but cannot be run in this
environment; that is stated in the docs and the final report.

## 11. Out of scope

Snippets / text expander, OCR, mobile, syncing images, a separate history shortcut
(opened from the menu and window instead), end-to-end encryption.
