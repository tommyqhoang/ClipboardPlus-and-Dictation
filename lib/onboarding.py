"""Interactive, local-first setup. Never starts the microphone."""

from __future__ import annotations

import errno
import hashlib
import http.client
import json
import logging
import math
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import desktop
import dictation
import hotkeys
import permissions
import telemetry

try:
    import logsetup

    _log = logsetup.get_logger("onboarding")
except ImportError:
    _log = logging.getLogger(__name__)

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
        check_disk_space(folder, MODEL_BYTES - total)
        url = f"https://huggingface.co/ggerganov/whisper.cpp/resolve/main/{destination.name}"
        say("Downloading the free base model (about 148 MB)...")
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


MODEL_BYTES = 160_000_000  # The largest model download allowed (base is about 148 MB).
MODEL_HOST = ("huggingface.co", 443)


def say(message: str = "") -> None:
    """Plain, line-by-line output: no colour, no cursor movement, no progress bars.

    A screen reader reads each line once, in order, and a log captures all of it.
    """
    print(message, flush=True)


def is_interactive() -> bool:
    """Whether there is someone at a terminal to answer questions."""
    try:
        return bool(sys.stdin and sys.stdin.isatty())
    except (AttributeError, ValueError):
        return False


@dataclass
class SetupOptions:
    """Answers given up front (command-line flags); anything unset is asked or defaulted."""

    language: str | None = None  # "en" or "auto"
    model: str | None = None  # A GGML model file to use instead of downloading
    device: str | None = None  # Microphone name or index
    share_usage: bool | None = None  # Consent to anonymous crash reports and statistics
    interactive: bool | None = None  # None: ask only when stdin is a terminal
    skip_mic_test: bool = False


def _connectable(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        connect((host, port), timeout).close()
        return True
    except OSError as exc:
        _log.info("no route to %s: %s", host, exc)
        return False


def preflight_download(
    folder: Path, needed: int = MODEL_BYTES, online: Callable[[], bool] | None = None
) -> None:
    """Fail early, with what to do, when the model download cannot succeed.

    Checks the network (so a download doesn't sit on a dead connection) and the free
    disk space (so it doesn't fill the disk half way). Raises DictationError.
    """
    if not (online or (lambda: _connectable(*MODEL_HOST)))():
        raise dictation.DictationError(
            "You appear to be offline, so the speech model can’t be downloaded. "
            "Connect to the internet and run setup again, or copy a GGML model "
            "(ggml-base.en.bin from huggingface.co/ggerganov/whisper.cpp) to this "
            "computer and choose it with --model PATH."
        )
    check_disk_space(folder, needed)


def check_disk_space(folder: Path, needed: int = MODEL_BYTES) -> None:
    """Raise DictationError when `folder`'s disk has less than `needed` bytes free."""
    probe = folder
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        free = shutil.disk_usage(probe).free
    except OSError as exc:
        _log.warning("cannot read free disk space for %s: %s", probe, exc)
        return  # Unknown: let the download report a real shortage.
    if free < needed:
        raise dictation.DictationError(
            f"Not enough free disk space for the speech model: {needed // 1_000_000} MB "
            f"needed, {free // 1_000_000} MB free on the disk holding {probe}. "
            "Free some space and run setup again."
        )


def check_microphone(values: dict[str, Any], seconds: float = 1.0) -> tuple[bool, str]:
    """Record about a second from the chosen microphone; (worked, what to tell the user).

    A microphone that opens but only returns silence still passes (a muted mic is
    a real setting) but the message says so.
    """
    blocked = permissions.microphone_blocked()
    if blocked:
        return False, blocked
    probe = dict(values, max_seconds=max(1, math.ceil(seconds)))
    try:
        result = subprocess.run(
            desktop.recorder_command(probe),
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=seconds + 6,
            check=False,
            **desktop.process_options(),
        )
    except subprocess.TimeoutExpired:
        return False, "The microphone did not answer in time. Check that it is connected."
    except OSError as exc:
        return False, permissions.microphone_message(reason=str(exc))
    pcm = result.stdout or b""
    if len(pcm) < 3200:  # Less than 0.1 second of 16 kHz 16-bit audio.
        reason = permissions.tail_of(result.stderr) or f"exit status {result.returncode}"
        return False, permissions.microphone_message(reason=reason)
    if not any(pcm):
        return True, "The microphone works but recorded only silence; check that it isn’t muted."
    return True, "The microphone recorded a short test."


class Wizard:
    """The setup steps, asking questions only when someone is there to answer them."""

    def __init__(self, paths: dictation.Paths, options: SetupOptions) -> None:
        self.paths = paths
        self.options = options
        self.interactive = is_interactive() if options.interactive is None else options.interactive

    def ask(self, prompt: str, default: str = "") -> str:
        """An answer, or `default` at once when nobody can answer (never blocks)."""
        if not self.interactive:
            return default
        try:
            return input(prompt).strip() or default
        except EOFError:
            self.interactive = False
            return default

    def confirm(self, prompt: str) -> bool:
        return self.ask(prompt + " [y/N] ").lower() == "y"

    def step(self, number: int, title: str) -> None:
        say()
        say(f"Step {number} of {STEPS}: {title}")

    def language(self) -> str:
        if self.options.language:
            return self.options.language
        self.step(1, "Language")
        choice = self.ask("Language: 1 English, 2 multilingual/auto [1]: ", "1")
        if choice not in ("1", "2"):
            raise dictation.DictationError("Choose 1 or 2; settings were not changed.")
        return "en" if choice == "1" else "auto"

    def model(self, language: str) -> Path:
        given = self.options.model or self.ask(
            "Your compatible GGML model path (Enter to download the free base model): "
        )
        if given:
            return validate_model(Path(given).expanduser().resolve())
        folder = self.paths.config.parent / "models"
        preflight_download(folder)
        milestones = {"next": 10}

        def progress(done: int, total: int) -> None:
            if total and done * 100 // total >= milestones["next"]:
                say(f"Downloaded {done * 100 // total} percent.")
                milestones["next"] = done * 100 // total // 10 * 10 + 10

        return validate_model(download_model(folder, language, progress))

    def microphone(self, values: dict[str, Any]) -> None:
        self.step(3, "Microphone")
        say("Available microphones (listing may ask for microphone permission):")
        subprocess.run(
            desktop.recorder_command(values, listing=True),
            timeout=15,
            check=False,
            **desktop.process_options(),
        )
        values["device"] = (
            self.options.device
            or self.ask(f"Microphone name/index [{values['device']}]: ")
            or values["device"]
        )
        if desktop.platform_name() == "windows" and values["device"] == "default":
            raise dictation.DictationError(
                "Windows needs the exact audio device name listed above."
            )
        if self.options.skip_mic_test:
            return
        say("Testing the microphone for about a second. Say something.")
        worked, detail = check_microphone(values)
        say(detail)
        if worked:
            return
        if not self.interactive:
            say("Continuing without a working microphone test; fix it before recording.")
            return
        if not self.confirm("Save this microphone anyway?"):
            raise dictation.DictationError(
                "The microphone did not record; settings were not changed. " + detail
            )

    def consent(self) -> None:
        """Ask, in plain words, whether to share anonymous reports. Never on by default."""
        setter = getattr(telemetry, "set_consent", None)
        if setter is None:
            return
        self.step(5, "Anonymous reports (optional)")
        say(
            "Clipboard+ can send anonymous crash reports and usage counts to help fix "
            "bugs. They never include your clipboard, transcripts, audio, file names or keys."
        )
        say("Nothing is sent unless you say yes here. You can change this in Settings.")
        if self.options.share_usage is not None:
            answer = self.options.share_usage
        else:
            answer = self.confirm("Share anonymous crash reports and usage statistics?")
        setter(answer)
        say("Sharing is on." if answer else "Sharing is off.")

    def finish(self, values: dict[str, Any], existing: dict[str, Any], model_path: Path) -> None:
        candidate = dictation.Config(self.paths)
        candidate.values = values
        candidate.check(recording=True)
        saved = dictation.DEFAULTS | existing
        for key in ("ffmpeg", "whisper_bin"):
            if not saved[key]:
                saved[key] = values[key]
        saved.update(
            backend="local",
            model=str(model_path),
            language=values["language"],
            device=values["device"],
            prompt=values["prompt"],
            live=values["live"],
            allow_remote=False,
        )
        dictation.private_dir(self.paths.config.parent)
        dictation.atomic(self.paths.config, json.dumps(saved, indent=2))

    def run(self) -> None:
        paths = self.paths
        say(f"Welcome to {hotkeys.APP_NAME}. Setup never starts recording.")
        say("Local transcription is free and offline after the model download.")
        if not self.interactive:
            say("No terminal to ask questions in: using your flags and the default choices.")
        values = dictation.Config(paths).values.copy()
        existing = dictation.read_json(paths.config)
        configured = bool(existing.get("model") or existing.get("endpoint"))
        if configured and not self.confirm("Update your existing settings?"):
            say("Settings unchanged.")
            return
        language = self.language()
        self.step(2, "Speech model")
        model_path = self.model(language)
        values.update(backend="local", model=str(model_path), language=language, allow_remote=False)
        for key, binary in (("whisper_bin", "whisper-cli"), ("ffmpeg", "ffmpeg")):
            values[key] = shutil.which(binary) or values[key]
        self.microphone(values)
        self.step(4, "Vocabulary and live drafts")
        values["prompt"] = self.ask("Optional names or specialist vocabulary (Enter to skip): ")
        values["live"] = self.confirm("Enable experimental rolling live previews?")
        self.finish(values, existing, model_path)
        self.consent()
        say(f"Settings saved: {paths.config}")
        goodbye()


STEPS = 5


def goodbye() -> None:
    say("Ready: run dictate-toggle to record; run it again to stop, then paste.")
    say("Try: 'Hello world. New paragraph. This is my first dictation.'")
    say("--cancel cancels; --transcribe retries retained audio; --copy-last copies saved text.")
    say("--watch displays live drafts when enabled. Nothing has been recorded by setup.")
    platform = desktop.platform_name()
    if platform == "macos":
        say(
            f"Open {hotkeys.APP_NAME} from Applications; it registers {hotkeys.DEFAULT.label()} itself."
        )
        say(permissions.accessibility_explanation())
    elif platform == "windows":
        say(f"Use the Start Menu {hotkeys.APP_NAME} shortcut or {hotkeys.DEFAULT.label()}.")
    else:
        say(
            f"GNOME installer shortcut: {hotkeys.DEFAULT.label()}. Other desktops: bind dictate-toggle."
        )


def run(paths: dictation.Paths, options: SetupOptions | None = None) -> None:
    if dictation.busy(paths):
        raise dictation.DictationError("Finish the current recording before setup.")
    Wizard(paths, options or SetupOptions()).run()
