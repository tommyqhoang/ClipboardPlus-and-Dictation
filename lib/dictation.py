"""Local-first dictation with Linux, macOS and Windows desktop adapters."""

from __future__ import annotations

import argparse
import array
import concurrent.futures
import contextlib
import http.client
import io
import json
import logging
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import wave
from pathlib import Path
from typing import Any

import cues
import desktop
import permissions
import telemetry
from desktop import lock

try:  # The rotating file log, when this install has it.
    import logsetup

    log = logsetup.get_logger("dictation")
except ImportError:
    log = logging.getLogger(__name__)


class DictationError(Exception):
    """An actionable error safe to display without credentials or transcripts."""


def _fsync_dir(folder: Path) -> None:
    """Make a rename durable: flush the directory entry (POSIX; Windows has no such call)."""
    if sys.platform == "win32":
        return
    try:
        fd = os.open(folder, os.O_RDONLY)
    except OSError as exc:
        log.debug("cannot open %s to sync it: %s", folder, exc)
        return
    try:
        os.fsync(fd)
    except OSError as exc:  # Some file systems refuse it; the data is still written.
        log.debug("cannot sync %s: %s", folder, exc)
    finally:
        os.close(fd)


def atomic(path: Path, text: str) -> None:
    """Write `text` to `path` so a reader sees the old or the new file, never half of one.

    The data is flushed to disk before the rename and the directory entry after it, so a
    power cut cannot leave an empty file behind. The temp file is removed only when the
    write fails (a successful rename has already consumed it).
    """
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".dictation-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(20):
            try:
                os.replace(name, path)
                break
            except PermissionError:
                # Windows readers can briefly hold a non-delete-sharing handle.
                if sys.platform != "win32" or attempt == 19:
                    raise
                time.sleep(0.01)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise
    _fsync_dir(path.parent)


def private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or (sys.platform != "win32" and path.stat().st_uid != os.getuid()):
        raise DictationError("The dictation directory must be owned by you and not a symlink.")
    if sys.platform != "win32":
        path.chmod(0o700)
    return path


class Paths:
    def __init__(self) -> None:
        config_root, cache_root, runtime_root = desktop.roots()
        self.config = config_root / "config.json"
        self.clipboard = config_root / "clipboard"
        self.cache = private_dir(cache_root)
        self.runtime = private_dir(runtime_root)
        self.state = self.runtime / "state.json"
        self.control = self.runtime / "control.json"
        self.audio = self.cache / "dictation.pcm"
        self.text = self.cache / "last.txt"
        self.preview = self.cache / "preview.txt"


def read_json(path: Path) -> dict[str, Any]:
    for attempt in range(20):
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
            break
        except FileNotFoundError:
            return {}
        except PermissionError as exc:
            # Windows can briefly deny a read while another process replaces the
            # state file. Give the atomic writer time to finish before reporting it.
            if sys.platform != "win32" or attempt == 19:
                raise DictationError(
                    f"Cannot read {path.name}: permission denied. Check that you own {path} "
                    "and can read it."
                ) from exc
            time.sleep(0.01)
        except json.JSONDecodeError as exc:
            raise DictationError(
                f"{path.name} isn’t valid JSON (line {exc.lineno}, column {exc.colno}: "
                f"{exc.msg}). Fix that line, or delete {path} to start again."
            ) from exc
        except UnicodeDecodeError as exc:
            raise DictationError(
                f"{path.name} isn’t valid UTF-8 text; delete {path} to start again."
            ) from exc
        except OSError as exc:
            raise DictationError(
                f"Cannot read {path.name} ({exc.strerror or exc.__class__.__name__})."
            ) from exc
    else:
        raise AssertionError("Read retries did not finish")
    if not isinstance(value, dict):
        raise DictationError(f"{path.name} must contain a JSON object.")
    return value


DEFAULTS: dict[str, Any] = {
    "backend": "local",
    "model": "",
    "language": "en",
    "device": "default",
    "whisper_bin": "",
    "arecord": "arecord",
    "wl_copy": "wl-copy",
    "notify": "",
    "audio_backend": "auto",
    "clipboard_backend": "auto",
    "ffmpeg": "ffmpeg",
    "powershell": "powershell.exe",
    # 0 means automatic: half the cores, capped, so small and large machines both fit.
    "threads": 0,
    "prompt": "",
    "endpoint": "",
    "api_model": "",
    "api_key_env": "DICTATION_API_KEY",
    "allow_remote": False,
    "live": True,
    "live_interval": 5,
    "live_window": 30,
    "timeout": 120,
    "max_seconds": 300,
    "keep_audio": False,
    "voice_commands": True,
    "vad_model": "",
    "preview_notifications": False,
    "overlay": True,
    "notifications": False,  # Session messages as desktop notifications (the pill shows them).
    "auto_paste": True,
    "sounds": "errors",  # Audible cues: "off", "errors", or "all" (start, stop and error).
    "rewrite_endpoint": "",
    "rewrite_model": "",
    "rewrite_api_key_env": "DICTATION_REWRITE_API_KEY",
    "rewrite_allow_remote": False,
}
ENV_NAMES = {"threads": "WHISPER_THREADS"}


def key_file(paths: Paths) -> Path:
    """API key saved by the desktop setup (owner-only); the environment wins."""
    return paths.config.parent / "transcription-key"


class Config:
    def __init__(self, paths: Paths) -> None:
        self.paths = paths
        raw = read_json(paths.config)
        unknown = raw.keys() - DEFAULTS.keys()
        if unknown:
            raise DictationError("Unknown configuration keys: " + ", ".join(sorted(unknown)))
        self.values = DEFAULTS | raw
        for key, default in DEFAULTS.items():
            env = os.environ.get("DICTATION_" + ENV_NAMES.get(key, key.upper()))
            if env is not None:
                if isinstance(default, bool):
                    if env.lower() not in ("0", "1", "true", "false"):
                        raise DictationError(f"{key} must be true or false.")
                    self.values[key] = env.lower() in ("1", "true")
                elif isinstance(default, int):
                    try:
                        self.values[key] = int(env)
                    except ValueError as exc:
                        raise DictationError(f"{key} must be an integer.") from exc
                else:
                    self.values[key] = env
            if type(self.values[key]) is not type(default):
                raise DictationError(f"Invalid type for configuration key: {key}")
        for key, low, high in (
            ("threads", 0, 128),  # 0: chosen from the machine's core count.
            ("max_seconds", 1, 600),
            ("timeout", 1, 600),
            ("live_interval", 1, 60),
            ("live_window", 5, 60),
        ):
            if not low <= self.values[key] <= high:
                raise DictationError(f"{key} must be between {low} and {high}.")
        if self.s("sounds") not in cues.MODES:
            raise DictationError("sounds must be off, errors, or all.")
        if self.s("backend") not in ("local", "http"):
            raise DictationError("backend must be local or http.")
        if self.s("audio_backend") not in ("auto", "alsa", "avfoundation", "dshow"):
            raise DictationError("audio_backend must be auto, alsa, avfoundation, or dshow.")
        if self.s("clipboard_backend") not in ("auto", "wayland", "x11", "pbcopy", "powershell"):
            raise DictationError(
                "clipboard_backend must be auto, wayland, x11, pbcopy, or powershell."
            )
        models = Path.home() / ".local/share/whisper.cpp/models"
        if not self.s("model"):
            selected = models / "dictation-model.bin"
            self.values["model"] = str(
                selected if selected.exists() else models / "ggml-base.en.bin"
            )
        if not self.s("whisper_bin"):
            bundled = desktop.bundled_binary("whisper-cli")
            self.values["whisper_bin"] = (
                str(bundled)
                if bundled is not None
                else (
                    shutil.which("whisper-cli")
                    or str(Path.home() / ".local/opt/whisper.cpp-v1.8.7/build/bin/whisper-cli")
                )
            )

    def s(self, key: str) -> str:
        return str(self.values[key])

    def n(self, key: str) -> int:
        if key == "threads" and not self.values[key]:
            # Automatic: half the cores (whisper also parallelises encode/decode),
            # always at least one and never more than eight, leaving the machine
            # responsive while a recording is transcribed.
            cores = os.cpu_count() or 4
            return max(1, min(8, cores // 2))
        return int(self.values[key])

    def b(self, key: str) -> bool:
        return bool(self.values[key])

    def check(self, recording: bool = False) -> None:
        commands = [desktop.clipboard_command(self.values)[0]]
        if recording:
            commands.append(desktop.recorder_command(self.values)[0])
            if desktop.audio_backend(self.values) == "dshow" and self.s("device") == "default":
                raise DictationError("Choose your microphone in Clipboard+: Settings, Microphone.")
        if self.s("backend") == "local":
            commands.append(self.s("whisper_bin"))
            if not Path(self.s("model")).expanduser().is_file():
                raise DictationError(
                    "Dictation isn’t set up yet: the speech model is missing. "
                    "Open Clipboard+ to finish setup."
                )
            if self.s("vad_model") and not Path(self.s("vad_model")).expanduser().is_file():
                raise DictationError("VAD model not found.")
        else:
            try:
                endpoint = urllib.parse.urlsplit(self.s("endpoint"))
                _ = endpoint.port
            except ValueError as exc:
                raise DictationError("The transcription endpoint URL is malformed.") from exc
            loopback = endpoint.hostname in ("localhost", "127.0.0.1", "::1")
            if endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
                raise DictationError(
                    "Use a plain endpoint URL; put credentials in the API key environment variable."
                )
            if not endpoint.hostname or endpoint.scheme not in ("http", "https"):
                raise DictationError("Set a complete HTTP transcription endpoint.")
            if not loopback and (endpoint.scheme != "https" or not self.b("allow_remote")):
                raise DictationError(
                    "Remote transcription requires HTTPS and allow_remote=true; audio leaves this machine."
                )
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.s("api_key_env")):
                raise DictationError(
                    "api_key_env must be an environment variable NAME, not an API key."
                )
        for command in commands:
            if not desktop.available(command):
                raise DictationError(f"Required executable unavailable: {Path(command).name}")


def notify(config: Config | None, message: str, *, session: bool = False) -> None:
    """A desktop notification (with default settings when they could not be read).

    A `session` message repeats what the recording pill already shows, so it is sent
    only when the user wants notifications too, or has turned the pill off. Errors and
    messages with no pill on screen are always sent.
    """
    values = config.values if config is not None else DEFAULTS
    if session and values["overlay"] and not values["notifications"]:
        return
    command = desktop.notification_command(values)
    if desktop.available(command[0]):
        try:
            use_stdin = desktop.platform_name() == "windows" and not values["notify"]
            subprocess.run(
                command if use_stdin else command + [message],
                input=message.encode("utf-8") if use_stdin else None,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
                check=False,
                **desktop.process_options(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.warning("notification failed: %s", exc)
    else:
        log.warning("no notification tool (%s); message not shown: %s", command[0], message)


def clean_text(text: str, commands: bool = True) -> str:
    # Only bracketed non-speech tokens: the spoken word "music" is valid dictation.
    text = re.sub(
        r"(?im)^\s*[\[(](?:music(?: playing)?|blank_audio|silence|no speech|applause)[\])]\s*$",
        "",
        text,
    )
    text = re.sub(r"\s+", " ", text).strip()
    if commands:
        text = re.sub(r"\s*\bnew paragraph\b[.,]?\s*", "\n\n", text, flags=re.I)
        text = re.sub(r"\s*\bnew line\b[.,]?\s*", "\n", text, flags=re.I)
    return text.strip()


_QUIET_DB, _LOUD_DB = -55.0, -14.0


def audio_level(pcm: bytes) -> float:
    """Loudness of 16-bit little-endian PCM from 0 (silence) to 1 (speaking up)."""
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) // 2 * 2])
    if not samples:
        return 0.0
    if sys.byteorder == "big":
        samples.byteswap()
    rms = math.sqrt(sum(s * s for s in samples) / len(samples))
    if rms < 1:
        return 0.0
    decibels = 20 * math.log10(rms / 32768)
    return min(1.0, max(0.0, (decibels - _QUIET_DB) / (_LOUD_DB - _QUIET_DB)))


def wav_bytes(pcm: bytes) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(pcm[: len(pcm) // 2 * 2])
    return out.getvalue()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


def transcribe(config: Config, pcm: bytes, cache: Path, *, timeout: int | None = None) -> str:
    timeout = config.n("timeout") if timeout is None else timeout
    if len(pcm) < 3200 or not any(pcm):
        return ""
    if config.s("backend") == "local":
        import engine

        # A ready engine (model already in memory) answers far faster than a fresh
        # whisper-cli start; any problem falls back to the usual path below.
        text = engine.transcribe(config, config.paths, wav_bytes(pcm), timeout)
        if text is not None:
            return clean_text(text, config.b("voice_commands"))
        fallback_note = ""
        if engine.last_failure is not None and engine.last_failure.kind in (
            engine.Failure.TIMEOUT,
            engine.Failure.HTTP_ERROR,
            engine.Failure.MODEL_MISSING,
        ):
            fallback_note = " " + str(engine.last_failure)
        with tempfile.TemporaryDirectory(prefix="inference-", dir=cache) as folder:
            audio = Path(folder) / "audio.wav"
            audio.write_bytes(wav_bytes(pcm))
            args = desktop.executable(config.s("whisper_bin")) + [
                "-m",
                str(Path(config.s("model")).expanduser()),
                "-f",
                str(audio),
                "-l",
                config.s("language"),
                "-nt",
                "-np",
                "-t",
                str(config.n("threads")),
            ]
            if config.s("prompt"):
                args += ["--prompt", config.s("prompt")]
            if config.s("vad_model"):
                args += ["--vad", "--vad-model", str(Path(config.s("vad_model")).expanduser())]
            try:
                result = subprocess.run(
                    args,
                    capture_output=True,
                    timeout=timeout,
                    check=False,
                    **desktop.process_options(),
                )
            except subprocess.TimeoutExpired as exc:
                raise DictationError(
                    "Transcription timed out; audio is available for retry." + fallback_note
                ) from exc
            if result.returncode:
                log.warning(
                    "whisper-cli exited %s: %s",
                    result.returncode,
                    permissions.tail_of(result.stderr),
                )
                raise DictationError(
                    "Whisper failed. Check model compatibility and available memory; audio is retained."
                    + fallback_note
                )
            text = result.stdout.decode("utf-8", errors="replace")
    else:
        boundary = uuid.uuid4().hex
        fields = {"response_format": "json"}
        if config.s("api_model"):
            fields["model"] = config.s("api_model")
        if config.s("language") != "auto":
            fields["language"] = config.s("language")
        if config.s("prompt"):
            fields["prompt"] = config.s("prompt")
        body = bytearray()
        for name, value in fields.items():
            body.extend(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
            )
        body.extend(
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="audio.wav"\r\nContent-Type: audio/wav\r\n\r\n'.encode()
        )
        body.extend(wav_bytes(pcm))
        body.extend(f"\r\n--{boundary}--\r\n".encode())
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
        key = os.environ.get(config.s("api_key_env"), "")
        if not key and key_file(config.paths).is_file():
            key = key_file(config.paths).read_text(encoding="utf-8").strip()
        if key:
            headers["Authorization"] = "Bearer " + key
        request = urllib.request.Request(config.s("endpoint"), data=bytes(body), headers=headers)
        try:
            with urllib.request.build_opener(NoRedirect()).open(
                request, timeout=timeout
            ) as response:
                data = response.read(1024 * 1024 + 1)
            if len(data) > 1024 * 1024:
                raise DictationError("Transcription response exceeded the size limit.")
            parsed = json.loads(data)
            if not isinstance(parsed, dict) or not isinstance(parsed.get("text"), str):
                raise DictationError("Transcription endpoint must return a JSON object with text.")
            text = parsed["text"]
        except urllib.error.HTTPError as exc:
            exc.close()
            raise DictationError(
                f"Transcription endpoint returned HTTP {exc.code}; audio retained. No automatic billed retries."
            ) from exc
        except (urllib.error.URLError, TimeoutError, ValueError, http.client.HTTPException) as exc:
            raise DictationError(
                "Transcription endpoint unavailable or returned invalid JSON; audio retained."
            ) from exc
    return clean_text(text, config.b("voice_commands"))


def busy(paths: Paths) -> bool:
    fd = lock(paths.runtime / "session.lock")
    if fd is None:
        return True
    os.close(fd)
    return False


def copy_text(config: Config, paths: Paths, text: str | None = None) -> None:
    if text is None and not paths.text.exists():
        raise DictationError("No saved transcript to copy.")
    try:
        subprocess.run(
            desktop.clipboard_command(config.values),
            input=paths.text.read_bytes() if text is None else text.encode("utf-8"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=True,
            **desktop.process_options(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise DictationError(
            "Transcript saved, but clipboard copy failed. Retry with Copy transcript."
            if text is None
            else "Couldn’t put that on the clipboard. Check that "
            + Path(desktop.clipboard_command(config.values)[0]).name
            + " is installed."
        ) from exc


def record_transcript(paths: Paths, text: str) -> None:
    """Keep the transcript in the clipboard history; it never affects the dictation."""
    try:
        import clipservice
    except ImportError:  # An older partial install: dictation still works.
        return
    clipservice.record_transcript(paths, text)


# Why the last paste_text() failed ("" when it worked or was never tried).
paste_failure = ""


def paste_command(config: Config) -> list[str]:
    """The key-press helper for this operating system and session."""
    platform = desktop.platform_name()
    if platform == "macos":
        return [
            "osascript",
            "-e",
            'tell application "System Events" to keystroke "v" using command down',
        ]
    if platform == "windows":
        return [
            config.s("powershell"),
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.SendKeys]::SendWait('^v')",
        ]
    if os.environ.get("WAYLAND_DISPLAY"):
        # Never fall back to X11 input injection in a Wayland session: the
        # focused native window might differ from XWayland's focused window.
        return ["wtype", "-M", "ctrl", "-k", "v", "-m", "ctrl"]
    return ["xdotool", "key", "--clearmodifiers", "ctrl+v"]


def paste_text(config: Config) -> bool:
    """Best-effort paste into the focused app; clipboard remains the fallback.

    On failure the reason (the helper's own error output, or that it is missing) is left
    in `paste_failure` and logged, so the caller can say what to fix.
    """
    global paste_failure
    paste_failure = ""
    command = paste_command(config)
    if desktop.platform_name() == "macos" and permissions.accessibility_trusted() is False:
        paste_failure = "Accessibility access is off"
        log.warning("auto-paste skipped: %s", paste_failure)
        return False
    try:
        subprocess.run(
            command,
            check=True,
            timeout=3,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            **desktop.process_options(),
        )
        return True
    except FileNotFoundError:
        paste_failure = f"{Path(command[0]).name} is not installed"
    except subprocess.CalledProcessError as exc:
        paste_failure = permissions.tail_of(exc.stderr) or f"exit status {exc.returncode}"
    except subprocess.TimeoutExpired:
        paste_failure = f"{Path(command[0]).name} timed out"
    except (OSError, subprocess.SubprocessError) as exc:
        paste_failure = str(exc) or exc.__class__.__name__
    log.warning("auto-paste failed: %s", paste_failure)
    return False


def explain_accessibility_once(config: Config, paths: Paths) -> None:
    """On macOS, say why Accessibility is needed before the first paste that lacks it."""
    if desktop.platform_name() != "macos" or permissions.accessibility_trusted() is not False:
        return
    marker = paths.cache / "accessibility-explained"
    if marker.exists():
        return
    notify(config, permissions.accessibility_explanation())
    try:
        marker.write_text("1", encoding="utf-8")
    except OSError as exc:
        log.debug("could not remember the Accessibility explanation: %s", exc)


def paste_failed_notice() -> str:
    """The actionable "copied, but couldn't paste" message for this system."""
    reason = paste_failure
    # A missing helper is explained by the install advice; don't say it twice.
    if reason.endswith("is not installed"):
        reason = ""
    return permissions.paste_failure_message(reason=reason)


PASTED = "Pasted into your app. It’s on the clipboard too."


def finish(config: Config, paths: Paths, *, auto_paste: bool = False) -> str:
    """Transcribe the retained audio: "pasted", "copied", or "empty" when nothing was said."""
    if not paths.audio.exists():
        raise DictationError("No retained audio. Start a new recording.")
    text = transcribe(config, paths.audio.read_bytes(), paths.cache)
    pasted = False
    if text:
        atomic(paths.text, text)
        # A new original invalidates the previous review draft and avoids
        # retaining older rewritten content after a successful transcription.
        (paths.cache / "concise.json").unlink(missing_ok=True)
        record_transcript(paths, text)
        copy_text(config, paths)
        wanted = auto_paste and config.b("auto_paste")
        if wanted:
            explain_accessibility_once(config, paths)
        pasted = wanted and paste_text(config)
        if wanted and not pasted:
            # Not a session message: the pill can't say how to fix it, so always send it.
            notify(config, paste_failed_notice())
        else:
            notify(
                config,
                "Pasted. Your transcript is also on the clipboard."
                if pasted
                else "Ready to paste: press "
                + ("Command+V." if desktop.platform_name() == "macos" else "Ctrl+V."),
                session=auto_paste,  # Only the shortcut's session has the pill.
            )
    else:
        notify(
            config,
            "No speech detected. Previous transcript and clipboard kept.",
            session=auto_paste,
        )
    if not config.b("keep_audio"):
        paths.audio.unlink(missing_ok=True)
    return ("pasted" if pasted else "copied") if text else "empty"


def model_name(config: Config) -> str:
    """The model for statistics: a standard name, or "custom" (never a file name)."""
    if config.s("backend") != "local":
        return "service"
    name = Path(config.s("model")).name
    return name if re.fullmatch(r"ggml-[\w.-]+\.bin", name) else "custom"


def open_app(config: Config) -> None:
    """Bring up the app window (it shows the saved recording with Retry and Discard).

    Like the recording pill, it is a window of its own: "overlay": false turns both off.
    """
    if not config.b("overlay"):
        return
    frozen = desktop.frozen_root() is not None
    if not frozen and not Path(__file__).resolve().with_name("app.py").is_file():
        return
    try:
        subprocess.Popen(
            desktop.relaunch("app"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **desktop.process_options(detached=True),
        )
    except OSError as exc:
        log.warning("could not open the app window: %s", exc)


def start_overlay(config: Config, token: str) -> subprocess.Popen[bytes] | None:
    """Launch the floating recording pill for this session, when wanted and present."""
    if not config.b("overlay"):
        return None
    frozen = desktop.frozen_root() is not None
    if not frozen and not Path(__file__).resolve().with_name("overlay.py").is_file():
        return None
    try:
        return subprocess.Popen(
            desktop.relaunch("overlay", token),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            **desktop.process_options(),
        )
    except OSError as exc:
        log.warning("could not start the recording pill: %s", exc)
        return None


def stop_recorder(recorder: subprocess.Popen[bytes] | None) -> None:
    """Stop a recorder even when its device disappears during shutdown."""
    if recorder is None or recorder.poll() is not None:
        return
    if recorder.stdin is not None:
        try:
            recorder.stdin.write(b"q\n")
            recorder.stdin.close()
        except OSError as exc:  # The device is already gone.
            log.debug("recorder did not take the quit request: %s", exc)
        try:
            recorder.wait(timeout=1)
            return
        except subprocess.TimeoutExpired:
            pass
    try:
        recorder.terminate()
    except OSError as exc:
        log.debug("recorder terminate failed: %s", exc)
    try:
        recorder.wait(timeout=1)
    except subprocess.TimeoutExpired:
        try:
            recorder.kill()
        except OSError as exc:
            log.debug("recorder kill failed: %s", exc)
        with contextlib.suppress(subprocess.TimeoutExpired):
            recorder.wait(timeout=2)


class Session:
    """One recording, from the shortcut's first press to the transcript.

    `worker` runs it in the background process the shortcut starts. The steps are
    methods so each can be read (and tested) alone.
    """

    def __init__(self, config: Config, paths: Paths, fd: int, token: str) -> None:
        self.config = config
        self.paths = paths
        self.fd = fd
        self.token = token
        self.recorder: subprocess.Popen[bytes] | None = None
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self.pending: concurrent.futures.Future[str] | None = None
        self.started = time.monotonic()
        self.next_preview = self.started + config.n("live_interval")
        self.cancelled = False
        self.interrupted = False
        self.warned = False
        self.recorder_errors = paths.cache / "recorder.err"
        self.pill: subprocess.Popen[bytes] | None = None
        self.pill_failed = False  # The pill could not open: notifications carry the story.

    def tell(self, message: str) -> None:
        """A session message: the pill shows it, so a notification only when it can't."""
        notify(self.config, message, session=not self.pill_failed)

    def cue(self, kind: str) -> None:
        """An audible cue, unless the pill (which plays them itself) is on screen."""
        if self.pill is None or self.pill_failed:
            cues.play(kind, self.config.s("sounds"))

    def check_pill(self) -> None:
        """Notice a pill that exited early (no display, unsupported desktop)."""
        if self.pill is None or self.pill_failed or self.pill.poll() in (None, 0):
            return
        self.pill_failed = True
        log.warning("recording pill exited with %s; using notifications", self.pill.returncode)
        self.tell("Recording. Press your shortcut again to stop.")
        self.cue("start")

    def interrupt(self, signum: int, frame: Any) -> None:
        self.interrupted = True

    def install_signals(self) -> None:
        signal.signal(signal.SIGTERM, self.interrupt)
        signal.signal(signal.SIGINT, self.interrupt)

    def state(self, phase: str, message: str = "", result: str = "") -> None:
        atomic(
            self.paths.state,
            json.dumps(
                {
                    "phase": phase,
                    "token": self.token,
                    "message": message,
                    "result": result,
                    "started_at": time.time() - (time.monotonic() - self.started),
                    "elapsed_seconds": round(time.monotonic() - self.started, 1),
                    "max_seconds": self.config.n("max_seconds"),
                }
            ),
        )

    def recorder_reason(self) -> str:
        """What the recording tool said when it stopped (its last lines of error output)."""
        try:
            return permissions.tail_of(self.recorder_errors.read_bytes())
        except OSError:
            return ""

    def launch_recorder(self, device: str) -> None:
        config, paths = self.config, self.paths
        # Raw PCM has no unfinalized WAV header; snapshots are wrapped on demand.
        with paths.audio.open("wb") as output, self.recorder_errors.open("wb") as errors:
            self.recorder = subprocess.Popen(
                desktop.recorder_command(config.values, device=device),
                stdout=output,
                stdin=subprocess.PIPE
                if desktop.audio_backend(config.values) != "alsa"
                else subprocess.DEVNULL,
                stderr=errors,
                close_fds=True,
                **desktop.process_options(),
            )

    def start_recorder(self) -> None:
        """Start the microphone (and the pill and engine) and confirm it is recording."""
        config, paths = self.config, self.paths
        blocked = permissions.microphone_blocked()
        if blocked:
            raise DictationError(blocked)
        paths.preview.unlink(missing_ok=True)
        # A configured microphone that is unplugged (or a new one the default has not picked up
        # yet) falls back to the system default; only when that fails too is it an error.
        chosen = config.s("device")
        reason = ""
        for device in fallback_devices(config, chosen):
            self.launch_recorder(device)
            time.sleep(0.08)
            if self.recorder.poll() is None:
                if device != chosen:
                    log.warning("microphone %r unavailable; using %r", chosen, device)
                    self.tell("Your microphone wasn’t available, so the system default is in use.")
                break
            reason = self.recorder_reason()
            log.warning("recorder %r exited at start: %s", device, reason or "no output")
            if self.recorder.stdin:
                self.recorder.stdin.close()
        else:
            raise DictationError(permissions.microphone_message(reason=reason))
        # The pill adds to the notifications (a notification is never missed).
        self.pill = start_overlay(config, self.token)  # Starts up while the microphone does.
        if config.s("backend") == "local":
            import engine

            if engine.read_info(paths) is None:
                engine.start(paths, config)  # Loads the model while the user speaks.

    def stop_requested(self) -> bool:
        """Whether the shortcut asked to stop or cancel (which is remembered)."""
        control = read_json(self.paths.control)
        if control.get("token") == self.token and control.get("action") in ("stop", "cancel"):
            self.cancelled = control["action"] == "cancel"
            return True
        return False

    def recorder_ended(self) -> bool:
        """Whether the recording tool exited by itself; raises when it failed."""
        assert self.recorder is not None
        if self.recorder.poll() is None:
            return False
        if self.recorder.returncode:
            reason = self.recorder_reason()
            log.warning("recorder stopped with %s: %s", self.recorder.returncode, reason)
            raise DictationError(
                "The microphone stopped"
                + (f" ({reason})" if reason else "")
                + ". Your audio is saved: open Clipboard+ and choose Retry, "
                "or run dictate-toggle --transcribe."
            )
        return True

    def time_is_up(self) -> bool:
        """Warn before the recording limit; True once it is reached."""
        remaining = self.config.n("max_seconds") - (time.monotonic() - self.started)
        if remaining <= 30 and not self.warned:
            self.warned = True
            self.tell(f"Recording stops automatically in {max(1, math.ceil(remaining))} seconds.")
        return remaining <= 0

    def collect_preview(self) -> None:
        """Save a finished live draft (a failed one only costs the draft)."""
        if self.pending is None or not self.pending.done():
            return
        try:
            preview = self.pending.result()
            atomic(self.paths.preview, preview)
            if preview and self.config.b("preview_notifications"):
                self.tell("Draft: " + preview[-160:])
        except (DictationError, OSError) as exc:
            log.info("live preview failed: %s", exc)
            self.state("recording", "Live preview unavailable; final transcription will still run.")
        self.pending = None
        self.next_preview = time.monotonic() + self.config.n("live_interval")

    def submit_preview(self) -> None:
        """Start a live draft of the latest audio when one is due."""
        config, paths = self.config, self.paths
        if not (
            config.b("live") and self.pending is None and time.monotonic() >= self.next_preview
        ):
            return
        with paths.audio.open("rb") as audio:
            size = paths.audio.stat().st_size
            audio.seek(max(0, size - config.n("live_window") * 32000) // 2 * 2)
            pcm = audio.read(config.n("live_window") * 32000)
        # A preview is optional. Bound it independently so Stop/Cancel cannot
        # wait for the full final-transcription timeout.
        self.pending = self.executor.submit(
            transcribe, config, pcm, paths.cache, timeout=min(5, config.n("timeout"))
        )

    def record_until_stopped(self) -> None:
        """Poll the shortcut, the recorder and the clock until the recording should end."""
        while True:
            if self.stop_requested():
                break
            if self.interrupted:
                raise DictationError(
                    "Recording interrupted. Your audio is saved: open the app to retry."
                )
            if self.recorder_ended() or self.time_is_up():
                break
            self.check_pill()
            self.collect_preview()
            self.submit_preview()
            time.sleep(0.05)

    def finalize(self) -> None:
        """Stop the recorder, then cancel or transcribe what was recorded."""
        config, paths = self.config, self.paths
        stop_recorder(self.recorder)
        loading = (
            "Loading the speech model; the first transcription can take longer."
            if config.s("backend") == "local"
            else "Transcribing…"
        )
        self.state(
            "cancelling" if self.cancelled else "transcribing", "" if self.cancelled else loading
        )
        # Do not overlap inference requests or release the session while a preview runs.
        self.executor.shutdown(wait=True)
        if self.cancelled:
            paths.audio.unlink(missing_ok=True)
            self.state("idle", "Recording cancelled.", result="cancelled")
            self.tell("Recording cancelled.")
            self.cue("stop")
            telemetry.event(
                "dictation_cancelled", wait=True, seconds=round(time.monotonic() - self.started)
            )
            return
        self.tell(loading)
        self.cue("stop")
        recorded = time.monotonic() - self.started
        began = time.monotonic()
        result = finish(config, paths, auto_paste=True)
        # The pill shows a pasted transcript as copied too, saying it was pasted.
        if result == "pasted":
            self.state("idle", PASTED, result="copied")
        else:
            self.state("idle", result=result)
        telemetry.event(
            "dictation_complete",
            wait=True,
            result=result,
            seconds=round(recorded),
            transcribe_seconds=round(time.monotonic() - began, 1),
            backend=config.s("backend"),
            model=model_name(config),
            language=config.s("language"),
            live=config.b("live"),
            overlay=config.b("overlay"),
        )

    def fail(self, exc: DictationError | OSError) -> None:
        """Record the error where the pill and app read it and send a notification.

        The notification goes out whether or not the pill is on screen: it is the one
        channel that reaches a user who is looking at another app.
        """
        config = self.config
        if isinstance(exc, DictationError):
            message = str(exc)
        else:
            log.exception("recording session failed")
            message = "Session failed; check device and file permissions."
        self.state("error", message)
        notify(config, message)
        self.cue("error")
        # Expected problems (no microphone, no model) are warnings; the rest are errors.
        telemetry.capture(
            exc,
            level="warning" if isinstance(exc, DictationError) else "error",
            wait=True,
            stage="recording",
            backend=config.s("backend"),
            model=model_name(config),
        )

    def cleanup(self) -> None:
        stop_recorder(self.recorder)
        self.executor.shutdown(wait=True)
        self.paths.preview.unlink(missing_ok=True)
        self.recorder_errors.unlink(missing_ok=True)
        os.close(self.fd)

    def run(self) -> None:
        self.install_signals()
        try:
            self.start_recorder()
            self.state("recording")
            self.tell("Recording. Press your shortcut again to stop.")
            self.cue("start")
            self.record_until_stopped()
            self.finalize()
        except (DictationError, OSError) as exc:
            self.fail(exc)
        finally:
            self.cleanup()


def worker(config: Config, paths: Paths, fd: int, token: str) -> None:
    Session(config, paths, fd, token).run()


def request_stop(paths: Paths, action: str) -> bool:
    """Ask the running recording to stop or cancel; False when none is recording."""
    current = read_json(paths.state)
    if action in ("toggle", "cancel") and current.get("phase") == "recording":
        atomic(
            paths.control,
            json.dumps(
                {"token": current["token"], "action": "cancel" if action == "cancel" else "stop"}
            ),
        )
        print("Cancelling…" if action == "cancel" else "Stopping…")
        return True
    return False


def dispatch_while_busy(config: Config, paths: Paths, action: str) -> None:
    """A session holds the recorder: stop it if asked to, otherwise explain."""
    if action == "discard":
        raise DictationError("Session is active; use --cancel while recording.")
    if request_stop(paths, action):
        return
    notify(config, "Still transcribing. Your text will be ready in a moment.", session=True)
    print("Dictation is busy; audio is protected until this session finishes.")


def wait_for_worker(paths: Paths, child: subprocess.Popen[bytes], token: str) -> None:
    """Block (up to 8 seconds) until the background worker acknowledges it started."""
    deadline = time.monotonic() + 8
    while True:
        current = read_json(paths.state)
        if current.get("token") == token and current.get("phase") != "starting":
            if current.get("phase") in ("error", "interrupted"):
                raise DictationError(
                    str(current.get("message") or "Session worker could not start.")
                )
            return
        if child.poll() is not None:
            raise DictationError("Session worker could not start; check your Python installation.")
        if time.monotonic() >= deadline:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
            raise DictationError("Session startup timed out. Try again.")
        time.sleep(0.02)


def start_worker(config: Config, paths: Paths, action: str) -> None:
    """Take the session lock and start a recording worker (or transcribe retained audio)."""
    try:
        config.check(recording=action in ("toggle", "start"))
    except DictationError:
        if action in ("toggle", "start"):
            open_app(config)  # Most often setup is unfinished: show where to finish it.
        raise
    fd = lock(paths.runtime / "session.lock")
    if fd is None:
        raise DictationError("Session is busy.")
    try:
        if action == "transcribe":
            finish(config, paths)
            atomic(paths.state, json.dumps({"phase": "idle"}))
            return
        if paths.audio.exists() and paths.audio.stat().st_size:
            if action in ("toggle", "start"):
                open_app(config)
            raise DictationError(
                "Your last recording wasn’t transcribed yet. Retry or discard it in "
                "Clipboard+, then record again. (Or run --transcribe / --discard.)"
            )
        token = uuid.uuid4().hex
        atomic(paths.state, json.dumps({"phase": "starting", "token": token}))
        # Windows cannot inherit a POSIX file lock. Keep the command lock until
        # the worker has acquired its own session lock and acknowledged startup.
        os.close(fd)
        fd = None
        child = subprocess.Popen(
            desktop.relaunch("dictation", "--worker", token),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **desktop.process_options(detached=True),
        )
        wait_for_worker(paths, child, token)
        print("Starting recording…")
    finally:
        if fd is not None:
            os.close(fd)


def dispatch(config: Config, paths: Paths, action: str) -> None:
    command_fd = lock(paths.runtime / "command.lock")
    began = paths.runtime / "command-started"
    if command_fd is None:
        print("Another shortcut action is running.")
        try:
            held = time.time() - began.stat().st_mtime
        except OSError:
            held = 0.0
        # A quick double press needs no message; a long retry or rewrite does, or the
        # shortcut just seems broken.
        if held > 2:
            notify(config, "Still finishing the last request. Try again in a moment.")
        return
    atomic(began, str(time.time()))
    try:
        if busy(paths):
            dispatch_while_busy(config, paths, action)
            return
        if action == "cancel":
            print("No active recording.")
            return
        if action == "discard":
            paths.audio.unlink(missing_ok=True)
            # The saved recording's error no longer applies.
            atomic(paths.state, json.dumps({"phase": "idle"}))
            print("Retained audio discarded. Saved transcript kept.")
            return
        start_worker(config, paths, action)
    finally:
        os.close(command_fd)


COMMAND_FLAGS = (
    "status",
    "transcribe",
    "cancel",
    "copy-last",
    "devices",
    "doctor",
    "init-config",
    "watch",
    "discard",
    "setup",
    "concise",
    "review",
    "copy-concise",
    "record",
    "setup-rewrite",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dictate-toggle",
        description="Cross-platform local dictation with optional live drafts and HTTP transcription.",
    )
    group = parser.add_mutually_exclusive_group()
    for flag in COMMAND_FLAGS:
        group.add_argument("--" + flag, action="store_true")
    group.add_argument("--worker", help=argparse.SUPPRESS)
    # Added by the desktop's keyboard shortcut (hotkeys.VIA_SHORTCUT); see acknowledge_press.
    parser.add_argument("--via-shortcut", action="store_true", help=argparse.SUPPRESS)
    setup = parser.add_argument_group(
        "setup options", "Answers for --setup; without a terminal the defaults are used."
    )
    setup.add_argument("--language", choices=("en", "auto"), help="English or multilingual")
    setup.add_argument("--model-file", metavar="PATH", help="use this GGML model, no download")
    setup.add_argument("--mic", metavar="NAME", help="microphone name or index")
    setup.add_argument(
        "--share-usage",
        action=argparse.BooleanOptionalAction,
        help="share anonymous crash reports and usage statistics (off unless you say yes)",
    )
    setup.add_argument(
        "--interactive", action="store_true", help="ask questions even when stdin is not a terminal"
    )
    setup.add_argument("--skip-mic-test", action="store_true", help="do not test the microphone")
    return parser


def print_status(config: Config, paths: Paths) -> None:
    import workflow

    current = workflow.snapshot(paths)
    print(
        json.dumps(
            {
                "phase": "idle",
                **current,
                "backend": config.s("backend"),
                "retained_audio": paths.audio.exists(),
                "live": config.b("live"),
                "platform": desktop.platform_name(),
                "config_file": str(paths.config),
            },
            indent=2,
        )
    )


def fallback_devices(config: Config, chosen: str):
    """The chosen microphone, then whatever else the system offers (found only when needed)."""
    yield chosen
    backend = desktop.audio_backend(config.values)
    if backend == "dshow":
        return  # Windows has no "default" to fall back to; the user picks one in Settings.
    tried = {chosen}
    for device in ("default", "pipewire", "pulse") if backend == "alsa" else ("default",):
        if device not in tried:
            tried.add(device)
            yield device
    if backend != "alsa":
        return
    try:  # A newly connected card the sound server has not made the default.
        listing = subprocess.run(
            desktop.recorder_command(config.values, listing=True),
            capture_output=True,
            timeout=5,
            check=False,
            **desktop.process_options(),
        ).stdout.decode("utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return
    for line in listing.splitlines():
        if line.startswith("plughw:") and line.strip() not in tried:
            tried.add(line.strip())
            yield line.strip()


def list_devices(config: Config) -> int:
    result = subprocess.run(
        desktop.recorder_command(config.values, listing=True),
        capture_output=True,
        timeout=15,
        check=False,
        **desktop.process_options(),
    )
    device_output = (result.stdout + result.stderr).decode("utf-8", errors="replace")
    print(device_output)
    # FFmpeg's device-enumeration operation intentionally returns nonzero.
    listed = "AVFoundation audio devices" in device_output or "(audio)" in device_output
    return 0 if listed else result.returncode


def run_rewriting(args: argparse.Namespace, config: Config, paths: Paths) -> None:
    import rewriting

    rewriting.run(
        config,
        paths,
        "setup"
        if args.setup_rewrite
        else "concise"
        if args.concise
        else "copy"
        if args.copy_concise
        else "review",
    )


def run_worker(config: Config, paths: Paths, token: str) -> None:
    worker_fd = lock(paths.runtime / "session.lock")
    if worker_fd is None:
        raise DictationError("A session already owns the recorder.")
    worker(config, paths, worker_fd, token)


def run_command(args: argparse.Namespace, config: Config, paths: Paths) -> int:
    """Run the command the flags name against a valid configuration."""
    if args.concise or args.review or args.copy_concise or args.setup_rewrite:
        run_rewriting(args, config, paths)
    elif args.worker:
        run_worker(config, paths, args.worker)
    elif args.status:
        print_status(config, paths)
    elif args.doctor:
        config.check(recording=True)
        print(
            "Configured executables and model/endpoint are valid. Device and endpoint access still require a session."
        )
    elif args.devices:
        return list_devices(config)
    elif args.copy_last:
        copy_text(config, paths)
    elif args.watch or args.record:
        import workflow

        if args.record:
            dispatch(config, paths, "start")
        workflow.watch(paths)
    elif args.discard:
        dispatch(config, paths, "discard")
    else:
        dispatch(
            config,
            paths,
            "cancel" if args.cancel else "transcribe" if args.transcribe else "toggle",
        )
    return 0


def report_failure(config: Config | None, exc: Exception) -> int:
    """Tell the user (notification and stderr) about an error that ended the command."""
    # A DictationError is an expected, explained condition (setup unfinished, no
    # speech…) shown to the user; only real failures are crash reports.
    if not isinstance(exc, DictationError):
        log.exception("dictation command failed")
        telemetry.capture(exc, wait=True)
    notify(
        config,
        str(exc)
        if isinstance(exc, DictationError)
        else "Dictation could not access a file or executable.",
    )
    print(
        str(exc)
        if isinstance(exc, DictationError)
        else "Could not access settings, files, or an executable.",
        file=sys.stderr,
    )
    return 1


def acknowledge_press(args: argparse.Namespace) -> None:
    """When the desktop ran this because the shortcut was pressed (GNOME, KDE, Sway and
    Hyprland run the command themselves), leave a note first thing so Settings can say
    "it works". Never gets in the way of the dictation itself."""
    if not args.via_shortcut:
        return
    try:
        import hotkeys

        hotkeys.acknowledge(Paths(), "dictation")
    except (ImportError, OSError, DictationError) as exc:
        log.debug("could not acknowledge the shortcut press: %s", exc)


def main() -> int:
    os.umask(0o077)
    args = build_parser().parse_args()
    acknowledge_press(args)
    telemetry.install("engine")
    config: Config | None = None
    try:
        paths = Paths()
        if args.setup:
            import onboarding

            onboarding.run(
                paths,
                onboarding.SetupOptions(
                    language=args.language,
                    model=args.model_file,
                    device=args.mic,
                    share_usage=args.share_usage,
                    interactive=True if args.interactive else None,
                    skip_mic_test=args.skip_mic_test,
                ),
            )
            return 0
        if args.init_config:
            private_dir(paths.config.parent)
            with paths.config.open("x", encoding="utf-8") as output:
                json.dump(DEFAULTS, output, indent=2)
            print(f"Settings created: {paths.config}")
            return 0
        config = Config(paths)
        return run_command(args, config, paths)
    except (DictationError, OSError, EOFError, subprocess.SubprocessError) as exc:
        return report_failure(config, exc)


if __name__ == "__main__":
    # The walkthrough imports this module; share exception types and state when
    # this file was launched as a script rather than importing a second copy.
    sys.modules["dictation"] = sys.modules[__name__]
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
