"""Desktop application operations, independent of the window toolkit."""

from __future__ import annotations

import json
import re
import subprocess
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import clipboardplus
import desktop
import dictation as d
import hotkeys
import onboarding

# Speech-to-text services with an OpenAI-compatible /audio/transcriptions API.
PROVIDERS = {
    "OpenAI": ("https://api.openai.com/v1/audio/transcriptions", "gpt-4o-mini-transcribe"),
    "Groq": ("https://api.groq.com/openai/v1/audio/transcriptions", "whisper-large-v3-turbo"),
    "Other (OpenAI-compatible)": ("", ""),
}


@dataclass(frozen=True)
class Remote:
    endpoint: str
    model: str
    key: str


class Service:
    def __init__(self, paths: d.Paths) -> None:
        self.paths = paths
        self.marker = paths.config.parent / "welcome.json"

    def ready(self) -> bool:
        """Whether the chosen features can be used; dictation needs a model and microphone."""
        if not hotkeys.Preferences(self.paths).features().dictation:
            return True
        try:
            d.Config(self.paths).check(recording=True)
            return True
        except d.DictationError:
            return False

    def completed(self) -> bool:
        return d.read_json(self.marker).get("complete") is True and self.ready()

    def complete(self) -> None:
        d.private_dir(self.marker.parent)
        d.atomic(self.marker, json.dumps({"complete": True}))

    def clipboard_plus_linked(self) -> bool:
        return clipboardplus.linked(self.paths.config.parent)

    def connect_clipboard_plus(self, key: str) -> None:
        """Link a Clipboard+ account. The key is saved only once the service accepts it."""
        try:
            cleaned = clipboardplus.clean_key(key)
        except ValueError as exc:
            raise d.DictationError(str(exc)) from exc
        result = clipboardplus.verify(cleaned)
        if result == "offline":
            raise d.DictationError(
                "Couldn’t reach Clipboard+. Check your internet connection and try again."
            )
        if result == "read-only":
            raise d.DictationError(
                "That key can’t save to Clipboard+. Generate one with clipboard write access."
            )
        if result == "invalid":
            raise d.DictationError(
                "Clipboard+ didn’t accept that key. Copy a fresh key from your account and try again."
            )
        if result != "ok":
            raise d.DictationError("Clipboard+ is having trouble right now. Try again shortly.")
        try:
            d.private_dir(self.paths.config.parent)
            clipboardplus.save_key(self.paths.config.parent, cleaned)
        except OSError as exc:
            raise d.DictationError(
                "Couldn’t save the Clipboard+ key. Check that your settings folder is writable."
            ) from exc

    def open_clipboard_history(self) -> None:
        """The user's history once linked; otherwise the Clipboard+ site."""
        import webbrowser

        webbrowser.open(
            clipboardplus.DASHBOARD_URL if self.clipboard_plus_linked() else clipboardplus.SITE
        )

    def disconnect_clipboard_plus(self) -> None:
        clipboardplus.remove_key(self.paths.config.parent)

    def microphones(self) -> list[str]:
        config = d.Config(self.paths)
        result = subprocess.run(
            desktop.recorder_command(config.values, listing=True),
            capture_output=True,
            timeout=15,
            check=False,
            **desktop.process_options(),
        )
        output = (result.stdout + result.stderr).decode("utf-8", errors="replace")
        backend = desktop.audio_backend(config.values)
        if backend == "alsa" and result.returncode != 0:
            raise d.DictationError(
                "Microphone discovery failed. Check audio permissions and your PipeWire/ALSA setup, then try again."
            )
        if backend == "alsa":
            devices = [
                line.strip() for line in output.splitlines() if line and not line[0].isspace()
            ]
        elif backend == "dshow":
            devices = re.findall(r'"([^"\n]+)"\s+\(audio\)', output)
        else:
            audio = output.partition("AVFoundation audio devices:")[2]
            devices = re.findall(r"\[\d+\]\s+(.+)", audio)
        if not devices:
            raise d.DictationError(
                "No microphones found. Connect a microphone and allow microphone access in system privacy settings, then try again."
            )
        return list(dict.fromkeys(devices))

    def prepare(
        self,
        language: str,
        device: str,
        model: str,
        progress: Callable[[int, int], None] | None = None,
        remote: Remote | None = None,
    ) -> None:
        if d.busy(self.paths):
            raise d.DictationError("Finish recording before changing setup.")
        if language not in ("en", "auto") or not device.strip():
            raise d.DictationError("Choose a language and microphone first.")
        if desktop.platform_name() == "windows" and device == "default":
            raise d.DictationError("Choose a microphone using Find microphones.")
        candidate = d.Config(self.paths)
        key_file = d.key_file(self.paths)
        settings: dict[str, object]
        if remote is not None:
            host = urllib.parse.urlsplit(remote.endpoint).hostname
            loopback = host in ("localhost", "127.0.0.1", "::1")
            if not remote.key and not key_file.is_file() and not loopback:
                raise d.DictationError("Enter the API key for your transcription service.")
            # Choosing a service is the explicit consent to send audio to it.
            settings = dict(
                backend="http",
                endpoint=remote.endpoint.strip(),
                api_model=remote.model.strip(),
                allow_remote=not loopback,
            )
        else:
            path = onboarding.validate_model(
                Path(model).expanduser()
                if model
                else onboarding.download_model(
                    self.paths.config.parent / "models", language, progress
                )
            )
            settings = dict(backend="local", model=str(path.resolve()), allow_remote=False)
        candidate.values.update(language=language, device=device.strip(), **settings)
        candidate.check(recording=True)
        d.private_dir(self.paths.config.parent)
        if remote is not None and remote.key:
            d.atomic(key_file, remote.key.strip())  # atomic() creates owner-only files.
        elif remote is None:
            key_file.unlink(missing_ok=True)
        # Save the user's file settings, not temporary DICTATION_* environment
        # overrides inherited by this desktop process.
        saved = d.DEFAULTS | d.read_json(self.paths.config)
        for key in ("ffmpeg", "whisper_bin"):
            if not saved[key]:
                saved[key] = candidate.values[key]
        saved.update(language=language, device=device.strip(), **settings)
        d.atomic(self.paths.config, json.dumps(saved, indent=2))

    def action(self, action: str) -> None:
        config = d.Config(self.paths)
        if action == "copy":
            d.copy_text(config, self.paths)
        else:
            d.dispatch(config, self.paths, action)
