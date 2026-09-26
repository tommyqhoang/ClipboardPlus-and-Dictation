# Whisper Dictation

Free, local dictation for Linux, macOS, and Windows. Install it once, then use a
normal desktop window: **Record → speak → Stop → paste**.

Linux remains the original platform. macOS and Windows support
is new; there is no signed app bundle or MSI installer yet. See
[desktop setup](docs/DESKTOPS.md) for dependencies, permissions, and validation limits.

| Platform | Installed launcher | Microphone | Clipboard |
| --- | --- | --- | --- |
| Linux/Wayland | Application menu | ALSA/PipeWire via arecord | wl-copy |
| macOS | `~/Applications/Whisper Dictation.app` | FFmpeg AVFoundation | pbcopy |
| Windows x64 | Start Menu | FFmpeg DirectShow | Windows clipboard |

Local Whisper is the default. You can select your own compatible local model,
use a local transcription server, or explicitly enable an external transcription
API. External services may charge; no subscription is required for local use.

## Quick install (no Git checkout)

**Publication status:** these commands work once this version is published to
the repository's `main` branch. They are not a claim that the current local
changes have been released. Windows automatic installation is x64 only and
still needs real-machine acceptance testing.

macOS or Debian/Ubuntu Linux, in Terminal:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/tommyqhoang/wayland-whisper-dictation/main/bootstrap.sh)"
```

Windows x64, in PowerShell:

```powershell
& ([scriptblock]::Create((Invoke-WebRequest -UseBasicParsing https://raw.githubusercontent.com/tommyqhoang/wayland-whisper-dictation/main/bootstrap.ps1).Content))
```

These commands execute downloaded code: review the bootstrap script first if
you prefer. Administrator permission may be requested by dependency installers;
do not run the entire installation as root. macOS uses Homebrew (installing it
if missing); Linux uses apt; Windows requires Microsoft's App Installer/WinGet
and installs Python, FFmpeg, the C++ runtime and a checksum-pinned Whisper build.
The app snapshot is downloaded automatically. Windows installs the checksum-pinned
Whisper 1.8.7 binary; Linux pins its source-build fallback to the matching release
and commit. Git is only needed for that Linux fallback. Dependencies and model
downloads need internet access.

When installation finishes, **Whisper Dictation opens automatically**. The
first-run screens help the user:

1. Choose English or multilingual transcription.
2. Find and select a microphone.
3. Download a verified free local model, or select an existing compatible model.
4. Learn the Record, Stop, Cancel, Copy, Retry, and Paste workflow.

The walkthrough does not begin recording. The microphone starts only after the
user presses **Record**. Later, the app is opened from Applications, the Start
Menu, or the Linux application menu—no application commands are required. The
app displays recording/transcription status, the latest transcript, clipboard
copying, and recoverable-audio actions. Settings and Help are available inside
the window. The interface scrolls on smaller displays instead of hiding setup or
recovery controls. No shell profile or system `PATH` is silently changed.

For reproducible deployment, download bootstrap from a reviewed commit and set
`DICTATION_REF` to that commit (Windows: pass `-Ref`). The quick commands track
`main`; app snapshots are HTTPS downloads, not signed application releases.

## Why use this instead of built-in dictation?

**Free dictation. Your model. Your words.**

- Choose a compatible local speech model, add vocabulary hints, or bring your
  own transcription endpoint. Local mode has no subscription or per-minute fee.
- Recover a failed recording and copy your last transcript without dictating again.
- Use the same configurable workflow across supported desktops.

Not automatically faster or more accurate: results depend on model, hardware,
microphone, accent, and vocabulary. Larger models can help at the cost of speed
and memory. Built-in dictation has tighter system integration and may be better
for immediate text insertion; this app currently copies text for you to paste.
Apple offers [on-device dictation in supported configurations](https://support.apple.com/guide/mac-help/mh40584/mac),
and Windows [Voice Access can work offline](https://support.microsoft.com/en-au/accessibility/windows/voice-access/set-up-voice-access).
Offline operation is therefore not unique to this app.

**Does it shorten or rephrase?** The engine has an experimental, opt-in text
rewrite adapter, but it is intentionally not part of the first-run experience or
required workflow. The desktop app currently prioritizes faithful transcription.
Any future rewrite UI should keep the original, show changes for review, and never
send text to an external provider without clear consent.

## Install from source on Linux

Requires Python 3.10+, ALSA/PipeWire, and a Wayland desktop.

```bash
./install.sh
```

The installer installs Debian dependencies, downloads the base English model,
builds whisper.cpp if necessary, adds Whisper Dictation to the application menu,
and requests the optional GNOME shortcut Super+Shift+D. Open the app to complete
the graphical walkthrough. Check the shortcut setup output; headless installations
skip GNOME registration. Use `--no-packages` when dependencies are already installed.

For a server-only setup, `./install.sh --http` skips the local model and Whisper
build. Configure the endpoint before recording. Existing settings are never
overwritten by installation or `--init-config`.

Other Wayland desktops can bind `~/.local/bin/dictate-toggle` themselves.
For example, Sway: `bindsym $mod+Shift+d exec ~/.local/bin/dictate-toggle`.
GNOME users can change the binding with
`DICTATION_BINDING='<Super><Shift>v' ./install.sh --no-packages`.

## Settings and accuracy

Edit `~/.config/dictation/config.json` (or `$XDG_CONFIG_HOME/dictation/config.json`).
Settings are read on each session, including sessions launched by the desktop
shortcut. This avoids relying on terminal environment variables reaching GNOME.

On macOS, settings default to
`~/Library/Application Support/WhisperDictation/config.json`; on Windows,
`%LOCALAPPDATA%\WhisperDictation\Config\config.json`.
`dictate-toggle --status` prints the active configuration path.

Example local settings:

```json
{
  "backend": "local",
  "model": "/absolute/path/to/ggml-small.en.bin",
  "language": "en",
  "device": "default",
  "threads": 4,
  "prompt": "Vocabulary: PostgreSQL, Kubernetes, Wayland.",
  "voice_commands": true,
  "live": false
}
```

The model must be compatible with whisper.cpp: an arbitrary chat-model name or
API key is not a local speech model. Install another model using
`DICTATION_MODEL_NAME=ggml-small.en.bin ./install.sh --no-packages`, or point
`model` at your own file. An explicit missing model produces an error rather
than silently using a different model.

Downloads are staged in a resumable partial file and checked for a GGML header
before activation. The built-in English and multilingual base models are verified
against pinned SHA-256 digests. For another model, set `DICTATION_MODEL_SHA256`
to a trusted digest; otherwise the installer clearly warns that only the GGML
header was checked. Model files selected in the app are also rejected early when
they do not have a whisper.cpp GGML header.

Larger models trade memory and speed for potential accuracy gains; measure them
on your microphone, accent, and vocabulary. Use a multilingual model with
`"language": "auto"` for automatic language detection. Vocabulary prompts can
help names and specialized terms, but are hints, not guarantees.

An optional `vad_model` path enables whisper.cpp's voice activity detection.
This requires a whisper-cli version supporting `--vad --vad-model` and its
compatible VAD model. Exact digital silence is skipped without inference.
Ordinary spoken words such as “music” are preserved.

With `voice_commands` enabled, “new line” and “new paragraph” become line breaks.
Disable it when you want those phrases transcribed literally.

## Bring your own transcription service

The HTTP adapter sends WAV audio as multipart form data and expects
`{"text": "..."}` JSON. It supports whisper.cpp's `/inference` endpoint and
compatible file-transcription endpoints; it does not support arbitrary chat APIs.

Local server settings:

```json
{
  "backend": "http",
  "endpoint": "http://127.0.0.1:8080/inference",
  "language": "en",
  "live": true
}
```

Run your own whisper.cpp server, for example:
`whisper-server -m /path/to/model.bin --host 127.0.0.1 --port 8080`.
The installer builds the CLI only; build the optional `whisper-server` target
in your whisper.cpp checkout if needed. A server retains the model in memory
between requests, avoiding repeated CLI model startup.

For an external compatible provider:

```json
{
  "backend": "http",
  "endpoint": "https://your-provider.example/v1/audio/transcriptions",
  "api_model": "your-provider-speech-model",
  "api_key_env": "DICTATION_API_KEY",
  "allow_remote": true,
  "live": false
}
```

Set the named API key environment variable in the desktop session or a private
launcher. Terminal exports only apply to commands launched from that terminal.
Keys are read from the environment and are not stored in settings, command-line
arguments, or application logs. Remote endpoints require HTTPS and explicit
`allow_remote` consent. Redirects are rejected so credentials and audio cannot
be forwarded unexpectedly. Failed requests are not automatically retried or
silently sent to another provider.

## Advanced: live preview

Set `live: true`, start recording, and run `dictate-toggle --watch` in a terminal.
Optionally set `preview_notifications: true` to show draft text in desktop
notifications (which may appear on the lock screen).

This is periodic near-live transcription, not token streaming. It transcribes
the latest `live_window` seconds (default 30) every `live_interval` seconds
(default 5), waiting for each result before scheduling another. Drafts are
provisional and never change the clipboard. After stopping, the entire recording
is transcribed once for the final result.

Use a local resident server for repeated previews when possible. The CLI loads
its model per request. Remote preview repeatedly uploads overlapping audio and
can increase charges. A running preview must finish or time out before final
transcription or cancellation completes; recording itself stops promptly.

## Advanced: recovery, diagnostics, and privacy

Normal recovery actions are buttons in the app. The commands below are optional
diagnostics for maintainers and automated environments; users do not need them
for everyday dictation.

```bash
dictate-toggle --status      # JSON phase and recovery status
dictate-toggle --cancel      # stop and discard the active recording
dictate-toggle --transcribe  # retry retained audio
dictate-toggle --copy-last   # recover from clipboard failure without inference
dictate-toggle --discard     # explicitly delete retained audio
dictate-toggle --devices     # list ALSA input names
dictate-toggle --doctor      # validate settings and required files/executables
```

Recordings are bounded to `max_seconds` (default 300, maximum 600), 16 kHz mono
16-bit PCM. The recorder belongs to a supervised session; no process is signalled
using a stale PID file. OS locks prevent concurrent sessions from overwriting
audio, including the background-worker startup window.

Transcripts are atomically saved in `~/.cache/dictation/last.txt`. Current audio
is `dictation.pcm`; failed sessions retain it for retry. Successful sessions
delete audio unless `keep_audio: true`. Retained audio must be retried or
explicitly discarded before starting another recording. Silence preserves the
previous transcript and clipboard.

Unix runtime files, audio, and settings use private permissions. Windows uses
per-user LocalAppData directories and their inherited access controls; do not
override the data directories to a shared location. Transcript text and
provider responses are not written to logs; user-facing errors are in
`--status`. Draft text is removed when the session ends. A hard process kill may
leave retained audio/drafts; these remain private files until explicitly removed
or the next session cleans the draft. Historical logs from older releases are
not automatically deleted.

Environment overrides still work: `DICTATION_MODEL`, `DICTATION_LANGUAGE`,
`DICTATION_WHISPER_BIN`, `DICTATION_WHISPER_THREADS`, `DICTATION_DEVICE`,
`DICTATION_WL_COPY`, `DICTATION_KEEP_AUDIO`, and corresponding
`DICTATION_<SETTING>` names. Boolean overrides accept 0/1 or true/false.
XDG config/cache/runtime paths are respected.

Uninstall with `./uninstall.sh` after the active session finishes. It removes
the installed command/module and GNOME binding, retaining models, settings,
transcripts, and recoverable audio.

## Architecture and development

`bin/dictate-toggle` is a small launcher for the standard-library Python runtime
in `lib/dictation.py`. `lib/desktop.py` owns OS-specific commands, paths, and locks.
`lib/app.py` provides the graphical workflow and `lib/app_service.py` keeps its
setup and action logic separate from Tk rendering.
`lib/rewriting.py` owns opt-in text-model requests and review/copy locking;
`lib/workflow.py` provides the read-only session monitor and safe terminal display.
Each recording starts one detached supervisor, which owns
the recorder, control state, optional preview task, and final transcription.
There is no always-running dictation daemon. Local CLI and HTTP transcription
share normalization, recovery, and clipboard delivery.

```bash
python3 -m unittest discover -s tests -v
ruff check lib tests setup-desktop.py
ruff format --check lib tests setup-desktop.py
mypy --strict lib setup-desktop.py
bash -n bin/dictate-toggle bootstrap.sh install.sh uninstall.sh tests/with-xvfb.sh
shellcheck bin/dictate-toggle bootstrap.sh install.sh uninstall.sh tests/check.sh tests/with-xvfb.sh
python3 -m compileall -q lib setup-desktop.py
```

Tests use a subprocess recorder and transcription fixture plus an actual local
HTTP server. They do not establish speech accuracy, hardware latency, or live
GNOME/Wayland behavior. Those require microphone and desktop testing on Linux.

Run the complete isolated Linux gate with:

```bash
docker build -f tests/Dockerfile -t wayland-dictation-quality .
docker run --rm wayland-dictation-quality
```

`tests/benchmark.py` optionally compares a real CLI and resident server using
your model and an existing 16 kHz mono WAV. It performs no downloads.
The server adapter follows the
[whisper.cpp server contract](https://github.com/ggml-org/whisper.cpp/tree/master/examples/server).
