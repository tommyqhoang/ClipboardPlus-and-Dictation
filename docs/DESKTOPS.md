# Desktop installation and platform notes

Most users should use the [one-command installer](../README.md#quick-install-no-git-checkout).
It downloads the application snapshot, installs required runtime dependencies,
registers a normal desktop launcher, opens Whisper Dictation automatically, and
shows the first-run walkthrough. Users do not need a Git checkout or application
commands.

## What gets installed

| Platform | Launcher | Local dependencies |
| --- | --- | --- |
| Debian/Ubuntu Linux | Top bar icon (starts at login), application menu, GNOME Ctrl+Alt+D | Python/Tk, ALSA tools, Wayland clipboard, whisper.cpp, AppIndicator; pystray and Pillow in a private venv |
| macOS | Menu bar icon from `~/Applications/Whisper Dictation.app` (starts at login), ⌃⌥D | Homebrew Python/Tk, FFmpeg, whisper.cpp; PyObjC in a private venv |
| Windows x64 | System tray icon (starts at login), Start Menu, Ctrl+Alt+D | Python/Tk, FFmpeg, C++ runtime, checksum-pinned whisper.cpp; pystray and Pillow in a private venv |

The Windows bootstrap currently pins whisper.cpp 1.8.7 and verifies the upstream
release digest. Debian/Ubuntu uses its package when available; the source fallback
pins the same release and commit instead of building an unreviewed moving branch.

The app opens to a welcome screen. It asks for language and microphone, then
downloads and verifies a free base model unless the user chooses an existing
compatible model. Setup does not record. The microphone starts only after the
user presses **Record**.

After setup, the app provides Record/Stop, Cancel, Copy, Retry saved recording,
Discard saved recording, Settings, and Help. A second launch focuses the existing
window instead of opening another recorder. The window sizes itself to the display
and exposes a scrollbar when all controls do not fit.

## Permissions

On macOS, allow microphone access when requested. If it was denied, use System
Settings → Privacy & Security → Microphone, then reopen Whisper Dictation.

On Windows, enable microphone access for desktop applications under Privacy &
security → Microphone. Windows device selection uses the exact DirectShow audio
name discovered by the app.

On Linux, microphone access is provided through ALSA/PipeWire. Sandboxed desktop
environments may require their own microphone permission. The graphical session
must expose a Wayland clipboard for automatic copying.

FFmpeg's [device documentation](https://ffmpeg.org/ffmpeg-devices.html) describes
the AVFoundation and DirectShow capture interfaces used on macOS and Windows.

## Developer and custom installation

Developers working from a checkout can run `./install.sh` on Debian/Ubuntu or
`python3 setup-desktop.py` on macOS/Windows. The graphical app is installed with
the core runtime. `python3 setup-desktop.py --launch-only` opens the installed
app; `--uninstall` removes known application files and its registered launcher.
Models, settings, transcripts, and recoverable audio are retained by uninstall.

The installer records absolute FFmpeg and Whisper executable paths so a launcher
does not depend on an interactive shell's `PATH`. The legacy terminal interface
remains available for maintainers and automation, but it is not part of the
normal user workflow.

## Verification limits

The window and first-run flow are exercised natively on macOS and under a Linux
virtual desktop. Launcher construction is tested for Linux, macOS, and Windows.
The CI definition includes all three operating systems, but an unrun workflow is
not evidence of a successful remote job.

Physical microphone permission prompts, Windows DirectShow capture and clipboard,
Linux application-menu refresh, and full one-command installs still require
acceptance testing on clean target machines. Windows ARM64 automatic installation
is not supported. The macOS bundle and Windows launcher are not code-signed or
notarized, so operating-system warnings may appear. Windows support remains
experimental until real-machine acceptance testing is complete.
