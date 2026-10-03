# Changelog

All notable changes to Clipboard+ / Dictation. Reconstructed from the git
history; versions follow the `v*` release tags.

## [Unreleased]

## [1.6.7] - 2026-10-03

### Changed
- macOS now labels the app process and Dock/menu identity as Clipboard+.
- Settings and the main window use clearer headings, more readable spacing, and a
  calmer enterprise-oriented visual style.

## [1.6.6] - 2026-10-03

## [1.6.5] - 2026-10-02

### Fixed
- Clipboard search and page loads no longer wait for the half-second window poll: results
  are collected within about 15 ms, so typing a search and opening the history feel instant.
- The "Searching clipboard…" label only appears for searches that take over a quarter of a
  second, so fast searches no longer make the list jump down and back up on every keystroke.
- The window no longer asks the system keychain (which can take seconds behind a locked
  Secret Service) on every refresh to learn whether Clipboard+ is linked; the answer is
  reused for 30 seconds and forgotten when a key is saved or removed.
- Opening the history from its shortcut shows the finished window once, raised and focused,
  instead of mapping it first and moving it a moment later.

## [1.6.4] - 2026-10-02

### Fixed
- macOS: the recording bar appears again. It was created after the app object, which
  stopped Tk from starting; it now starts first, and a failure is logged as a warning.
- macOS: the menu bar icon is found wherever the app bundle keeps its images, with a
  system-symbol fallback and a warning in the log if neither is available.
- macOS: the packaged app now includes the frameworks that check microphone and
  accessibility permission, so a denied microphone is explained instead of "unknown".
- Release checks now launch the recording bar for real and verify the Mac bundle's icons.

## [1.6.3] - 2026-10-01

### Changed
- macOS microphone permission text now names Clipboard+.
- The release version bump command now updates both platform bootstrap fallback tags.

### Fixed
- The recording pill now stays out of the app dock, task switcher and taskbar across
  macOS, Windows and Linux.

### Fixed
- The macOS/Linux and Windows bootstrap commands now fall back to the current
  release when GitHub's release API is unavailable.

## [1.6.2] - 2026-10-01

### Fixed
- In-app updates now select the macOS installer for the computer's architecture.

## [1.6.1] - 2026-10-01

### Added
- Releases now include a native Intel Mac installer and run the macOS checks on both
  Intel and Apple silicon hosts. Choose the installer that matches the Mac.

## [1.6.0] - 2026-10-01

### Fixed
- macOS login items now unload the correct service target and recover an outdated
  launcher after an app rename, restoring the missing menu bar icon after reinstall.
- Recent-copy actions now accept pystray's callback arguments on Windows/Linux.
- Closing the recording pill cancels its pending frame callback; GUI fixtures
  release Tk objects on the UI thread before later background tests run.
- Clipboard capture recovers from a database that cannot open, backs off after failed
  process launches, and stops when quitting even after the tray adopted an existing service.
- macOS shortcut presets applied in Settings take effect immediately; a refused choice
  restores the previous saved shortcut. Unreadable shortcut events never start recording.
- macOS skips clipboard reads replaced during the privacy check. An expired capture
  pause now refreshes the menu even when preferences have not changed.
- The macOS installer validates a staged bundle before replacement, refuses unrelated
  or running apps, and restores the previous app if copying, validation, or launch fails.

### Added
- macOS right-click and Control-click menu with history, capture pause/resume, dictation,
  shortcut recording, Settings, Open at Login, website, and Quit.
- Native AppKit menu and real pystray callback regression checks; GUI dependencies are
  tested on each CI platform. Local shell gates now include the packaging scripts.

### Changed
- Windows left-click opens history or the app window; recording starts only from its
  explicit action or shortcut. Busy transcription actions are disabled.
- macOS Clear History uses the shared scope/favorites confirmation dialog. Escape clears
  search first, then closes the popover. Settings exposes Open at login on all platforms.
- Missing macOS icon images fall back to a visible `C+` title; timer and Windows shortcut
  callback errors are contained so the next action can still work.

## [1.5.0] - 2026-09-29

### Added
- Optional concise drafts for a transcript using a separate text model. The original stays
  available for review, and the draft is copied only when you choose. The service can run
  locally or use an endpoint you configure; sending text to a remote service requires
  explicit permission in Settings. Platforms: all.

### Changed
- Clipboard history opens and searches in the background, keeping the window responsive
  while saved items load. Repeated searches reuse results until the database changes.
  Platforms: all.
- Clipboard opening, search, and storage errors now offer a clear recovery path. Tray and
  menu bar launches report failures instead of silently leaving the app unavailable.
  Platforms: all.

## [1.4.3] - 2026-09-29

### Changed
- CI and release workflows use actions/checkout 7.0.1 and actions/setup-python 7.0.0 (Dependabot #1).
  No change to the app. Platforms: build tooling for macOS, Windows and Linux.

## [1.4.2] - 2026-09-29

### Changed
- One default shortcut on every platform, so it works the same everywhere: dictation is
  **Win+Shift+D** (Windows), **Super+Shift+D** (Linux) and **⇧⌘D** (macOS); clipboard
  history is the same with F. Ctrl+Shift+D and Ctrl+Shift+F did not work everywhere
  (browsers, terminals and other apps took them first) and are now one-click presets.
  A shortcut you already chose is kept. Combinations the system owns (Win+D, ⌘Space,
  ⌥⌘D which shows the Dock, and similar) are refused in the recorder with a clear reason.
  Platforms: all.
- Saving in the shortcut recorder now stays on the page and asks you to press the new
  shortcut, so you know it works before you leave. Platforms: all.
- The Linux desktop shortcut (GNOME, KDE, Sway, Hyprland) now runs
  `dictate-toggle --via-shortcut` / `app.py --clipboard --via-shortcut`. Existing
  bindings without the marker keep working; the app rewrites its own on start.
  Platforms: Linux.

### Added
- **Test it** for every shortcut: in Settings (both rows), after Save in the recorder,
  on Home when a shortcut has not been heard yet, and in the first-run tutorial. It asks
  you to press the shortcut and says "It works", or, after 8 seconds, why it was not
  heard (a clash the desktop reported, a registration that failed, or the likely cause)
  with one-click buttons that try the next shortcut that can work, ending with the exact
  steps to set one by hand (and, on Linux, a Copy command button). The result is kept,
  so Home and Settings stay truthful. Platforms: all; on Linux the press is heard
  through the `--via-shortcut` marker, on macOS and Windows by the menu bar and tray.

### Fixed
- Two release runs for the same version can no longer overlap (the draft job recreates
  the draft release): the release workflow queues them.

### Fixed
- Clearing the clipboard history with a Clipboard+ account connected now clears the
  account too, so the web dashboard matches: the dialog's default is "Everywhere: this
  device and your Clipboard+ account" ("This device only" is an explicit choice and says the
  web copy stays). The request is saved before anything is cleared here, so a crash or
  going offline never leaves "cleared here, account never told"; it finishes on its own,
  and the window says so ("Cleared everywhere ✓", or "Will finish clearing your account
  when you're back online"). A copy made during the clear is kept and syncs.
- Deleting or clearing on the web now reaches the desktop even when it happened outside
  the sync window (a web "clear all", a long-offline computer): the app compares its
  history with the account's complete listing about every 30 minutes and on Sync now, and
  removes only items it has confirmed for over 10 minutes, with nothing unsent, that a
  complete, error-free listing (read twice) no longer holds.
- Undo after deleting an item no longer deletes or duplicates it on the account, and
  items removed by the retention limit stay on the account without coming back here.
  "Delete all clipboard data" stays on this device.
  Platforms: all (shared code for macOS, Windows and Linux).

## [1.4.1] - 2026-09-29

### Fixed
- macOS: recording a shortcut saved Command as Option and Option+letter as an invalid
  key, so the shortcut you pressed was not the one registered. Modifiers now come from
  the key event's state and Option combos use the hardware key code. Registration
  failures report their real reason, errors in the shortcut handler are logged, and the
  handler never blocks. Settings shows "Ready" and "Heard" so you can test a shortcut.
  Platforms: all (the recorder), macOS (the Carbon handler).

### Changed
- The clipboard history shortcut uses the same recorder as dictation (change it to any
  keys, turn it off, presets as quick picks) on macOS, Windows and Linux.

### Added
- Image clips in the recent copies now read "Image 1280×720" on macOS, Windows and Linux,
  and the Linux tray shows their thumbnail (as macOS already did). Platforms: Linux and
  macOS show the picture; the Windows tray cannot draw pictures in menus, so it is text.
- A parity test keeps the macOS menu bar and the Linux/Windows tray from drifting apart,
  and a pull request checklist item asks which platforms a change covers.

## [1.4.0] - 2026-09-29

### Added
- Browser sign-in for the Clipboard+ account and Ctrl+Shift shortcuts.
- LICENSE (MIT), third-party notices, security policy, contributing guide,
  issue templates, Dependabot, CodeQL and dependency auditing.
- Release pipeline: bumping `APP_VERSION` on main publishes the release automatically, tests gate it,
  ffmpeg downloads are checksum-verified, tools are version pinned, and macOS
  notarization / Windows signing run when signing secrets are configured.
- `--purge` / `-Purge` on the uninstallers to also erase user data.

### Changed
- Release builds are limited to the three main installers (macOS Apple
  Silicon DMG, Windows setup, Linux AppImage).
- Open Clipboard, not Dictation, for clipboard-only users; the Windows
  executable is named `Clipboard+.exe`.

## [1.3.2] - 2026-09-28
- Ship separate Apple Silicon and Intel Mac installers.

## [1.3.1] - 2026-09-28
- Harden all three release builds for desktop.
- Run the macOS runtime import test after strict typing.

## [1.3.0] - 2026-09-28
- First release with packaged installers: PyInstaller onedir bundles wrapped as
  a macOS DMG (ad-hoc signed, with a double-click installer), a Windows Inno
  Setup installer and a Linux AppImage, each bundling ffmpeg and whisper.cpp.
- Frozen-install relaunch, self-update path (disabled for v1 pending a safe
  swap), AppRun dispatcher, macOS Open-at-Login for the packaged app.
- Cross-platform CI fixes; strict typing gate repaired.

## Before 1.3.0 (2026-09-26 to 2026-09-28)
- Renamed to Clipboard+ with new logo and palette; login item and upgrade fixes.
- Clipboard history: search/clear actions in the tray menu, favorites disk
  bounds, secure erase, damaged-history recovery, secrets are never stored.
- Dictation: resumable model downloads, truthful results, shortcut retries.
- Privacy: nothing is reported until the user opts in; quiet crash reports.
- Install lives in `~/.local/lib/whisper-dictation`; hardened updater.
