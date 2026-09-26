"""Local-first dictation with Linux, macOS and Windows desktop adapters."""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import http.client
import io
import json
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

import desktop
from desktop import lock


class DictationError(Exception):
    """An actionable error safe to display without credentials or transcripts."""


def atomic(path: Path, text: str) -> None:
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".dictation-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
        for attempt in range(20):
            try:
                os.replace(name, path)
                break
            except PermissionError:
                # Windows readers can briefly hold a non-delete-sharing handle.
                if sys.platform != "win32" or attempt == 19:
                    raise
                time.sleep(0.01)
    finally:
        Path(name).unlink(missing_ok=True)


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
        self.cache = private_dir(cache_root)
        self.runtime = private_dir(runtime_root)
        self.state = self.runtime / "state.json"
        self.control = self.runtime / "control.json"
        self.audio = self.cache / "dictation.pcm"
        self.text = self.cache / "last.txt"
        self.preview = self.cache / "preview.txt"


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError) as exc:
        raise DictationError(
            f"Cannot read {path.name}; check its JSON syntax and permissions."
        ) from exc
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
    "threads": 4,
    "prompt": "",
    "endpoint": "",
    "api_model": "",
    "api_key_env": "DICTATION_API_KEY",
    "allow_remote": False,
    "live": False,
    "live_interval": 5,
    "live_window": 30,
    "timeout": 120,
    "max_seconds": 300,
    "keep_audio": False,
    "voice_commands": True,
    "vad_model": "",
    "preview_notifications": False,
    "rewrite_endpoint": "",
    "rewrite_model": "",
    "rewrite_api_key_env": "DICTATION_REWRITE_API_KEY",
    "rewrite_allow_remote": False,
}
ENV_NAMES = {"threads": "WHISPER_THREADS"}


class Config:
    def __init__(self, paths: Paths) -> None:
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
            ("threads", 1, 128),
            ("max_seconds", 1, 600),
            ("timeout", 1, 600),
            ("live_interval", 1, 60),
            ("live_window", 5, 60),
        ):
            if not low <= self.values[key] <= high:
                raise DictationError(f"{key} must be between {low} and {high}.")
        if self.s("backend") not in ("local", "http"):
            raise DictationError("backend must be local or http.")
        if self.s("audio_backend") not in ("auto", "alsa", "avfoundation", "dshow"):
            raise DictationError("audio_backend must be auto, alsa, avfoundation, or dshow.")
        if self.s("clipboard_backend") not in ("auto", "wayland", "pbcopy", "powershell"):
            raise DictationError("clipboard_backend must be auto, wayland, pbcopy, or powershell.")
        models = Path.home() / ".local/share/whisper.cpp/models"
        if not self.s("model"):
            selected = models / "dictation-model.bin"
            self.values["model"] = str(
                selected if selected.exists() else models / "ggml-base.en.bin"
            )
        if not self.s("whisper_bin"):
            self.values["whisper_bin"] = shutil.which("whisper-cli") or str(
                Path.home() / ".local/opt/whisper.cpp/build/bin/whisper-cli"
            )

    def s(self, key: str) -> str:
        return str(self.values[key])

    def n(self, key: str) -> int:
        return int(self.values[key])

    def b(self, key: str) -> bool:
        return bool(self.values[key])

    def check(self, recording: bool = False) -> None:
        commands = [desktop.clipboard_command(self.values)[0]]
        if recording:
            commands.append(desktop.recorder_command(self.values)[0])
            if desktop.audio_backend(self.values) == "dshow" and self.s("device") == "default":
                raise DictationError("Set device to a Windows microphone name from --devices.")
        if self.s("backend") == "local":
            commands.append(self.s("whisper_bin"))
            if not Path(self.s("model")).expanduser().is_file():
                raise DictationError(
                    "Local model not found. Set model in config.json or DICTATION_MODEL."
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
        for command in commands:
            if not desktop.available(command):
                raise DictationError(f"Required executable unavailable: {Path(command).name}")


def notify(config: Config, message: str) -> None:
    command = desktop.notification_command(config.values)
    if desktop.available(command[0]):
        try:
            use_stdin = desktop.platform_name() == "windows" and not config.s("notify")
            subprocess.run(
                command if use_stdin else command + [message],
                input=message.encode("utf-8") if use_stdin else None,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
                check=False,
                **desktop.process_options(),
            )
        except (OSError, subprocess.TimeoutExpired):
            pass


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


def transcribe(config: Config, pcm: bytes, cache: Path) -> str:
    if len(pcm) < 3200 or not any(pcm):
        return ""
    if config.s("backend") == "local":
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
                    timeout=config.n("timeout"),
                    check=False,
                    **desktop.process_options(),
                )
            except subprocess.TimeoutExpired as exc:
                raise DictationError(
                    "Transcription timed out; audio is available for retry."
                ) from exc
            if result.returncode:
                raise DictationError(
                    "Whisper failed. Check model compatibility and available memory; audio is retained."
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
        if key:
            headers["Authorization"] = "Bearer " + key
        request = urllib.request.Request(config.s("endpoint"), data=bytes(body), headers=headers)
        try:
            with urllib.request.build_opener(NoRedirect()).open(
                request, timeout=config.n("timeout")
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
            "Transcript saved, but clipboard copy failed. Retry with --copy-last."
        ) from exc


def finish(config: Config, paths: Paths) -> None:
    if not paths.audio.exists():
        raise DictationError("No retained audio. Start a new recording.")
    text = transcribe(config, paths.audio.read_bytes(), paths.cache)
    if text:
        atomic(paths.text, text)
        # A new original invalidates the previous review draft and avoids
        # retaining older rewritten content after a successful transcription.
        (paths.cache / "concise.json").unlink(missing_ok=True)
        copy_text(config, paths)
        notify(
            config,
            "Transcript copied. Paste with "
            + ("Command+V." if desktop.platform_name() == "macos" else "Ctrl+V."),
        )
    else:
        notify(config, "No speech detected. Previous transcript and clipboard kept.")
    if not config.b("keep_audio"):
        paths.audio.unlink(missing_ok=True)


def worker(config: Config, paths: Paths, fd: int, token: str) -> None:
    recorder: subprocess.Popen[bytes] | None = None
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    pending: concurrent.futures.Future[str] | None = None
    started = time.monotonic()
    next_preview = started + config.n("live_interval")
    cancelled = False
    interrupted = False

    def interrupt(signum: int, frame: Any) -> None:
        nonlocal interrupted
        interrupted = True

    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGINT, interrupt)

    def state(phase: str, message: str = "") -> None:
        atomic(
            paths.state,
            json.dumps(
                {
                    "phase": phase,
                    "token": token,
                    "message": message,
                    "started_at": time.time() - (time.monotonic() - started),
                    "elapsed_seconds": round(time.monotonic() - started, 1),
                }
            ),
        )

    def stop() -> None:
        if recorder is not None and recorder.poll() is None:
            if recorder.stdin is not None:
                with contextlib.suppress(OSError):
                    recorder.stdin.write(b"q\n")
                    recorder.stdin.close()
                try:
                    recorder.wait(timeout=1)
                    return
                except subprocess.TimeoutExpired:
                    pass
            recorder.terminate()
            try:
                recorder.wait(timeout=1)
            except subprocess.TimeoutExpired:
                recorder.kill()
                recorder.wait()

    try:
        paths.preview.unlink(missing_ok=True)
        # Raw PCM has no unfinalized WAV header; snapshots are wrapped on demand.
        with paths.audio.open("wb") as output:
            recorder = subprocess.Popen(
                desktop.recorder_command(config.values),
                stdout=output,
                stdin=subprocess.PIPE
                if desktop.audio_backend(config.values) != "alsa"
                else subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                **desktop.process_options(),
            )
        time.sleep(0.08)
        if recorder.poll() is not None:
            raise DictationError(
                "Microphone could not start. Check --devices and your OS microphone permission."
            )
        state("recording")
        notify(config, "Recording. Press your shortcut again to stop.")
        while True:
            control = read_json(paths.control)
            if control.get("token") == token and control.get("action") in ("stop", "cancel"):
                cancelled = control["action"] == "cancel"
                break
            if interrupted:
                raise DictationError("Session interrupted; audio retained for --transcribe.")
            if recorder.poll() is not None:
                if recorder.returncode:
                    raise DictationError(
                        "Microphone stopped unexpectedly; audio retained for --transcribe."
                    )
                break
            if time.monotonic() - started >= config.n("max_seconds"):
                break
            if pending is not None and pending.done():
                try:
                    preview = pending.result()
                    atomic(paths.preview, preview)
                    if preview and config.b("preview_notifications"):
                        notify(config, "Draft: " + preview[-160:])
                except (DictationError, OSError):
                    state(
                        "recording", "Live preview unavailable; final transcription will still run."
                    )
                pending = None
                next_preview = time.monotonic() + config.n("live_interval")
            if config.b("live") and pending is None and time.monotonic() >= next_preview:
                with paths.audio.open("rb") as audio:
                    size = paths.audio.stat().st_size
                    audio.seek(max(0, size - config.n("live_window") * 32000) // 2 * 2)
                    pcm = audio.read(config.n("live_window") * 32000)
                pending = executor.submit(transcribe, config, pcm, paths.cache)
            time.sleep(0.05)
        stop()
        state("cancelling" if cancelled else "transcribing")
        # Do not overlap inference requests or release the session while a preview runs.
        executor.shutdown(wait=True)
        if cancelled:
            paths.audio.unlink(missing_ok=True)
            state("idle", "Recording cancelled.")
            notify(config, "Recording cancelled.")
        else:
            notify(config, "Transcribing…")
            finish(config, paths)
            state("idle")
    except (DictationError, OSError) as exc:
        message = (
            str(exc)
            if isinstance(exc, DictationError)
            else "Session failed; check device and file permissions."
        )
        state("error", message)
        notify(config, message)
    finally:
        stop()
        executor.shutdown(wait=True)
        paths.preview.unlink(missing_ok=True)
        os.close(fd)


def dispatch(config: Config, paths: Paths, action: str) -> None:
    command_fd = lock(paths.runtime / "command.lock")
    if command_fd is None:
        print("Another shortcut action is running.")
        return
    try:
        if busy(paths):
            if action == "discard":
                raise DictationError("Session is active; use --cancel while recording.")
            current = read_json(paths.state)
            if action in ("toggle", "cancel") and current.get("phase") == "recording":
                atomic(
                    paths.control,
                    json.dumps(
                        {
                            "token": current["token"],
                            "action": "cancel" if action == "cancel" else "stop",
                        }
                    ),
                )
                print("Cancelling…" if action == "cancel" else "Stopping…")
            else:
                print("Dictation is busy; audio is protected until this session finishes.")
            return
        if action == "cancel":
            print("No active recording.")
            return
        if action == "discard":
            paths.audio.unlink(missing_ok=True)
            print("Retained audio discarded. Saved transcript kept.")
            return
        config.check(recording=action in ("toggle", "start"))
        fd = lock(paths.runtime / "session.lock")
        if fd is None:
            raise DictationError("Session is busy.")
        try:
            if action == "transcribe":
                finish(config, paths)
                atomic(paths.state, json.dumps({"phase": "idle"}))
                return
            if paths.audio.exists() and paths.audio.stat().st_size:
                raise DictationError(
                    "Unfinished audio is retained. Use --transcribe to retry or --discard to remove it."
                )
            token = uuid.uuid4().hex
            atomic(paths.state, json.dumps({"phase": "starting", "token": token}))
            # Windows cannot inherit a POSIX file lock. Keep the command lock until
            # the worker has acquired its own session lock and acknowledged startup.
            os.close(fd)
            fd = None
            child = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "--worker", token],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **desktop.process_options(detached=True),
            )
            deadline = time.monotonic() + 8
            while True:
                current = read_json(paths.state)
                if current.get("token") == token and current.get("phase") != "starting":
                    break
                if child.poll() is not None:
                    raise DictationError(
                        "Session worker could not start; check your Python installation."
                    )
                if time.monotonic() >= deadline:
                    child.terminate()
                    child.wait(timeout=5)
                    raise DictationError("Session startup timed out. Try again.")
                time.sleep(0.02)
            print("Starting recording…")
        finally:
            if fd is not None:
                os.close(fd)
    finally:
        os.close(command_fd)


def main() -> int:
    os.umask(0o077)
    config: Config | None = None
    parser = argparse.ArgumentParser(
        prog="dictate-toggle",
        description="Cross-platform local dictation with optional live drafts and HTTP transcription.",
    )
    group = parser.add_mutually_exclusive_group()
    for flag in (
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
    ):
        group.add_argument("--" + flag, action="store_true")
    group.add_argument("--worker", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        paths = Paths()
        if args.setup:
            import onboarding

            onboarding.run(paths)
            return 0
        if args.init_config:
            private_dir(paths.config.parent)
            with paths.config.open("x", encoding="utf-8") as output:
                json.dump(DEFAULTS, output, indent=2)
            print(f"Settings created: {paths.config}")
            return 0
        config = Config(paths)
        if args.concise or args.review or args.copy_concise or args.setup_rewrite:
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
            return 0
        if args.worker:
            worker_fd = lock(paths.runtime / "session.lock")
            if worker_fd is None:
                raise DictationError("A session already owns the recorder.")
            worker(config, paths, worker_fd, args.worker)
        elif args.status:
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
        elif args.doctor:
            config.check(recording=True)
            print(
                "Configured executables and model/endpoint are valid. Device and endpoint access still require a session."
            )
        elif args.devices:
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
    except (DictationError, OSError, EOFError, subprocess.SubprocessError) as exc:
        if config is not None:
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


if __name__ == "__main__":
    # The walkthrough imports this module; share exception types and state when
    # this file was launched as a script rather than importing a second copy.
    sys.modules["dictation"] = sys.modules[__name__]
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
