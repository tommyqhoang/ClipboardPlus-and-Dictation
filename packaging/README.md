# Packaging Clipboard+

Builds a self-contained onedir bundle per OS with PyInstaller — no system
package manager, no sudo/UAC, no visible Python. See
`docs/superpowers/specs/2026-09-28-packaged-installers-design.md` for the original
design (a historical record; this file and the code describe what ships).

One residual dependency: on Linux, ALSA-backend mic capture still shells
out to the system `arecord` (from `alsa-utils`) rather than a bundled
binary — only ffmpeg and whisper.cpp are bundled, per the design spec's
scope. Most desktops already have `alsa-utils` installed. The AppImage's
`AppRun` runs a preflight at startup: if `arecord` is missing it prints and
notifies a message naming the package, and clipboard history keeps working.
This is a documented requirement, not an oversight.

Architectures: the release builds Linux x86_64, Windows x86_64, macOS Intel
(x86_64) and macOS Apple silicon (arm64). There is no Linux ARM64 or Windows
ARM64 installer; those users use the source install.

Licensing: `LICENSE` and `THIRD-PARTY-NOTICES.md` (whisper.cpp MIT, ffmpeg GPL
with a source offer) are copied into every installer.

The commands below produce a bundle with no ffmpeg or whisper.cpp — that
bundling is a separate step CI runs after `pyinstaller` and before wrapping
(fetches a static ffmpeg, builds whisper.cpp from the same pinned source
install.sh uses, and copies both into `dist/clipboardplus/`); see the
"Bundle ffmpeg + whisper.cpp" steps in `.github/workflows/release.yml` for
the exact commands to reproduce that locally.

## Release pipeline and supply chain

- A push to `main` or a manual `workflow_dispatch` runs `release.yml`. It reads
  `lib/desktop.py` `APP_VERSION`, skips versions that are already released,
  requires the full `quality.yml` suite to pass, then builds and inspects every
  installer. The workflow publishes the release and its `v<APP_VERSION>` tag
  only after all installer assets and checksums are ready.
- Pinned inputs: Python packages in `requirements-gui.txt` (GUI toolkits, the
  single place these pins live) and `requirements-release.txt` (adds the
  PyInstaller version); action SHAs; whisper.cpp tag + commit; ffmpeg URLs with
  SHA-256 (`FFMPEG_*` in `release.yml`); `appimagetool` release tag with
  SHA-256; Inno Setup version. To bump a downloaded tool, change the URL and its
  hash together (download it and run `sha256sum`); a mismatch fails the build.
  macOS ffmpeg comes from Homebrew (brew verifies its bottle; the version floats
  and is logged).
- After wrapping, each installer is inspected on the runner: AppImage extracted
  and its contents checked, DMG mounted and verified with `codesign` (and
  `spctl` / `stapler` when notarized), Windows installer silently installed
  and uninstalled.
- Dependabot updates actions and pip pins weekly; CodeQL scans Python; the
  `dependency-audit` job runs `pip-audit` on the pinned requirements.

## Signing and notarization secrets

All signing is optional. With no secrets the release still builds (macOS
ad-hoc signed, Windows unsigned) exactly as before. Add these as GitHub
repository (or environment) secrets:

macOS Developer ID + notarization (all needed together; the steps run when
`APPLE_CERTIFICATE` is set):

| Secret | Value |
|---|---|
| `APPLE_CERTIFICATE` | base64 of the "Developer ID Application" `.p12` (`base64 -i cert.p12`) |
| `APPLE_CERTIFICATE_PASSWORD` | password of that `.p12` |
| `APPLE_SIGNING_IDENTITY` | e.g. `Developer ID Application: Your Name (TEAMID)` |
| `APPLE_ID` | Apple ID used for notarization |
| `APPLE_TEAM_ID` | 10-character team id |
| `APPLE_APP_SPECIFIC_PASSWORD` | app-specific password for that Apple ID |

The bundle is signed with the hardened runtime, a secure timestamp and
`packaging/macos/entitlements.plist` (unsigned executable memory and
library-validation off for the PyInstaller runtime, microphone access), the DMG
is signed, submitted with `notarytool --wait` and stapled. `Info.plist` sets
`LSMinimumSystemVersion` 11.0.

Windows, choose one (Azure wins when both exist):

| Secret | Value |
|---|---|
| `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET` | service principal for Azure Trusted Signing |
| `AZURE_SIGNING_ENDPOINT` | e.g. `https://eus.codesigning.azure.net` |
| `AZURE_SIGNING_ACCOUNT`, `AZURE_SIGNING_PROFILE` | signing account and certificate profile names |
| `WINDOWS_CERTIFICATE`, `WINDOWS_CERTIFICATE_PASSWORD` | alternative: base64 `.pfx` code-signing certificate and its password (signtool) |

The app's executables and then `Clipboard+-Setup.exe` are signed and
timestamped. Signing steps have not been exercised without real credentials;
run a tagged pre-release once they are added and check the run.

## Uninstall

Windows: the Inno uninstaller (asks about user data, default keep). macOS:
`Uninstall Clipboard+.command` on the DMG (`--purge` deletes data). Linux:
`Clipboard+-x86_64.AppImage --uninstall [--purge] [--delete-appimage]`. The
repo-level `uninstall.sh` / `uninstall.ps1` handle source installs (`--purge` /
`-Purge`). See the top-level README, "Removing a packaged install".

## Linux

    pip install -r requirements-release.txt
    pyinstaller packaging/linux/clipboardplus.spec
    ls dist/clipboardplus/   # tray, app, dictation, engine, overlay, updates, clipservice
    dist/clipboardplus/tray --help    # smoke-test before wrapping; Ctrl+C if it doesn't exit on its own
    bash packaging/linux/build-appimage.sh   # needs appimagetool on PATH (CI pins a release); produces dist/Clipboard+-x86_64.AppImage

## macOS

    pip install -r requirements-release.txt
    pyinstaller packaging/macos/clipboardplus.spec
    bash packaging/macos/build-dmg.sh   # needs hdiutil (macOS only); produces dist/Clipboard+.dmg

`build-dmg.sh` ad-hoc re-signs every Mach-O in the bundle (innermost first,
then the `.app`) and verifies with `codesign --verify --deep --strict`. This
is mandatory even for the "unsigned" build: Apple Silicon refuses to exec
unsigned or invalidly-signed native code, and the dylibbundler step that
bundles Homebrew's ffmpeg invalidates ffmpeg's shipped signatures by
rewriting load paths. Ad-hoc signing is NOT Developer ID signing — the app
still isn't notarized, so the DMG also ships an `Install Clipboard+.command`
helper: double-clicking it copies the app to /Applications, strips the
download-quarantine flag Gatekeeper blocks on ("damaged and can't be
opened"), and launches the app. (A .command is not an app bundle, so
Gatekeeper doesn't assess it — Terminal opens it with at most a one-click
"downloaded from the internet?" confirmation.) With the `APPLE_*`
secrets configured (see below), `release.yml` instead signs with Developer ID,
notarizes and staples, and the helper becomes unnecessary (kept as a fallback).

## Windows

    pip install -r requirements-release.txt
    pyinstaller packaging\windows\clipboardplus.spec
    iscc packaging\windows\clipboardplus.iss
    :: needs Inno Setup's iscc on PATH; produces dist\Clipboard+-Setup.exe

## Manual pre-release checklist (run this on a clean VM/user account, no dev tools)

1. Install the artifact (`.dmg` drag, Inno Setup `.exe`, or `.AppImage`
   after `chmod +x`).
2. Confirm zero terminal windows appear at any point.
3. Confirm the *only* prompt is the one OS security click (macOS: double-click
   "Install Clipboard+" in the DMG — at most a "downloaded from the internet?"
   confirmation; the drag route needs the manual `xattr -dr com.apple.quarantine`
   step. Windows: SmartScreen "More info -> Run anyway"; Linux: none beyond the
   chmod/"allow executing" step).
4. Run first-run setup end to end: the model download shows real progress
   (megabytes and a percentage, not a spinner), the GNOME shortcut (Linux)
   or global shortcut registration succeeds, "Open at Login" takes effect
   after a reboot.
5. Once a newer tagged release exists, trigger the in-app "Check for
   Updates" and confirm it downloads, swaps, and relaunches cleanly.
