"""A local speech engine that stays ready between dictations.

whisper-cli loads the whole model from disk for every recording, which is most
of the wait on a cold cache. When whisper-server (from the same whisper.cpp
build) is installed, a small supervisor keeps one running on this machine only
(127.0.0.1, a port chosen at start) with the model already in memory, and shuts
it down after half an hour unused. Dictation starts it in the background when
recording begins, so the model loads while the user speaks; if it is missing,
slow, or dies, transcription quietly uses whisper-cli exactly as before.
"""

from __future__ import annotations

import argparse
import enum
import json
import logging
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

import desktop
import dictation as d

try:
    import logsetup

    log = logsetup.get_logger("engine")
except ImportError:
    log = logging.getLogger(__name__)

IDLE_SECONDS = 30 * 60.0  # A quiet engine exits; the next dictation starts it again.
STARTUP_SECONDS = 120.0  # Model load allowance before the engine is given up on.
HEALTH_SECONDS = 1.0
MAX_REPLY = 1024 * 1024


class Failure(enum.Enum):
    """Why the ready engine could not answer (transcription then uses whisper-cli)."""

    SERVER_MISSING = "server_missing"  # No whisper-server binary was found.
    MODEL_MISSING = "model_missing"  # The speech model file is not there.
    TIMEOUT = "timeout"  # The engine did not answer in time.
    HTTP_ERROR = "http_error"  # The engine answered with an error status.
    EMPTY_AUDIO = "empty_audio"  # There was no audio to transcribe.
    UNAVAILABLE = "unavailable"  # Could not connect (engine exited or still loading).
    BAD_REPLY = "bad_reply"  # The reply was not the expected JSON.


_MESSAGES = {
    Failure.SERVER_MISSING: "The fast speech engine (whisper-server) isn’t installed; "
    "using the slower whisper-cli. Reinstall Clipboard+ to get it.",
    Failure.MODEL_MISSING: "The speech model file is missing. Open Clipboard+ and download "
    "a model in Settings.",
    Failure.TIMEOUT: "The speech engine took too long to answer. Try a shorter recording or "
    "a smaller model.",
    Failure.HTTP_ERROR: "The speech engine reported an error.",
    Failure.EMPTY_AUDIO: "There was no audio to transcribe. Check your microphone.",
    Failure.UNAVAILABLE: "The speech engine isn’t running yet; it starts with the next recording.",
    Failure.BAD_REPLY: "The speech engine sent something unreadable.",
}


class EngineError(Exception):
    """A classified engine failure; str() is a message safe to show the user."""

    def __init__(self, kind: Failure, detail: str = "") -> None:
        self.kind = kind
        self.detail = detail
        text = _MESSAGES[kind]
        super().__init__(f"{text} ({detail})" if detail else text)


def classify(exc: BaseException) -> EngineError:
    """Sort a low-level exception from talking to the engine into an EngineError."""
    if isinstance(exc, EngineError):
        return exc
    if isinstance(exc, urllib.error.HTTPError):
        return EngineError(Failure.HTTP_ERROR, f"HTTP {exc.code}")
    reason = getattr(exc, "reason", None)
    if isinstance(exc, (TimeoutError, socket.timeout)) or isinstance(
        reason, (TimeoutError, socket.timeout)
    ):
        return EngineError(Failure.TIMEOUT)
    if isinstance(exc, ValueError):
        return EngineError(Failure.BAD_REPLY)
    return EngineError(Failure.UNAVAILABLE, exc.__class__.__name__)


# The most recent problem, for a caller that wants to explain a fallback (None: none yet).
last_failure: EngineError | None = None


def _fail(error: EngineError) -> None:
    global last_failure
    last_failure = error
    log.info("engine: %s", error)


def server_binary(config: d.Config) -> str:
    """The whisper-server path: bundled with this build, beside the configured
    whisper-cli, or on PATH."""
    import shutil

    bundled = desktop.bundled_binary("whisper-server")
    if bundled is not None:
        return str(bundled)
    suffix = ".exe" if sys.platform == "win32" else ""
    cli = str(config.s("whisper_bin"))
    fallback = Path.home() / ".local/opt/whisper.cpp-v1.8.7/build/bin"
    for candidate in (
        "whisper-server",
        str(Path(cli).with_name("whisper-server" + suffix)),
        str(fallback / ("whisper-server" + suffix)),
    ):
        if candidate and (Path(candidate).is_file() or shutil.which(candidate)):
            return candidate
    return ""


def server_command(config: d.Config, port: int, public: Path) -> list[str] | None:
    """Arguments that start whisper-server the way dictation uses it, or None."""
    binary = server_binary(config)
    if not binary:
        return None
    args = desktop.executable(binary) + [
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--public",
        str(public),
        "-m",
        str(Path(config.s("model")).expanduser()),
        "-l",
        config.s("language"),
        "-nt",
        "-t",
        str(config.n("threads")),
    ]
    if config.s("vad_model"):
        args += ["--vad", "--vad-model", str(Path(config.s("vad_model")).expanduser())]
    return args


# -- the shared pointer (runtime/engine.json) ---------------------------


def info_file(paths: d.Paths) -> Path:
    return paths.runtime / "engine.json"


def read_info(paths: d.Paths, clock: Callable[[], float] = time.time) -> dict[str, Any] | None:
    """The running engine's address, or None when nobody usable is registered."""
    try:
        raw = json.loads(info_file(paths).read_text(encoding="utf-8"))
        port, pid, started = raw.get("port"), raw.get("pid"), raw.get("started")
        if not isinstance(port, int) or not isinstance(pid, int):
            return None
        if not isinstance(started, (int, float)) or clock() - started > 12 * 3600:
            return None  # A leftover pointer from an old login session.
        return {"port": port, "pid": pid}
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        log.debug("unreadable engine pointer: %s", exc)
        return None


def write_info(paths: d.Paths, port: int) -> None:
    d.private_dir(paths.runtime)
    d.atomic(
        info_file(paths),
        json.dumps({"port": port, "pid": os.getpid(), "started": time.time()}),
    )


def used_file(paths: d.Paths) -> Path:
    return paths.runtime / "engine-used"


def touch(paths: d.Paths) -> None:
    """Note activity so the supervisor keeps the engine alive."""
    try:
        with used_file(paths).open("a"):
            pass
    except OSError as exc:
        log.debug("could not note engine activity: %s", exc)


# -- client ------------------------------------------------------------


def transcribe(config: d.Config, paths: d.Paths, wav: bytes, timeout: float) -> str | None:
    """Text from the ready engine, or None when it is not usable (caller falls back)."""
    global last_failure
    if len(wav) <= 44:  # A WAV header and no samples.
        _fail(EngineError(Failure.EMPTY_AUDIO))
        return None
    info = read_info(paths)
    if info is None:
        return None
    boundary = "engine-" + os.urandom(8).hex()
    body = bytearray()
    body.extend(
        f'--{boundary}\r\nContent-Disposition: form-data; name="response_format"\r\n\r\n'
        "json\r\n".encode()
    )
    if config.s("language") != "auto":
        body.extend(
            f'--{boundary}\r\nContent-Disposition: form-data; name="language"\r\n\r\n'
            f"{config.s('language')}\r\n".encode()
        )
    if config.s("prompt"):
        body.extend(
            f'--{boundary}\r\nContent-Disposition: form-data; name="prompt"\r\n\r\n'
            f"{config.s('prompt')}\r\n".encode()
        )
    body.extend(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="audio.wav"'
        f"\r\nContent-Type: audio/wav\r\n\r\n".encode()
    )
    body.extend(wav)
    body.extend(f"\r\n--{boundary}--\r\n".encode())
    request = urllib.request.Request(
        f"http://127.0.0.1:{info['port']}/inference",
        data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
            data = response.read(MAX_REPLY + 1)
        touch(paths)
        parsed = json.loads(data)
        if not isinstance(parsed, dict) or "text" not in parsed:
            raise EngineError(Failure.BAD_REPLY)
        last_failure = None
        return str(parsed["text"])
    except (OSError, ValueError, EngineError) as exc:
        _fail(classify(exc))
        return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """The engine never moves; a redirect would mean something else answered."""

    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: Any, headers: Any, newurl: Any
    ) -> None:
        return None


def start(paths: d.Paths, config: d.Config) -> bool:
    """Begin starting the engine (it reports ready via engine.json). Never blocks."""
    if not Path(config.s("model")).expanduser().is_file():
        _fail(EngineError(Failure.MODEL_MISSING))
        return False
    command = server_command(config, _free_port(), _public_dir(paths))
    if command is None:
        _fail(EngineError(Failure.SERVER_MISSING))
        return False
    try:
        subprocess.Popen(
            desktop.relaunch("engine", "--supervise", *command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **desktop.process_options(detached=True),
        )
        return True
    except OSError as exc:
        _fail(EngineError(Failure.UNAVAILABLE, str(exc)))
        return False


def _free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _public_dir(paths: d.Paths) -> Path:
    """An empty directory: nothing but the inference endpoint is served."""
    folder = paths.cache / "engine-public"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


# -- supervisor --------------------------------------------------------


def _healthy(port: int, timeout: float = HEALTH_SECONDS) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=timeout) as response:
            return int(response.status) == 200
    except (OSError, ValueError) as exc:
        log.debug("engine health check failed: %s", exc)
        return False


def supervise(paths: d.Paths, command: list[str]) -> int:
    """Run whisper-server until it is idle too long; the return code is its own."""
    import signal

    port = int(command[command.index("--port") + 1])
    info_file(paths).unlink(missing_ok=True)
    server = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **desktop.process_options(),
    )
    interrupted = False

    def stop(signum: int, frame: Any) -> None:
        nonlocal interrupted
        interrupted = True

    signal.signal(signal.SIGTERM, stop)
    try:
        deadline = time.monotonic() + STARTUP_SECONDS
        while time.monotonic() < deadline:
            if server.poll() is not None:
                return server.returncode  # Bad model or flags; dictation falls back.
            if _healthy(port):
                write_info(paths, port)
                touch(paths)
                break
            time.sleep(0.25)
        else:
            server.terminate()
            server.wait(timeout=10)
            return 1
        while server.poll() is None and not interrupted:
            time.sleep(5.0)
            try:
                idle = time.time() - used_file(paths).stat().st_mtime
            except OSError:  # Nobody has touched the marker: treat as long idle.
                idle = float("inf")
            if idle > IDLE_SECONDS:
                break
        return server.returncode if server.poll() is not None else 0
    finally:
        info_file(paths).unlink(missing_ok=True)
        if server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()


def main() -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--supervise", nargs=argparse.REMAINDER, dest="command")
    args = parser.parse_args()
    if not args.command:
        parser.print_usage()
        return 2
    paths = d.Paths()
    lock = desktop.lock(paths.runtime / "engine.lock")
    if lock is None:
        return 0  # Another engine is already being kept ready.
    try:
        return supervise(paths, list(args.command))
    finally:
        os.close(lock)


if __name__ == "__main__":
    raise SystemExit(main())
