# Clipboard+ and Dictation

*Formerly Whisper Dictation & Clipboard+.* Download page: https://clipboardplus.apercallc.com/desktop.html ·
Source: https://github.com/tommyqhoang/ClipboardPlus-and-Dictation

Free desktop app for Linux, macOS, and Windows with two features you can use
separately or together:

- **Dictation**: install it once, then **press a shortcut → speak → press it
  again → paste** in any app. Local Whisper by default.
- **Clipboard history**: remembers what you copy (text, links and images) so you can
  search it, star favorites and copy things back. Everything stays on your computer
  unless you choose to sync it with a [Clipboard+](https://clipboardplus.apercallc.com)
  account.

The same icon, menu and workflow on every platform: the app lives in the macOS
menu bar, the Windows system tray, or the Linux top bar and starts at login.
Press **Super+Shift+D** (**Win+Shift+D** on Windows, **⌃⌥⇧D** on a Mac) in any app, talk, press it again, and
notifications tell you it is recording, transcribing, and then ready to paste.

Linux remains the original platform. macOS and Windows support
is new; there is no signed app bundle or MSI installer yet. See
[desktop setup](docs/DESKTOPS.md) for dependencies, permissions, and validation limits.

| Platform | Installed launcher | Microphone | Clipboard (dictation) | Clipboard history capture |
| --- | --- | --- | --- | --- |
| Linux/Wayland | Top bar icon + GNOME shortcut | ALSA/PipeWire via arecord | wl-copy | `wl-paste --watch` where the desktop allows it, otherwise X11 selection events (also under XWayland) |
| macOS | Menu bar (`~/Applications/Clipboard+ and Dictation.app`) | FFmpeg AVFoundation | pbcopy | Pasteboard change count, polled twice a second |
| Windows x64 | System tray + Start Menu | FFmpeg DirectShow | Windows clipboard | Clipboard sequence number, polled four times a second |

Local Whisper is the default. You can select your own compatible local model,
use a local transcription server, or explicitly enable an external transcription
API. External services may charge; no subscription is required for local use.

## Privacy and anonymous diagnostics

The setup flow and **Settings → Privacy** include a single switch, **Share anonymous
crash reports and usage statistics**. It starts off: nothing is sent unless you
turn it on, and you can turn it off again at any time. `DO_NOT_TRACK=1` or `DICTATION_TELEMETRY=0` disables reporting for a launch.

When enabled, usage reports contain a random per-installation identifier, a fixed
event name, approved feature choices, and bounded counts or durations. Crash reports
contain the exception type and frames from Clipboard+ and Dictation only. Clipboard history,
transcripts, audio, file names and paths, email addresses, API keys, and arbitrary
error text are never sent. The app does not wait for reporting and quietly drops it
when offline. Test runs and CI always disable reporting.

Desktop usage reports reach the Clipboard+ API, which validates the fixed schema and
forwards it to Google Analytics only when its server-side Measurement Protocol secret
is configured. That secret is never included in the app or its installer.

## Quick install (no Git checkout)

Windows automatic installation is x64 only and still needs real-machine
acceptance testing.

macOS or Linux (Debian/Ubuntu, Fedora, Arch, openSUSE), in Terminal:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/tommyqhoang/ClipboardPlus-and-Dictation/main/bootstrap.sh)"
```

Windows x64, in PowerShell:

```powershell
& ([scriptblock]::Create((Invoke-WebRequest -UseBasicParsing https://raw.githubusercontent.com/tommyqhoang/ClipboardPlus-and-Dictation/main/bootstrap.ps1).Content))
```

These commands execute downloaded code: review the bootstrap script first if
you prefer. Administrator permission may be requested by dependency installers;
do not run the entire installation as root. macOS uses Homebrew (installing it
if missing); Linux uses apt, dnf, pacman or zypper and installs only what is missing; Windows requires Microsoft's App Installer/WinGet
and installs Python, FFmpeg, the C++ runtime and a checksum-pinned Whisper build.
The app snapshot is downloaded automatically. Windows installs the checksum-pinned
Whisper 1.8.7 binary; Linux pins its source-build fallback to the matching release
and commit. Git is only needed for that Linux fallback. Dependencies and model
downloads need internet access.

When installation finishes, **Clipboard+ and Dictation opens automatically**. The
first-run screens help the user:

1. Choose what to use: **Dictation**, **Clipboard history**, or **Both**. Only the
   steps for what you chose follow, and you can change it later in Settings.
2. For dictation: choose English or multilingual transcription and select a
   microphone (microphones are found automatically), then choose the transcription AI:
   - **Free on-device AI** (recommended): a verified 148 MB Whisper model,
     downloaded once; audio never leaves the computer.
   - **A Whisper model file you already have.**
   - **Your own AI service**: OpenAI, Groq, or any OpenAI-compatible
     `/audio/transcriptions` endpoint, with your API key. Audio is sent to that
     service, which may charge.
3. For clipboard history: an opt-in screen. Nothing is captured until you
   press **Turn on**; **Not now** leaves it off.
4. A short walkthrough, and (for clipboard history) an optional card to connect a
   [Clipboard+](https://clipboardplus.apercallc.com) account so the history also
   shows on the website and in the browser extension.

The walkthrough does not begin recording. The microphone starts only when you
press the shortcut (or **Test** next to the microphone, which listens for three
seconds and keeps nothing).

### Everyday use (all platforms)

- **Super+Shift+D** (Win+Shift+D on Windows, ⌃⌥⇧D on macOS) starts recording from any app. Press it again
  to stop. A small bar at the top of the screen shows your voice level while
  it listens, then *Transcribing…* and *Copied*; paste with Ctrl+V (⌘V). Click
  its ■ to stop or ✕ to cancel. The icon turns red while recording. (Set
  `"overlay": false` in `config.json` for plain notifications instead.)
- **Super+Shift+F** (Win+Shift+F on Windows, ⌃⌥⇧F on macOS) opens the clipboard
  history with the search box ready, from any app. Choose another shortcut or turn
  it off in *Settings → Keyboard shortcuts*.
- In the window, Ctrl+F (⌘F) searches the clipboard history, Ctrl+, (⌘,) opens
  Settings and Ctrl+W (⌘W) closes it. In the search box, ↑/↓ choose a result,
  Enter copies it and Esc clears the search. Opened by the history shortcut, a
  second Esc closes the window.
- The icon's menu: a status line, Start/Stop, Cancel Recording (while recording),
  Copy Last Transcript (dictation items appear only while Dictation is on),
  **Clipboard History…**, **Pause Clipboard Capture** (for an hour, or until you
  resume), **Settings…**, and under **More**: **Shortcut** (presets or *Record New
  Shortcut…*), **Open at Login** and **Clipboard+ Website…**. (The macOS menu bar
  keeps these at the top level.)
- If another shortcut already uses your keys (GNOME gives them to the first one),
  the Dictation tab says which one and offers to take the keys back.
- Your own service's API key is stored in the settings folder as
  `transcription-key`, readable only by your user account. An environment
  variable named by `api_key_env` still takes precedence.

Platform details:

- **macOS**: menu bar only (no Dock icon). The shortcut is a system hotkey, so no
  Accessibility permission is needed; macOS asks for microphone access on the
  first recording. Open at login is the LaunchAgent
  `~/Library/LaunchAgents/org.whisperdictation.menubar.plist`.
- **Windows**: system tray; the shortcut is registered with `RegisterHotKey`.
  Open at login is the `WhisperDictation` value under
  `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`.
- **Linux**: Wayland does not let apps capture global keys, so the shortcut is a
  GNOME custom keybinding that runs `~/.local/bin/dictate-toggle`; changing it
  from the menu updates GNOME. On other desktops, bind that command yourself.
  The top bar icon needs AppIndicator support (built into Ubuntu; an extension
  on stock GNOME). Open at login is `~/.config/autostart/whisper-dictation.desktop`.

The menu bar/tray component (PyObjC and Pillow on macOS; pystray, Pillow and, on Linux, python-xlib elsewhere) is
installed into `~/.local/share/whisper-dictation/venv` (Windows: under the app
folder in `%LOCALAPPDATA%`), so the system Python is not modified. No shell
profile or system `PATH` is silently changed.

For reproducible deployment, download bootstrap from a reviewed commit and set
`DICTATION_REF` to that commit (Windows: pass `-Ref`). The quick commands track
`main`; app snapshots are HTTPS downloads, not signed application releases.

## Clipboard history

The window has **Clipboard**, **Dictation** and **Settings** tabs (only for the
features you turned on). The Clipboard tab lists what you copied, newest first,
with a preview or thumbnail, how long ago, and where it came from (Desktop,
Dictation or Cloud). Search it, filter by All / Favorites / Images / Text, star an
item, click a row to copy it back, or delete it. *Clear history* deletes text,
links and copied images; with a Clipboard+ account connected it clears
**everywhere** by default (or **this device only**), and it keeps favorites unless
you say otherwise. Cleared items are not brought back by the next sync. Dictation transcripts join
the history too.

A small background service, started and restarted by the tray or menu bar app,
does the capturing; the window and dictation only read the shared database.

### What is stored, and where

- Text and links up to 1 MB each, and images up to 10 MB each (500 MB in total; the
  oldest non-favorite images go first). Files and other formats are ignored.
- By default the newest 1,000 items for up to 30 days are kept; favorites are never
  removed. Change both in Settings, or turn images off.
- Everything lives in the settings folder under `clipboard/`: a SQLite database
  (`clips.db`) and image files, readable only by you. It is **not** encrypted, so
  anything you copy that is not marked secret is stored in plain form.
- Nothing is captured until you turn Clipboard history on. **Pause Clipboard
  Capture** stops it for an hour or until you resume.
- Anything a password manager marks secret is skipped and never read: the
  `x-kde-passwordManagerHint` target on Linux, `org.nspasteboard.ConcealedType` /
  `TransientType` / `AutoGeneratedType` on macOS, and
  `ExcludeClipboardContentFromMonitorProcessing` / `CanIncludeInClipboardHistory = 0`
  on Windows. A password manager that does not set these cannot be recognised.
- A copied API key, access token or private key in a well-known format (for
  example `sk-…`, `ghp_…`, `AKIA…`, `-----BEGIN PRIVATE KEY-----`) is skipped the
  same way, so it is never stored or synced.
- Images never leave the computer, even with an account connected.
- **Settings → Delete all clipboard data** erases the database and images on this
  computer (the Clipboard+ account is not touched). Uninstalling keeps your history,
  like every other setting.

### Clipboard+ account (optional)

Connect an account in Settings (or during setup) and the history is mirrored with
your Clipboard+ account, so the website and the browser extension show the same
items without duplicates.

- **Create account** or **Sign in** with email and password. The app uses them for
  one request to create a key limited to clipboard read and write, then discards
  the password and the session; only the key is kept, as `clipboard-plus-key`
  (readable only by you), plus the address for display. Accounts that use Google
  sign-in can paste a key made on the website (**Use an API key instead**).
- Text and links are sent (up to 50,000 bytes each); images and audio never are.
  Requests go over HTTPS only and redirects are never followed, so the key cannot
  be forwarded elsewhere.
- Sync runs about every minute, five seconds after a new copy, and on **Sync now**.
  Items made elsewhere appear here (never written to your clipboard); favorites and
  labels follow whichever side changed last; deleting an item deletes it in both
  places, and *Clear history → Everywhere* clears the account too.
- The same copy captured by the app and by the extension (a few seconds apart) is
  one item: the Clipboard+ service skips identical content from a different source
  within ten minutes, and the extension merges such an item on pull. That needs the
  updated Clipboard+ service and extension.
- If the account refuses the key, the card says **Reconnect needed** and syncing
  stops until you sign in again. Being offline just retries with a growing delay
  (30 seconds up to 10 minutes). **Disconnect** asks whether to keep the history on
  this computer; the account keeps its own copy either way.
- Existing installs that had connected a key only for dictation no longer upload
  transcripts on their own: turn on Clipboard history and syncing continues.

### Shortcuts

| Platform | Dictation shortcut | Clipboard history shortcut |
| --- | --- | --- |
| Linux | Super+Shift+D (GNOME keybinding) | Super+Shift+F (GNOME keybinding) |
| Windows | Win+Shift+D | Win+Shift+F |
| macOS | ⌃⌥⇧D | ⌃⌥⇧F |

These were checked against Chrome's published shortcut list, which uses Alt, Ctrl,
Ctrl+Shift and (on a Mac) ⌘ combinations with D but no Win/Super or ⌃⌥ ones. That
list does not cover the in-page shortcuts of web apps such as Google Docs, or the
shortcuts of other software you run; pick any preset or record your own from the
menu. `Ctrl+Alt+D`, the earlier default, is still one click away.

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

The installer detects your package manager (apt, dnf, pacman or zypper), installs only the dependencies that are missing (asking for administrator access only then), downloads the base English model,
builds whisper.cpp if necessary, adds Clipboard+ and Dictation to the application menu,
and requests the optional GNOME shortcut Super+Shift+D. Open the app to complete
the graphical walkthrough. Check the shortcut setup output; headless installations
skip GNOME registration. Use `--no-packages` when dependencies are already installed.

For a server-only setup, `./install.sh --http` skips the local model and Whisper
build. Configure the endpoint before recording. Existing settings are never
overwritten by installation or `--init-config`.

Other Wayland desktops can bind `~/.local/bin/dictate-toggle` themselves.
For example, Sway: `bindsym $mod+Shift+d exec ~/.local/bin/dictate-toggle`.
GNOME users change the shortcut from the top bar icon's **Shortcut** menu. The
app re-applies its saved shortcut each time it starts, so
`DICTATION_BINDING='<Super><Shift>v' ./install.sh --no-packages` only lasts when
the top bar app is not used.

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

Unix runtime files, audio, settings and the clipboard history use private permissions. Windows uses
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
the installed command/module and GNOME binding, and stops the clipboard service,
retaining models, settings, transcripts, recoverable audio and the clipboard history
(use **Delete all clipboard data** first to erase it).

## Architecture and development

`bin/dictate-toggle` is a small launcher for the standard-library Python runtime
in `lib/dictation.py`. `lib/desktop.py` owns OS-specific commands, paths, and locks.
`lib/app.py` provides the graphical workflow and `lib/app_service.py` keeps its
setup and action logic separate from Tk rendering.
The clipboard history is a separate headless process (`lib/clipservice.py`) that the
tray starts and restarts: `lib/clipwatch*.py` are the per-OS watchers behind one
`Watcher` interface, `lib/clipstore.py` is the only code that touches the SQLite
database and image files, `lib/clipsync.py` is the two-way account sync (pure logic
over the store and a cloud client, tested offline), `lib/clipboardplus.py` is the
account/API client, `lib/clipui.py` the Tk pages, and `lib/clipcontrol.py` the shared
tray/menu-bar supervision. The window and dictation reach the service through the
database, `clip-status.json` and small runtime signal files.
`lib/rewriting.py` owns opt-in text-model requests and review/copy locking;
`lib/workflow.py` provides the read-only session monitor and safe terminal display.
Each recording starts one detached supervisor, which owns
the recorder, control state, optional preview task, and final transcription.
There is no always-running dictation daemon (only the clipboard service runs
continuously, and only while Clipboard history is on). Local CLI and HTTP transcription
share normalization, recovery, and clipboard delivery.

```bash
python3 -m unittest discover -s tests -v
ruff check lib tests tools setup-desktop.py
ruff format --check lib tests tools setup-desktop.py
mypy --strict lib tools setup-desktop.py
bash -n bin/dictate-toggle bootstrap.sh install.sh uninstall.sh tests/with-xvfb.sh
shellcheck bin/dictate-toggle bootstrap.sh install.sh uninstall.sh tests/check.sh tests/with-xvfb.sh
python3 -m compileall -q lib setup-desktop.py
```

The app icon is generated, not hand-edited: run `python3 tools/make_icon.py` on
macOS to rebuild the matching native and website icon files in `lib/`, `assets/`,
and `site/assets/`.

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
