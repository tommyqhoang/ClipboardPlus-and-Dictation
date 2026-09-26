# Whisper Dictation & Clipboard+ Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax. Every task is test-first: write the failing test, watch it fail, implement, watch it pass, commit.

**Goal:** Turn the dictation app into "Whisper Dictation & Clipboard+": a cross-platform clipboard manager (text, links, images) with optional two-way Clipboard+ sync, selectable as Dictation, Clipboard, or Both.

**Architecture:** A headless `clipservice` process owns capture (per-OS watchers), the SQLite store, retention and cloud sync. The tray/menu bar starts it; the Tk window and the dictation engine use the shared `clipstore` API. Cloud logic (`clipsync`) is pure over a `Store` and a `Cloud` client so it is testable offline.

**Tech Stack:** Python 3.10+ stdlib (`sqlite3`, `urllib`, `ctypes`), Tk, pystray/Pillow/python-xlib (Linux/Windows venv), PyObjC (macOS venv), Node/Jest (Clipboard+ backend and extension).

**Spec:** `docs/superpowers/specs/2026-09-26-clipboard-and-dictation-design.md`

## Global Constraints

- Python floor 3.10; new modules pass `mypy --strict`, `ruff check`, `ruff format`.
- Every new `lib/*.py` and `lib/*.png` must be listed in `setup-desktop.py` (`MODULES` / `ICONS`) and in both uninstallers (test `test_installer_lists_every_module_and_icon_in_lib` enforces the install side).
- Display name is exactly `Whisper Dictation & Clipboard+`; internal ids, folder names, bundle id `org.whisperdictation.desktop`, WM class `WhisperDictation` do not change.
- Shortcut defaults: Super+Shift+D (Windows shows `Win+Shift+D`), macOS `⌃⌥⇧D`; `Ctrl+Alt+D` stays a preset.
- Cloud limits: text ≤ 50,000 bytes, ≤ 100 items per `/sync` request, key scopes `clipboard:read` + `clipboard:write`, HTTPS only, redirects never followed.
- Local limits: text ≤ 1 MB, image ≤ 10 MB each and 500 MB total, thumbnails 256 px, defaults keep 1000 items / 30 days (favorites exempt).
- Nothing is captured until the user enables Clipboard. Concealed content is never stored. Images are never uploaded.
- macOS and Windows code cannot be run here; it must share the tested `Watcher` interface and be reported as unrun.

## Review Focus

- Clipboard holds a password-manager secret → skipped (test per platform's concealed marker).
- Copy is 2 GB / binary / huge image → skipped, memory bounded, service keeps running.
- Same copy captured by app and extension, or by two devices → one item (server window + local link + extension merge).
- Sync interrupted mid-push, offline for days, key revoked → retries are idempotent, backoff, "reconnect" state, no data loss.
- Two windows/processes writing the store (service, window, dictation) → no `database is locked` failure surfaces to the user.

---

### Task 1: Feature modes and preferences

**Files:** Modify `lib/hotkeys.py` (`Preferences`), `lib/app_service.py` (`ready`, `completed`), `tests/test_hotkeys.py`, `tests/test_app.py`.

**Interfaces:**
- Produces: `hotkeys.Features(dictation: bool, clipboard: bool)`; `Preferences.features() -> Features` (default `Features(True, False)` when unset); `Preferences.save(features=Features)`; `Preferences.clipboard() -> ClipboardSettings` and `save(clipboard=ClipboardSettings)` where `ClipboardSettings(keep_items=1000, keep_days=30, images=True, paused_until=0.0)`; `Service.ready()` returns `True` when dictation is disabled.

- [ ] Write tests: defaults `Features(True, False)`; save/read round-trip; unknown/corrupt JSON falls back to defaults; `keep_items` clamped to 50–10000, `keep_days` to 1–3650; `Service.ready()` is `True` with no model when dictation is off, unchanged when on.
- [ ] Run → FAIL. Implement dataclasses and clamping in `hotkeys.py`; make `Service.ready()` consult `Preferences.features()`.
- [ ] Run → PASS; `ruff`, `mypy`. Commit `feat: feature modes and clipboard preferences`.

### Task 2: Rename and per-platform default shortcut

**Files:** Modify `lib/hotkeys.py` (`DEFAULT`, `PRESETS`), `lib/app.py`, `lib/tray.py`, `lib/menubar.py`, `setup-desktop.py`, `install.sh`, `README.md`, `docs/DESKTOPS.md`, tests.

**Interfaces:** Produces `hotkeys.APP_NAME = "Whisper Dictation & Clipboard+"`; `hotkeys.default_shortcut(platform: str) -> Shortcut` (`macos` → `Shortcut(("ctrl","alt","shift"),"D")`, otherwise `Shortcut(("shift","cmd"),"D")`); `DEFAULT = default_shortcut(desktop.platform_name())`.

- [ ] Tests: `default_shortcut("macos").label("macos") == "⌃⌥⇧D"`, linux `"Super+Shift+D"`, windows `"Win+Shift+D"`; `PRESETS` unique and contains `Ctrl+Alt+D`; every user-visible title/notification uses `APP_NAME` (grep test over `lib/` for the old bare title strings `"Whisper Dictation"` in window titles/tray title).
- [ ] Implement; set window title, `pystray.Icon` title, desktop entry `Name=`, `CFBundleDisplayName`, Windows shortcut name (remove old `Whisper Dictation.lnk` on install; legacy handled by the existing shortcut script), notification titles.
- [ ] Run suite; commit `feat: rename to Whisper Dictation & Clipboard+ and safe default shortcuts`.

### Task 3: Clipboard store

**Files:** Create `lib/clipstore.py`, `tests/test_clipstore.py`; modify `setup-desktop.py`, `uninstall.sh`.

**Interfaces (Produces):**
```python
@dataclass(frozen=True)
class Item: id: int; kind: str; text: str; image_file: str; thumb_file: str; width: int; height: int; bytes: int; created_at: float; updated_at: float; favorite: bool; label: str; source: str; cloud_id: str; cloud_key: str
class Store:
    def __init__(self, directory: Path) -> None: ...
    def add_text(self, text: str, source: str = "desktop", now: float | None = None) -> Item | None
    def add_image(self, png: bytes, width: int, height: int, source: str = "desktop", now: float | None = None) -> Item | None
    def list(self, *, query: str = "", kind: str = "", favorites: bool = False, limit: int = 50, before: float | None = None) -> list[Item]
    def get(self, item_id: int) -> Item | None
    def set_favorite(self, item_id: int, value: bool) -> None
    def delete(self, item_id: int) -> None
    def clear(self, *, keep_favorites: bool = True) -> int
    def prune(self, keep_items: int, keep_days: int, image_cache_mb: int = 500) -> int
    def dirty(self, limit: int = 100) -> list[Item]
    def close(self) -> None
```
Also sync helpers defined in Task 12. `add_*` return `None` when the content is empty or over the local limit; identical content (same `sha`) updates `created_at`/`updated_at` and returns the existing item; a single `http(s)` URL is `kind="url"`.

- [ ] Tests: add/list order; identical text moves to top and count stays 1; whitespace-only and > 1 MB text rejected; URL vs text kind; image add writes `images/<sha>.png` + `thumbs/<sha>.png` (thumb skipped without Pillow) and identical image dedupes; > 10 MB image rejected; search is case-insensitive substring on text/label; kind and favorites filters; paging with `before`; delete removes image files; `clear(keep_favorites=True)` keeps starred; `prune` honors count, age, favorites exempt and the 500 MB image cap (oldest non-favorite images first); two `Store` instances on one directory write concurrently (threads) without `database is locked` (WAL + `busy_timeout=5000`); schema version stored in `meta` and a v0 database migrates.
- [ ] Implement (WAL, `PRAGMA user_version`, private directory `0700`, files `0600`, all SQL parameterized; thumbnails via optional Pillow).
- [ ] Add `clipstore.py` to `MODULES` and uninstall lists. Run → PASS. Commit `feat: local clipboard store`.

### Task 4: Watcher interface and Linux watcher

**Files:** Create `lib/clipwatch.py`, `lib/clipwatch_linux.py`, `tests/test_clipwatch.py`; modify `setup-desktop.py`, `uninstall.sh`.

**Interfaces (Produces):**
```python
@dataclass(frozen=True)
class Clip: text: str = ""; image_png: bytes = b""; concealed: bool = False
class Watcher(Protocol):
    def next_change(self, timeout: float) -> Clip | None   # blocks up to timeout; None = no change
    def close(self) -> None
def create_watcher(platform: str | None = None) -> Watcher
def limit_clip(clip: Clip, max_text: int = 1_000_000, max_image: int = 10_000_000) -> Clip | None
```
`clipwatch_linux.LinuxWatcher` uses `wl-paste --watch` when `wl-paste --watch true` starts and stays up, else X11 XFIXES (python-xlib) with `XConvertSelection` for `UTF8_STRING` / `image/png`, INCR-safe with a 2 s read timeout, and `x-kde-passwordManagerHint` → `concealed=True`.

- [ ] Tests (pure): `limit_clip` drops oversize text/image; a `FakeWatcher` conforms to `Watcher`; `parse_targets` marks concealed when the password-manager target is present; `choose_target` prefers image/png over text when both exist and text otherwise.
- [ ] Integration test (skipped when no X display): start `Xvfb`, run a tiny `xclip`-free owner using python-xlib to own `CLIPBOARD`, assert `LinuxWatcher.next_change(2)` returns the text; then an unrelated selection does not fire.
- [ ] Implement; add modules to `MODULES`/uninstall. Commit `feat: clipboard watcher interface and Linux watcher`.

### Task 5: macOS and Windows watchers

**Files:** Create `lib/clipwatch_macos.py`, `lib/clipwatch_windows.py`; modify `lib/clipwatch.py` (`create_watcher`), tests.

- [ ] Tests (logic only, platform APIs faked): macOS `MacWatcher` with a fake pasteboard (`changeCount`, `types`, `dataForType_`) yields text, converts TIFF→PNG through an injected converter, skips when a concealed type is present, returns `None` when `changeCount` is unchanged; Windows `WindowsWatcher` with a fake `user32` yields text for `CF_UNICODETEXT`, converts DIB via injected converter, skips when `ExcludeClipboardContentFromMonitorProcessing` is registered, and retries `OpenClipboard` on `ERROR_ACCESS_DENIED` up to 5 times.
- [ ] Implement using PyObjC (`NSPasteboard.generalPasteboard()`, polling 0.5 s) and `ctypes` (`AddClipboardFormatListener`, `GetClipboardSequenceNumber`, `GetPriorityClipboardFormat`); `create_watcher` imports the platform module lazily.
- [ ] `python -m py_compile` all three; `mypy --strict` with the existing PyObjC overrides. Commit `feat: macOS and Windows clipboard watchers (interface-tested)`.

### Task 6: Clipboard service process

**Files:** Create `lib/clipservice.py`, `tests/test_clipservice.py`; modify `setup-desktop.py`, `uninstall.sh`.

**Interfaces (Produces):**
```python
class Service:
    def __init__(self, store: Store, watcher: Watcher, prefs: Callable[[], ClipboardSettings], paths: d.Paths, clock: Callable[[], float] = time.time) -> None
    def step(self, timeout: float) -> None   # one loop iteration: capture, prune (every 5 min), write status
    def paused(self) -> bool
def main() -> int   # lock `clipservice.lock`, run until `clip-quit`
```
Status file `clip-status.json`: `{"state": "capturing|paused|error", "count": int, "updated": float}`.

- [ ] Tests: text clip stored with source `desktop`; concealed clip not stored; paused (`paused_until` in the future, or `-1` = until resumed) not stored; disabled feature stores nothing; images skipped when `images=False`; clip equal to the dictation marker (`ignore` file with hash and time < 10 s) skipped; watcher exception → status `error` and loop continues; prune runs on schedule with the injected clock; `main()` refuses a second instance and exits on `clip-quit`.
- [ ] Implement; the ignore marker helper `clipservice.mark_own_write(paths, text)` is used by the dictation engine. Commit `feat: clipboard service`.

### Task 7: Tray and menu bar integration

**Files:** Modify `lib/tray.py`, `lib/menubar.py`, `tests/test_tray.py`.

- [ ] Tests: with clipboard enabled the tray starts `clipservice.py` once (Popen patched) and restarts it after it exits; disabled → never started; *Pause Clipboard Capture* toggles `paused_until` (`-1` and now+3600); *Clipboard History…* opens the window on `--clipboard`; dictation items hidden when dictation is disabled and the dictation shortcut is not registered; menu rebuilt only when mode/pause state changes (no per-tick rebuild); status line reflects `clip-status.json`.
- [ ] Implement in both apps identically (same labels); `app.py` gains the `--clipboard` page flag (Task 9). Commit `feat: tray integration for the clipboard service`.

### Task 8: Dictation records transcripts in the store

**Files:** Modify `lib/dictation.py` (`finish`, remove `share_transcript`), `lib/clipboardplus.py`, tests.

- [ ] Tests: after a transcript, the store has one item with source `dictation` (even when clipboard capture is off); the ignore marker prevents a second item from the watcher; nothing is uploaded directly any more (the old `share_transcript` tests are replaced by sync-path tests in Task 13); a store failure never fails the dictation (logged notice only).
- [ ] Implement using `clipstore.Store` (lazy import, tolerate `ImportError`). Commit `feat: dictation transcripts join clipboard history`.

### Task 9: Window: tabs, Clipboard page, opt-in

**Files:** Create `lib/clipui.py`; modify `lib/app.py`, `lib/app_service.py`, `tests/test_app.py`, `tests/test_clipui.py`.

**Interfaces:** `clipui.ClipboardPage(app: App, store: Store)` with `render()`, `refresh()`, `copy(item_id)`, `toggle_favorite(item_id)`, `delete(item_id)`, `clear()`; `App.tab(name: str)` switches between `clipboard`, `dictation`, `settings` for enabled features.

- [ ] Tests (Xvfb): tabs shown only for enabled features; list renders text/URL rows and an image thumbnail; typing in search filters after debounce; filter chips; favorite toggles the star and the store; copy puts text on the Tk clipboard and shows "Copied"; delete asks nothing for one item and removes it; *Clear history* asks scope and keep-favorites, and clears; empty state text for no history / no results / capture paused; 50-row paging (`Load more`); opt-in step appears in setup only when Clipboard is chosen and *Not now* leaves the feature off.
- [ ] Implement with lazy rows (canvas + recycled frames), thumbnails via `tk.PhotoImage(file=...)`, a 300 ms search debounce, no per-poll full re-render (compare `(count, newest_id, query, filter)`). Commit `feat: clipboard history window and opt-in`.

### Task 10: Setup mode choice and Settings

**Files:** Modify `lib/app.py` (`welcome`, `begin_setup`, `settings`), `lib/app_service.py`, tests.

- [ ] Tests: welcome shows three choices (Dictation, Clipboard history, Both); choosing Clipboard only skips model/microphone setup and never requires `Config.check`; Both runs dictation setup then the clipboard opt-in; Settings changes features live (tray picks it up on the next tick via the preferences stamp); *Delete all clipboard data* asks for confirmation and removes the database and images.
- [ ] Implement. Commit `feat: choose dictation, clipboard or both`.

### Task 11: Backend duplicate window (clipboardplus repo)

**Files (repo `~/Documents/GitHub/clipboardplus`):** Modify `backend/src/routes/clipboard.js` (POST `/` and `/sync`), add tests under the repo's Jest setup; modify `background.js` pull-merge.

- [ ] Read the repo's `AGENTS.md`, the existing route tests and `makeLocalSyncKey`/merge code first.
- [ ] Tests (Jest): item identical to one saved ±10 min earlier from a different source is skipped (POST and `/sync`); same source re-copy still allowed; different content, or outside the window, still inserted; null source treated as different. Extension: a pulled item with identical content within ±10 min of a local item is merged, not added.
- [ ] Implement; run the repo's unit suite. Commit in that repo only; do not push or deploy.

### Task 12: Cloud client and account flow

**Files:** Modify `lib/clipboardplus.py`; create `tests/test_clipboardplus_account.py`.

**Interfaces (Produces):**
```python
class Cloud:
    def __init__(self, key: str, api: str = API) -> None
    def pull(self, since: float | None) -> Pull            # Pull(items: list[CloudItem], deleted: list[Tombstone])
    def push(self, items: list[Item]) -> None
    def toggle_favorite(self, cloud_id: str) -> None
    def delete(self, cloud_id: str) -> None                 # 404 = success
    def clear(self, *, favorites: bool) -> None
def register(email: str, password: str, api: str = API) -> str      # returns bearer token
def login(email: str, password: str, api: str = API) -> str
def create_key(token: str, name: str, api: str = API) -> str        # scopes clipboard:read+write; returns key
def verify(key: str, api: str = API) -> str                         # ok | invalid | read-only | write-only | offline | error
def sync_key(kind: str, created_ms: int, primary: str) -> str       # type|ms|first 200 chars
class AuthError(Exception); class SyncError(Exception)
```

- [ ] Tests (fake opener, plus a local HTTP server for redirects): register/login map 409 → "account exists, sign in", 401 → "wrong email or password", `GOOGLE_ACCOUNT_ONLY` → "use a key from the web"; `create_key` sends scopes and returns the token; the password/token never appear in exceptions or logs; `verify` distinguishes read-only and write-only; `sync_key` equals the server's `makeSyncKey` for fixtures shared with the backend test; redirects never followed; malformed JSON → `SyncError`.
- [ ] Implement. Commit `feat: Clipboard+ account and API client`.

### Task 13: Sync engine

**Files:** Create `lib/clipsync.py`, `tests/test_clipsync.py`; extend `lib/clipstore.py` with sync methods.

**Interfaces:** Store gains `mark_pushed(item_id, cloud_key)`, `link(item_id, cloud_id, cloud_favorite)`, `find_by_key(cloud_key)`, `add_cloud(item: CloudItem)`, `delete_local_by_key(cloud_key)`, `tombstones() -> list[Tombstone]`, `clear_tombstone(cloud_key)`, `set_skip(item_id)`, `meta_get/meta_set`. `clipsync.Engine(store: Store, cloud: Cloud, clock)` with `run_once() -> Report(pushed, pulled, deleted, errors)`.

- [ ] Tests (fake `Cloud`): push sends only dirty text/url (never images), ≤ 100 per request, is idempotent when repeated; pull inserts unknown items with source `cloud` and never touches the OS clipboard; pulled item matching local key links instead of duplicating; pulled item matching content within ±10 minutes from another source links instead of duplicating; tombstone deletes the local item; local delete of a linked item calls `delete` and drops the tombstone; 404 on delete is success; favorite change calls toggle only when it differs from the last known cloud state; newer `updated_at` wins for favorite/label; *clear everywhere* calls `clear` with the right flag; `since` = last success − 120 s; 401/403 → engine state `auth` and no further calls; network error → backoff schedule; 400/413 marks `sync_skip`; replaying the same pull twice changes nothing.
- [ ] Implement; service (Task 6) runs `Engine.run_once()` every 60 s, 5 s after a local change, or on `clip-sync-now`. Commit `feat: two-way Clipboard+ sync`.

### Task 14: Account UI

**Files:** Modify `lib/clipui.py`, `lib/app.py`, `lib/app_service.py`, tests.

- [ ] Tests (Xvfb, fake client): account card states *Not connected* / *Connected as <email>* / *Reconnect needed*; *Create account* and *Sign in* run off the UI thread, show inline errors from Task 12 messages, clear the password field, create the key and store it; *Use an API key instead* verifies read and write; *Sync now* writes `clip-sync-now`; *Disconnect* asks whether to keep local history and removes the key; the old dictation-only Clipboard+ card and `share_transcript` upload are gone.
- [ ] Implement. Commit `feat: account flow in the desktop app`.

### Task 15: Hardening, docs, install/uninstall, CI

**Files:** Modify `README.md`, `docs/DESKTOPS.md`, `tests/VALIDATION.md`, `setup-desktop.py`, `uninstall.sh`, `install.sh`, `.github/workflows/quality.yml`, tests.

- [ ] Add Pillow to the macOS venv requirements; update the install probes (`clipstore`, `python-xlib` on Linux); uninstall removes new modules, service lock/status files, and keeps the clipboard database unless the user chose *Delete all clipboard data*.
- [ ] Docs: features and modes, privacy (what is stored, where, concealed skipping, pause, images local), account flow, shortcut table verified against Chrome, verification limits (macOS/Windows watchers unrun, Google Docs combos).
- [ ] Full suite under Xvfb and plain; `bash tests/check.sh`; shellcheck/shfmt in Docker; end-to-end on this machine: install to a temp prefix, run the service against the real X server, copy text and an image with `wl-copy`, see both in the store; container installs on the five distros. Fix anything found. Commit `docs: clipboard manager, privacy and verification notes`.

---

## Self-review

- **Spec coverage:** modes (T1, T10), rename and shortcut (T2), store (T3), watchers (T4–T5), service (T6), tray/menu (T7), dictation integration (T8), window (T9), setup (T10), server window and extension merge (T11), account and client (T12), sync (T13), account UI (T14), docs/CI/uninstall (T15). Out-of-scope items are not planned.
- **Placeholders:** none; interfaces are given as signatures, tests as concrete assertions.
- **Type consistency:** `Item`, `Store`, `Clip`, `Watcher`, `Cloud`, `Engine`, `ClipboardSettings`, `Features` names are used identically across tasks.
