# AGENTS.md — Clipboard+ desktop app (wayland-whisper-dictation)

Rules for every agent (Claude, Codex, Hermes) working in this repo.

## What it is

A desktop app with two features, each optional: **Dictation** (press a global
shortcut, speak, the transcript is pasted or copied; whisper.cpp on-device or
the user's own AI service) and **Clipboard+** (searchable clipboard history,
optionally synced to a Clipboard+ account). Python + Tk, Linux first (GNOME
Wayland/X11), also macOS and Windows.

- `lib/app.py` — the window (setup, Dictation, Clipboard, Settings tabs)
- `lib/dictation.py` — recording/transcription worker and the CLI
- `lib/tray.py`, `lib/menubar.py` — the always-running tray / menu bar app
- `lib/overlay.py` — the recording pill
- `lib/clip*.py`, `lib/clipboardplus.py` — clipboard capture, storage, sync
- `setup-desktop.py`, `install.sh`, `bootstrap.*` — installers
- The download page lives on the Clipboard+ site (clipboardplus.apercallc.com); this repo
  no longer carries a `site/` (retired 2026-09-28 — was an unmaintained GitHub Pages mirror)

## Verify before claiming done

```sh
sh tests/check.sh          # all of CI's checks (~1 min): lint, mypy for Linux/macOS/Windows,
                           # shellcheck, shfmt, tests + coverage under Xvfb
sh tests/check.sh --full   # also Python 3.10 and the 5 distro installs in Docker (~10 min)
sh tests/with-xvfb.sh /usr/bin/python3 -m unittest discover -s tests   # tests only
```

Run it before pushing instead of waiting on GitHub Actions; `git config core.hooksPath
.githooks` makes every push run it. Run `--full` after touching an installer.
check.sh picks a Python with Tk (the shell's Homebrew python has none); GUI tests run
under Xvfb, never on the user's display.

## Working rules

1. The user runs the **installed copy** in `~/.local/lib/whisper-dictation`, not the repo. After
   changing app code, `./install.sh` is needed before they see it — ask first.
2. Every user-facing change keeps the calm, plain-language voice of the UI copy.
3. Never send transcripts, clipboard content or keys anywhere the user did not
   choose; telemetry must stay free of content (see `lib/telemetry.py`).
4. Add a test with every bug fix; keep `mypy --strict` and ruff clean.
5. **Versions: one source, never hand-edited.** The version lives in `lib/desktop.py`
   (`APP_VERSION`). Never type a version number into `pyproject.toml`, the bootstrap
   scripts, `Info.plist`, the installer or a workflow. To release, run
   `python tools/bump_version.py X.Y.Z` (it also rolls `CHANGELOG.md`'s `[Unreleased]`
   section), then commit. Only bump when the user asks for a release. If you add a new
   place that needs the version, derive it from `APP_VERSION` or add it to
   `tools/bump_version.py` plus its test in `tests/test_packaging.py`.
