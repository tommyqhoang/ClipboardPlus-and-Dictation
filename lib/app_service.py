"""Desktop application operations, independent of the window toolkit."""

from __future__ import annotations

import json
import re
import socket
import sqlite3
import subprocess
import threading
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import clipboardplus
import clipservice
import clipstore
import clipsync
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


class MicrophoneTest:
    """Listen to one microphone for a few seconds to show a level meter. Nothing is kept."""

    CHUNK = 1600  # 50 ms of 16 kHz 16-bit mono audio.

    def __init__(self, paths: d.Paths, device: str, seconds: float = 3.0) -> None:
        if d.busy(paths):
            raise d.DictationError("Finish the current dictation first.")
        values = dict(d.Config(paths).values, device=device, max_seconds=int(seconds) + 2)
        stdin = subprocess.DEVNULL if desktop.audio_backend(values) == "alsa" else subprocess.PIPE
        try:
            self.process = subprocess.Popen(
                desktop.recorder_command(values),
                stdin=stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                **desktop.process_options(),
            )
        except OSError as exc:
            raise d.DictationError("That microphone could not be opened.") from exc
        self.deadline = time.monotonic() + seconds
        self.level = 0.0  # The latest reading, 0 to 1.
        self.peak = 0.0
        self.heard = 0  # Readings with a voice in them.
        self.readings = 0
        threading.Thread(target=self._listen, daemon=True).start()

    def _listen(self) -> None:
        stream = self.process.stdout
        while stream is not None:
            chunk = stream.read(self.CHUNK)
            if not chunk:
                return
            self.level = d.audio_level(chunk)
            self.peak = max(self.peak, self.level)
            self.readings += 1
            self.heard += self.level > 0.35

    def done(self) -> bool:
        return time.monotonic() >= self.deadline or self.process.poll() is not None

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        if self.process.stdout is not None:
            self.process.stdout.close()

    def outcome(self) -> str:
        """none, quiet, faint or good (for statistics; `verdict` says it in words)."""
        if not self.readings:
            return "none"
        if self.peak < 0.15:
            return "quiet"
        return "good" if self.heard else "faint"

    def verdict(self) -> str:
        return {
            "none": "No sound came from that microphone. Pick another one, or check it is on.",
            "quiet": "That microphone is silent or very quiet. Try another one, or raise its level.",
            "faint": "Heard you, but faintly. Move closer or raise the microphone level.",
            "good": "Sounds good. This microphone is ready.",
        }[self.outcome()]


@dataclass(frozen=True)
class Remote:
    endpoint: str
    model: str
    key: str


@dataclass(frozen=True)
class Account:
    """What the account card shows."""

    kind: str  # disconnected, connected or reconnect (the account refused the key)
    email: str = ""
    sync: str = "off"  # off, syncing, ok, offline, error or auth: the service's report
    synced: float = 0.0  # When the account last synced fine (epoch seconds; 0: never)


_KEY_PROBLEMS = {
    "read-only": "That key can’t save to Clipboard+. Generate one with clipboard read and "
    "write access.",
    "write-only": "That key can’t read your Clipboard+ history. Generate one with clipboard "
    "read and write access.",
    "no-access": "That key has no clipboard access. Generate one with clipboard read and "
    "write access.",
    "invalid": "Clipboard+ didn’t accept that key. Copy a fresh key from your account and "
    "try again.",
    "offline": "Couldn’t reach Clipboard+. Check your internet connection and try again.",
}


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

    def clipboard_plus_email(self) -> str:
        return clipboardplus.read_email(self.paths.config.parent)

    def clipboard_plus_state(self) -> Account:
        if not self.clipboard_plus_linked():
            return Account("disconnected")
        report = clipservice.read_status(self.paths)
        sync = str(report.get("sync", "off"))
        return Account(
            "reconnect" if sync == "auth" else "connected",
            self.clipboard_plus_email(),
            sync,
            float(report.get("synced") or 0.0),
        )

    def _link_clipboard_plus(self, key: str, email: str) -> None:
        try:
            # Whatever account this device was linked to before, the history starts
            # afresh with the new key (uploads are idempotent, so nothing duplicates).
            if self.paths.clipboard.exists():
                store = clipstore.Store(self.paths.clipboard)
                try:
                    store.reset_sync()
                finally:
                    store.close()
        except sqlite3.Error as exc:
            raise d.DictationError("The clipboard history is busy. Try again in a moment.") from exc
        try:
            d.private_dir(self.paths.config.parent)
            clipboardplus.save_key(self.paths.config.parent, key)
            clipboardplus.save_email(self.paths.config.parent, email)
        except OSError as exc:
            raise d.DictationError(
                "Couldn’t save the Clipboard+ key. Check that your settings folder is writable."
            ) from exc
        self.sync_clipboard_now()

    def sign_in_clipboard_plus(self, email: str, password: str, *, create: bool) -> None:
        """Create an account or sign in, then keep only a clipboard read/write key.

        The password and the session token are used for these requests and dropped.
        """
        email = email.strip()
        if not email or not password.strip():
            raise d.DictationError("Enter your email and password.")
        name = f"{hotkeys.APP_NAME} on {socket.gethostname()}"[:80]
        try:
            token = (clipboardplus.register if create else clipboardplus.login)(email, password)
            key = clipboardplus.create_key(token, name)
        except clipboardplus.AuthError as exc:
            raise d.DictationError(str(exc)) from None
        self._link_clipboard_plus(key, email)

    def connect_clipboard_plus(self, key: str) -> None:
        """Link a Clipboard+ account by key. It is saved only once the service accepts it."""
        try:
            cleaned = clipboardplus.clean_key(key)
        except ValueError as exc:
            raise d.DictationError(str(exc)) from exc
        result = clipboardplus.verify(cleaned)
        if result in _KEY_PROBLEMS:
            raise d.DictationError(_KEY_PROBLEMS[result])
        if result != "ok":
            raise d.DictationError("Clipboard+ is having trouble right now. Try again shortly.")
        self._link_clipboard_plus(cleaned, "")

    def sync_clipboard_now(self) -> None:
        """Ask the clipboard service to sync at once."""
        d.private_dir(self.paths.runtime)
        d.atomic(self.paths.runtime / clipservice.SYNC_NOW, "1")

    def disconnect_clipboard_plus(self, keep_history: bool = True) -> None:
        """Forget the account. The history here is kept or deleted; the account is untouched."""
        clipboardplus.remove_key(self.paths.config.parent)
        (self.paths.runtime / clipservice.SYNC_NOW).unlink(missing_ok=True)
        if not self.paths.clipboard.exists():
            return
        store = clipstore.Store(self.paths.clipboard)
        try:
            store.reset_sync()  # Whatever account comes next starts from scratch.
            if not keep_history:
                store.wipe()
        finally:
            store.close()

    def clear_clipboard(
        self, store: clipstore.Store, *, everywhere: bool, keep_favorites: bool
    ) -> int:
        """Clear the history on this device, and in the account too when asked."""
        everywhere = everywhere and self.clipboard_plus_linked()
        count = store.clear(keep_favorites=keep_favorites, tombstones=everywhere)
        # The next sync must not bring back what was just cleared (images never sync).
        store.meta_set(clipstore.META_CLEARED, repr(time.time()))
        if everywhere:
            clipsync.request_clear(store, favorites=not keep_favorites)
            self.sync_clipboard_now()
        return count

    def delete_clipboard_data(self) -> None:
        """Erase the clipboard history, images and sync state on this device."""
        store = clipstore.Store(self.paths.clipboard)
        try:
            store.wipe()
        finally:
            store.close()

    def copy_item(self, item: clipstore.Item, store: clipstore.Store) -> None:
        """Put a history item back on the system clipboard."""
        config = d.Config(self.paths)
        try:
            if item.kind == "image":
                desktop.copy_image(config.values, store.image_path(item))
            else:
                d.copy_text(config, self.paths, item.text)
        except OSError as exc:
            raise d.DictationError(str(exc)) from exc

    def open_clipboard_website(self) -> None:
        """The user's history once linked; otherwise the Clipboard+ site."""
        import webbrowser

        webbrowser.open(
            clipboardplus.DASHBOARD_URL if self.clipboard_plus_linked() else clipboardplus.SITE
        )

    def microphones(self) -> list[str]:
        """The microphones to offer (device ids; `microphone_names` has friendly names)."""
        self.microphone_names: dict[str, str] = {}
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
            devices = self._alsa_microphones(output)
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

    def _alsa_microphones(self, listing: str) -> list[str]:
        """The inputs worth offering from `arecord -L`, the system default first.

        Outputs, mixers and raw variants (null, dmix, surround, hw…) are left out; each
        sound card is offered once, by the name its description gives it.
        """
        entries: list[tuple[str, str]] = []
        for line in listing.splitlines():
            if line and not line[0].isspace():
                entries.append((line.strip(), ""))
            elif entries and not entries[-1][1] and line.strip():
                entries[-1] = (entries[-1][0], line.strip())
        names = {"default": "System default (recommended)"}
        devices = ["default"] if any(device == "default" for device, _ in entries) else []
        for device, description in entries:
            if device in ("pipewire", "pulse"):
                names[device] = "PipeWire" if device == "pipewire" else "PulseAudio"
                devices.append(device)
            elif device.startswith("plughw:"):
                names[device] = description or device
                devices.append(device)
        if not devices:  # An unusual listing: offer it as it is rather than nothing.
            devices = [device for device, _ in entries if device != "null"]
        self.microphone_names = names
        return devices

    def prepare(
        self,
        language: str,
        device: str,
        model: str,
        progress: Callable[[int, int], None] | None = None,
        remote: Remote | None = None,
        pause: threading.Event | None = None,
    ) -> None:
        if d.busy(self.paths):
            raise d.DictationError("Finish recording before changing setup.")
        if language not in ("en", "auto") or not device.strip():
            raise d.DictationError("Choose a language and microphone first.")
        if desktop.platform_name() == "windows" and device == "default":
            raise d.DictationError("Choose Refresh, then pick the microphone you’ll speak into.")
        candidate = d.Config(self.paths)
        key_file = d.key_file(self.paths)
        settings: dict[str, object]
        new_host = False
        if remote is not None:
            host = urllib.parse.urlsplit(remote.endpoint.strip()).hostname
            loopback = host in ("localhost", "127.0.0.1", "::1")
            # A saved key belongs to the service it was entered for; never send it elsewhere.
            new_host = host != urllib.parse.urlsplit(candidate.s("endpoint")).hostname
            if not remote.key and not loopback and (new_host or not key_file.is_file()):
                raise d.DictationError(
                    f"Enter the API key for {host}. A saved key is only used with its own service."
                    if key_file.is_file()
                    else "Enter the API key for your transcription service."
                )
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
                    self.paths.config.parent / "models", language, progress, pause
                )
            )
            settings = dict(backend="local", model=str(path.resolve()), allow_remote=False)
        candidate.values.update(language=language, device=device.strip(), **settings)
        candidate.check(recording=True)
        d.private_dir(self.paths.config.parent)
        if remote is not None and remote.key:
            d.atomic(key_file, remote.key.strip())  # atomic() creates owner-only files.
        elif remote is None or new_host:
            key_file.unlink(missing_ok=True)
        # Save the user's file settings, not temporary DICTATION_* environment
        # overrides inherited by this desktop process.
        saved = d.DEFAULTS | d.read_json(self.paths.config)
        for key in ("ffmpeg", "whisper_bin"):
            if not saved[key]:
                saved[key] = candidate.values[key]
        saved.update(language=language, device=device.strip(), **settings)
        d.atomic(self.paths.config, json.dumps(saved, indent=2))

    def set_voice(self, language: str, device: str) -> None:
        """Save the language and microphone at once; the next recording uses them."""
        if d.busy(self.paths):
            raise d.DictationError("Finish recording before changing the microphone or language.")
        if language not in ("en", "auto") or not device.strip():
            raise d.DictationError("Choose a language and microphone first.")
        if desktop.platform_name() == "windows" and device == "default":
            raise d.DictationError("Choose Refresh, then pick the microphone you’ll speak into.")
        d.private_dir(self.paths.config.parent)
        saved = d.DEFAULTS | d.read_json(self.paths.config)
        saved.update(language=language, device=device.strip())
        d.atomic(self.paths.config, json.dumps(saved, indent=2))

    def set_option(self, key: str, value: bool) -> None:
        """Save one on/off dictation option at once (the recording bar, live drafts)."""
        if key not in ("overlay", "live", "auto_paste"):
            raise ValueError(key)
        d.private_dir(self.paths.config.parent)
        saved = d.DEFAULTS | d.read_json(self.paths.config)
        saved[key] = value
        d.atomic(self.paths.config, json.dumps(saved, indent=2))

    def action(self, action: str) -> None:
        config = d.Config(self.paths)
        if action == "copy":
            d.copy_text(config, self.paths)
        elif action == "copy-concise":
            import rewriting

            rewriting.run(config, self.paths, "copy")
        else:
            d.dispatch(config, self.paths, action)

    def concise(self) -> str:
        """Generate a reviewable draft while retaining the original transcript."""
        import rewriting

        rewriting.run(d.Config(self.paths), self.paths, "concise")
        return str(d.read_json(self.paths.cache / "concise.json").get("text", ""))
