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
- `site/` — unused; the download page lives on the Clipboard+ site

## Verify before claiming done

```sh
sh tests/check.sh          # ruff, format, mypy --strict, shellcheck, shfmt, tests
sh tests/with-xvfb.sh python3 -m unittest discover -s tests   # tests only, headless
```

Use `/usr/bin/python3` (the shell's Homebrew python has no Tk). GUI tests run
under Xvfb, never on the user's display.

## Working rules

1. The user runs the **installed copy** in `~/.local/lib/whisper-dictation`, not the repo. After
   changing app code, `./install.sh` is needed before they see it — ask first.
2. Every user-facing change keeps the calm, plain-language voice of the UI copy.
3. Never send transcripts, clipboard content or keys anywhere the user did not
   choose; telemetry must stay free of content (see `lib/telemetry.py`).
4. Add a test with every bug fix; keep `mypy --strict` and ruff clean.
