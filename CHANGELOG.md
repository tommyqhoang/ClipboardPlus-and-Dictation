# Changelog

All notable changes to Clipboard+ / Dictation. Reconstructed from the git
history; versions follow the `v*` release tags.

## [Unreleased]

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
