"""Interactive, local-first setup. Never starts the microphone."""

from __future__ import annotations

import errno
import hashlib
import http.client
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

import desktop
import dictation
import hotkeys

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


# A single connection attempt; a broken address family (commonly IPv6) then
# costs seconds instead of the full read timeout for every advertised address.
CONNECT_ATTEMPT_SECONDS = 4.0
# Partial files untouched this long belong to an interrupted download.
STALE_PARTIAL_SECONDS = 600


def connect(
    address: tuple[str, int], timeout: object = None, source_address: Any = None
) -> socket.socket:
    """Connect like socket.create_connection, alternating address families."""
    host, port = address
    queues: dict[int, list[Any]] = {}
    for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
        queues.setdefault(info[0], []).append(info)
    ordered: list[Any] = []
    while any(queues.values()):
        for queue in queues.values():
            if queue:
                ordered.append(queue.pop(0))
    read_timeout = float(timeout) if isinstance(timeout, (int, float)) else None
    attempt = min(read_timeout or CONNECT_ATTEMPT_SECONDS, CONNECT_ATTEMPT_SECONDS)
    error: OSError = OSError(f"No network address found for {host}.")
    for family, kind, proto, _, target in ordered:
        sock = socket.socket(family, kind, proto)
        try:
            sock.settimeout(attempt)
            if source_address:
                sock.bind(source_address)
            sock.connect(target)
            sock.settimeout(read_timeout)
            return sock
        except OSError as exc:
            sock.close()
            error = exc
    raise error


class _Connection(http.client.HTTPSConnection):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        setattr(self, "_create_connection", connect)


class _Handler(urllib.request.HTTPSHandler):
    def https_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
        return self.do_open(_Connection, req, context=ssl.create_default_context())


def open_url(url: str | urllib.request.Request) -> Any:
    return urllib.request.build_opener(_Handler).open(url, timeout=60)


class DownloadPaused(dictation.DictationError):
    """A download stopped intentionally and can continue from its partial file."""


def download_model(
    folder: Path,
    language: str,
    progress: Callable[[int, int], None] | None = None,
    pause: threading.Event | None = None,
) -> Path:
    dictation.private_dir(folder)
    fd = desktop.lock(folder / ".download.lock")
    if fd is None:
        raise dictation.DictationError(
            "A model download is already running. Wait for it to finish."
        )
    try:
        return _download_model(folder, language, progress, pause)
    finally:
        os.close(fd)
        # Keep the lock inode: unlinking permits concurrent callers to lock
        # different files with the same name.


def _download_model(
    folder: Path,
    language: str,
    progress: Callable[[int, int], None] | None,
    pause: threading.Event | None,
) -> Path:
    name, expected = MODELS[language]
    destination = dictation.private_dir(folder) / f"ggml-{name}.bin"
    if destination.is_file():
        with destination.open("rb") as stream:
            digest = hashlib.sha256()
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest() == expected:
            return destination
        # Damaged or replaced: keep it aside (never delete a user's file) and fetch anew.
        os.replace(destination, destination.with_name(destination.name + ".damaged"))
    for stale in folder.glob("ggml-*.bin.part"):
        if time.time() - stale.stat().st_mtime > STALE_PARTIAL_SECONDS:
            stale.unlink(missing_ok=True)
    partial = destination.with_suffix(".bin.part")
    if partial.is_symlink():
        raise dictation.DictationError("The partial model file must not be a symlink.")
    keep_partial = False
    try:
        digest = hashlib.sha256()
        total = partial.stat().st_size if partial.exists() else 0
        if total > 160_000_000:
            partial.unlink()
            total = 0
        if total:
            with partial.open("rb") as previous:
                for block in iter(lambda: previous.read(1024 * 1024), b""):
                    digest.update(block)
        url = f"https://huggingface.co/ggerganov/whisper.cpp/resolve/main/{destination.name}"
        print("Downloading the free base model (about 148 MB)...", flush=True)
        with partial.open("ab" if total else "wb") as output:
            try:
                if pause is not None and pause.is_set():
                    keep_partial = True
                    raise DownloadPaused("Download paused. Resume when you’re ready.")
                request = (
                    urllib.request.Request(url, headers={"Range": f"bytes={total}-"})
                    if total
                    else url
                )
                with open_url(request) as response:
                    status = getattr(response, "status", 200)
                    if total and status == 200:
                        # Some mirrors ignore Range; restart instead of appending
                        # another whole model to the retained prefix.
                        output.seek(0)
                        output.truncate()
                        total = 0
                        digest = hashlib.sha256()
                    elif total:
                        content_range = re.fullmatch(
                            r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", "")
                        )
                        if (
                            status != 206
                            or not content_range
                            or int(content_range[1]) != total
                            or int(content_range[2]) + 1 != int(content_range[3])
                        ):
                            raise dictation.DictationError(
                                "The model server returned an invalid resume response. Try downloading again."
                            )
                    size = total + int(response.headers.get("Content-Length") or 0)
                    while True:
                        if pause is not None and pause.is_set():
                            keep_partial = True
                            raise DownloadPaused("Download paused. Resume when you’re ready.")
                        block = response.read(1024 * 1024)
                        if not block:
                            break
                        total += len(block)
                        if total > 160_000_000:
                            raise dictation.DictationError(
                                "Model download exceeded the expected size."
                            )
                        digest.update(block)
                        output.write(block)
                        if progress:
                            progress(total, size)
            except (urllib.error.URLError, http.client.HTTPException, TimeoutError) as exc:
                keep_partial = True
                raise dictation.DictationError(
                    "Couldn’t download the speech model. Check your internet connection and "
                    "try again, or choose a model file or your own AI service instead."
                ) from exc
            except OSError as exc:
                if exc.errno == errno.ENOSPC:
                    raise dictation.DictationError(
                        "Not enough free disk space for the speech model (about 150 MB)."
                    ) from exc
                # A dropped connection (reset, SSL): keep what arrived so a retry resumes.
                keep_partial = True
                raise dictation.DictationError(
                    "The speech model download was interrupted. Try again to resume where it stopped."
                ) from exc
        if digest.hexdigest() != expected:
            raise dictation.DictationError("Model checksum mismatch; download was not activated.")
        os.replace(partial, destination)
    finally:
        if not keep_partial:
            partial.unlink(missing_ok=True)
    return destination


def run(paths: dictation.Paths) -> None:
    if dictation.busy(paths):
        raise dictation.DictationError("Finish the current recording before setup.")
    print(f"Welcome to {hotkeys.APP_NAME}. Setup never starts recording.")
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
        print(
            f"Open {hotkeys.APP_NAME} from Applications; it registers {hotkeys.DEFAULT.label()} itself."
        )
    elif desktop.platform_name() == "windows":
        print(f"Use the Start Menu {hotkeys.APP_NAME} shortcut or {hotkeys.DEFAULT.label()}.")
    else:
        print(
            f"GNOME installer shortcut: {hotkeys.DEFAULT.label()}. Other desktops: bind dictate-toggle."
        )
