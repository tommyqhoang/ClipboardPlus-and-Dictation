# Packaging Clipboard+

Builds a self-contained onedir bundle per OS with PyInstaller — no system
package manager, no sudo/UAC, no visible Python. See
`docs/superpowers/specs/2026-09-28-packaged-installers-design.md` for why.

One residual dependency: on Linux, ALSA-backend mic capture still shells
out to the system `arecord` (from `alsa-utils`) rather than a bundled
binary — only ffmpeg and whisper.cpp are bundled, per the design spec's
scope. Most desktops already have `alsa-utils` installed; this is a known,
intentional gap, not an oversight.

## Linux

    pip install pyinstaller
    pyinstaller packaging/linux/clipboardplus.spec
    ls dist/clipboardplus/   # tray, app, dictation, engine, overlay, updates, clipservice
    dist/clipboardplus/tray --help    # smoke-test before wrapping; Ctrl+C if it doesn't exit on its own
    bash packaging/linux/build-appimage.sh   # needs appimagetool on PATH; produces dist/Clipboard+-x86_64.AppImage

## macOS

    pip install pyinstaller
    pyinstaller packaging/macos/clipboardplus.spec
    bash packaging/macos/build-dmg.sh   # needs hdiutil (macOS only); produces dist/Clipboard+.dmg

## Windows

    pip install pyinstaller
    pyinstaller packaging\windows\clipboardplus.spec
    iscc packaging\windows\clipboardplus.iss
    :: needs Inno Setup's iscc on PATH; produces dist\Clipboard+-Setup.exe

## Manual pre-release checklist (run this on a clean VM/user account, no dev tools)

1. Install the artifact (`.dmg` drag, Inno Setup `.exe`, or `.AppImage`
   after `chmod +x`).
2. Confirm zero terminal windows appear at any point.
3. Confirm the *only* prompt is the one OS security click (Gatekeeper
   right-click-Open on macOS, SmartScreen "More info -> Run anyway" on
   Windows; none at all on Linux beyond the chmod/"allow executing" step).
4. Run first-run setup end to end: the model download shows real progress
   (megabytes and a percentage, not a spinner), the GNOME shortcut (Linux)
   or global shortcut registration succeeds, "Open at Login" takes effect
   after a reboot.
5. Once a newer tagged release exists, trigger the in-app "Check for
   Updates" and confirm it downloads, swaps, and relaunches cleanly.
