"""Desktop application operations, independent of the window toolkit."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import desktop
import dictation as d
import onboarding


class Service:
    def __init__(self, paths: d.Paths) -> None:
        self.paths = paths
        self.marker = paths.config.parent / "welcome.json"

    def ready(self) -> bool:
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

    def prepare(self, language: str, device: str, model: str) -> None:
        if d.busy(self.paths):
            raise d.DictationError("Finish recording before changing setup.")
        if language not in ("en", "auto") or not device.strip():
            raise d.DictationError("Choose a language and microphone first.")
        if desktop.platform_name() == "windows" and device == "default":
            raise d.DictationError("Choose a microphone using Find microphones.")
        candidate = d.Config(self.paths)
        path = onboarding.validate_model(
            Path(model).expanduser()
            if model
            else onboarding.download_model(self.paths.config.parent / "models", language)
        )
        candidate.values.update(
            backend="local",
            model=str(path.resolve()),
            language=language,
            device=device.strip(),
            allow_remote=False,
        )
        candidate.check(recording=True)
        d.private_dir(self.paths.config.parent)
        # Save the user's file settings, not temporary DICTATION_* environment
        # overrides inherited by this desktop process.
        saved = d.DEFAULTS | d.read_json(self.paths.config)
        for key in ("ffmpeg", "whisper_bin"):
            if not saved[key]:
                saved[key] = candidate.values[key]
        saved.update(
            backend="local",
            model=str(path),
            language=language,
            device=device.strip(),
            allow_remote=False,
        )
        d.atomic(self.paths.config, json.dumps(saved, indent=2))

    def action(self, action: str) -> None:
        config = d.Config(self.paths)
        if action == "copy":
            d.copy_text(config, self.paths)
        else:
            d.dispatch(config, self.paths, action)
