# Contributing

Thanks for helping. Read `AGENTS.md` for the architecture and house rules.

## Setup

    python -m pip install -r requirements-dev.txt
    git config core.hooksPath .githooks   # runs the checks before every push

## Before you open a pull request

    sh tests/check.sh          # lint, format, mypy --strict, shell checks, tests + coverage
    sh tests/check.sh --full   # also Python 3.10 and bare installs in Docker

CI (`.github/workflows/quality.yml`) runs the same on Linux, macOS and Windows.

## Guidelines

- Keep changes small and focused; add or update tests with the code.
- The app version lives in `lib/desktop.py` (`APP_VERSION`); `pyproject.toml`,
  the macOS `Info.plist` template and the Windows installer must agree with it
  (a test enforces this). Add a `CHANGELOG.md` entry for user-visible changes.
- Do not add runtime dependencies to the standard-library core without
  discussion; GUI pins live in `requirements-gui.txt` only.
- Never commit secrets, models, or clipboard/transcript data.
- Releases are automatic; see "Releasing" below.

By contributing you agree your work is licensed under the MIT license.

## Platform parity

Every fix and feature ships to macOS, Windows and Linux together. Where to make a change:

- `lib/app.py` (the window) and the shared modules (`hotkeys.py`, `clipcontrol.py`,
  `clipstore.py`, `updates.py`, `app_service.py`) are one code path for all platforms.
- `lib/menubar.py` is the macOS menu bar; `lib/tray.py` is the Linux and Windows tray
  (`lib/traymenu.py` adds thumbnails on Linux). A change to one usually needs the other.
  Move logic both need into a shared module (as `clipcontrol.preview_text` does) rather
  than copying it.
- `tests/test_platform_parity.py` fails when a menu capability exists in one of the two
  files and not the other. If a difference is deliberate, list it in `ALLOWED` there,
  with the reason.
- A release publishes only when the macOS, Windows and Linux builds all succeed and pass
  their tests, so a platform that breaks blocks everyone. Known limit: the Windows tray
  (pystray) cannot draw pictures in menu items, so image clips are text there
  ("Image 1280×720"); macOS and Linux show a thumbnail.

## Releasing

Run `python tools/bump_version.py X.Y.Z`, commit, and merge to `main`. The release
workflow sees the new `APP_VERSION`, runs the tests, builds the macOS, Windows and
Linux installers, and publishes `vX.Y.Z` with checksums once everything passed.
There is no tag to push and nothing to upload.
