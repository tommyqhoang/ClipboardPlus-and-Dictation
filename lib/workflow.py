"""Read-only terminal status and live drafts; never captures a microphone."""

from __future__ import annotations

import contextlib
import time
from typing import Any

import dictation as d


def display_text(text: str) -> str:
    # Model/transcript text must not inject terminal escape/control sequences.
    return "".join(char for char in text if char.isprintable() or char in "\n\t")


def snapshot(paths: d.Paths) -> dict[str, Any]:
    current = {"phase": "idle", **d.read_json(paths.state)}
    active = d.busy(paths)
    if not active and current["phase"] in ("starting", "recording", "transcribing", "cancelling"):
        current.update(
            phase="interrupted", message="Worker exited; use --transcribe for retained audio."
        )
    if active and isinstance(current.get("started_at"), (int, float)):
        current["elapsed_seconds"] = max(0, round(time.time() - current["started_at"], 1))
    return current | {"active": active, "retained_audio": paths.audio.exists()}


def watch(paths: d.Paths) -> None:
    print("Session monitor. Ctrl+C closes this viewer; it does NOT stop recording.", flush=True)
    print("Press your shortcut again to stop, or run --cancel in another terminal.", flush=True)
    previous = ""
    previous_draft = ""
    while True:
        current = snapshot(paths)
        elapsed = int(current.get("elapsed_seconds", 0))
        line = f"[{str(current['phase']).upper()}] {elapsed // 60:02d}:{elapsed % 60:02d}"
        if current.get("message"):
            line += " — " + str(current["message"])
        if line != previous:
            print(display_text(line), flush=True)
            previous = line
        if not current["active"]:
            break
        with contextlib.suppress(FileNotFoundError):
            draft = paths.preview.read_text(encoding="utf-8")
            if draft != previous_draft:
                print("Live draft (provisional):\n" + display_text(draft), flush=True)
                previous_draft = draft
        time.sleep(0.2)
    if current["retained_audio"]:
        print("Audio retained: --transcribe retries; --discard deletes the recording.")
    if paths.text.exists():
        print("Last saved transcript:\n" + display_text(paths.text.read_text(encoding="utf-8")))
        print("--copy-last copies it. --concise makes an optional shorter draft.")
