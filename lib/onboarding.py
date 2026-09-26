"""Interactive, local-first setup. Never starts the microphone."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path

import desktop
import dictation

MODELS = {
    "en": ("base.en", "a03779c86df3323075f5e796cb2ce5029f00ec8869eee3fdfb897afe36c6d002"),
    "auto": ("base", "60ed5bc3dd14eea856493d334349b405782ddcaf0028d4b5df4088345fba2efe"),
}


def validate_model(path: Path) -> Path:
    """Reject common wrong-file selections before the first recording."""
    path = path.expanduser().resolve()
    if not path.is_file():
        raise dictation.DictationError("Model not found; settings were not changed.")
    try:
        with path.open("rb") as stream:
            magic = stream.read(4)
    except OSError as exc:
        raise dictation.DictationError("The selected model could not be read.") from exc
    if magic != b"lmgg":
        raise dictation.DictationError(
            "The selected file is not a whisper.cpp GGML model; settings were not changed."
        )
    return path


def download_model(folder: Path, language: str) -> Path:
    name, expected = MODELS[language]
    destination = dictation.private_dir(folder) / f"ggml-{name}.bin"
    if destination.is_file():
        with destination.open("rb") as stream:
            digest = hashlib.sha256()
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest() == expected:
            return destination
        raise dictation.DictationError(
            "Existing model failed verification; move it aside and retry."
        )
    fd, partial = tempfile.mkstemp(prefix="model-", suffix=".part", dir=folder)
    try:
        digest = hashlib.sha256()
        total = 0
        url = f"https://huggingface.co/ggerganov/whisper.cpp/resolve/main/{destination.name}"
        print("Downloading the free base model (about 148 MB)...", flush=True)
        with os.fdopen(fd, "wb") as output:
            with urllib.request.urlopen(url, timeout=60) as response:
                while block := response.read(1024 * 1024):
                    total += len(block)
                    if total > 160_000_000:
                        raise dictation.DictationError("Model download exceeded the expected size.")
                    digest.update(block)
                    output.write(block)
        if digest.hexdigest() != expected:
            raise dictation.DictationError("Model checksum mismatch; download was not activated.")
        os.replace(partial, destination)
    finally:
        Path(partial).unlink(missing_ok=True)
    return destination


def run(paths: dictation.Paths) -> None:
    if dictation.busy(paths):
        raise dictation.DictationError("Finish the current recording before setup.")
    print("Welcome to Whisper Dictation. Setup never starts recording.")
    print("Local transcription is free and offline after the model download.")
    values = dictation.Config(paths).values.copy()
    existing = dictation.read_json(paths.config)
    configured = bool(existing.get("model") or existing.get("endpoint"))
    if configured and input("Update your existing settings? [y/N] ").lower() != "y":
        print("Settings unchanged.")
        return
    choice = input("Language: 1 English, 2 multilingual/auto [1]: ").strip() or "1"
    if choice not in ("1", "2"):
        raise dictation.DictationError("Choose 1 or 2; settings were not changed.")
    language = "en" if choice == "1" else "auto"
    model = input(
        "Your compatible GGML model path (Enter to download the free base model): "
    ).strip()
    model_path = validate_model(
        Path(model).expanduser().resolve()
        if model
        else download_model(paths.config.parent / "models", language)
    )
    values.update(backend="local", model=str(model_path), language=language, allow_remote=False)
    for key, binary in (("whisper_bin", "whisper-cli"), ("ffmpeg", "ffmpeg")):
        values[key] = shutil.which(binary) or values[key]
    print("Available microphones (listing may ask for microphone permission):")
    subprocess.run(
        desktop.recorder_command(values, listing=True),
        timeout=15,
        check=False,
        **desktop.process_options(),
    )
    values["device"] = (
        input(f"Microphone name/index [{values['device']}]: ").strip() or values["device"]
    )
    if desktop.platform_name() == "windows" and values["device"] == "default":
        raise dictation.DictationError("Windows needs the exact audio device name listed above.")
    values["prompt"] = input("Optional names or specialist vocabulary (Enter to skip): ").strip()
    values["live"] = input("Enable experimental rolling live previews? [y/N] ").lower() == "y"
    candidate = dictation.Config(paths)
    candidate.values = values
    candidate.check(recording=True)
    saved = dictation.DEFAULTS | existing
    for key in ("ffmpeg", "whisper_bin"):
        if not saved[key]:
            saved[key] = values[key]
    saved.update(
        backend="local",
        model=str(model_path),
        language=language,
        device=values["device"],
        prompt=values["prompt"],
        live=values["live"],
        allow_remote=False,
    )
    dictation.private_dir(paths.config.parent)
    dictation.atomic(paths.config, json.dumps(saved, indent=2))
    print(f"Settings saved: {paths.config}")
    print("Ready: run dictate-toggle to record; run it again to stop, then paste.")
    print("Try: 'Hello world. New paragraph. This is my first dictation.'")
    print("--cancel cancels; --transcribe retries retained audio; --copy-last copies saved text.")
    print("--watch displays live drafts when enabled. Nothing has been recorded by setup.")
    if desktop.platform_name() == "macos":
        print("Add the installed command to Apple Shortcuts > Run Shell Script and assign a key.")
    elif desktop.platform_name() == "windows":
        print("Use the Start Menu Whisper Dictation shortcut or Ctrl+Alt+D.")
    else:
        print("GNOME installer shortcut: Super+Shift+D. Other desktops: bind dictate-toggle.")
