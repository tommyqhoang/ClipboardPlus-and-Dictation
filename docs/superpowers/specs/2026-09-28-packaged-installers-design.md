# Packaged installers for macOS, Windows, and Linux

> **Historical design record, not the current spec.** Where this document
> differs from what ships, the code and `packaging/README.md` win. Known
> differences: code signing is now conditional (Developer ID + notarization on
> macOS and Azure Trusted Signing / signtool on Windows when release secrets
> exist; ad-hoc/unsigned otherwise); in-app self-update for packaged installs
> is disabled in v1; the AppImage is assembled by `build-appimage.sh` and
> `appimagetool` (linuxdeploy is not used); Linux dictation needs the system
> `arecord` (alsa-utils); only the three main installers are built (macOS
> Apple Silicon, Windows x86_64, Linux x86_64).

Status: approved for planning
Decided with the user: no code signing (budget is $0), real downloadable
installers per OS, and a fully self-contained bundle (no system package
managers, no sudo/UAC at all).

## 1. Goals / non-goals

**Goals**
- A user downloads one file per OS and ends up with a running app, with no
  visible Python, pip, or terminal output, and at most one unavoidable OS
  security click (Gatekeeper right-click-Open on macOS, SmartScreen "Run
  anyway" on Windows) — never a repeated string of `y`/`sudo password` prompts.
- Zero system package managers (no brew/apt/dnf/pacman/zypper/winget calls) in
  the packaged path. Python, ffmpeg, and whisper.cpp all ship inside the app.
- Windows and Linux installs need no admin/root privilege at all. macOS needs
  none beyond the one-time Gatekeeper click.
- The existing first-run setup wizard (model download with a real progress
  bar, pause/resume) is reused unchanged — it already does the "show the
  download" job this project asked for.
- Free tooling only (PyInstaller, Inno Setup, hdiutil, appimagetool/
  linuxdeploy) — no paid signing, no paid build services.

**Non-goals (explicitly deferred, not part of this spec)**
- Code signing / notarization on either OS (revisit if budget changes).
- Windows MSI/MSIX, Linux `.deb`/Flatpak/Snap — AppImage only for v1.
- Rewriting the multi-process architecture (tray/menubar + dictation engine +
  GUI window as separate processes) into a single in-process app. Approach A
  keeps that architecture; it only changes *how* those processes are launched
  once frozen.
- Changing anything about the source-install path (`install.sh`,
  `bootstrap.sh`/`.ps1`, `setup-desktop.py`). They keep working for
  contributors/Linux power users who want to build from source; the new
  installers are the path documented for regular users.

## 2. Current state (what this builds on)

- Five-ish separate entry-point scripts (`menubar.py`, `tray.py`, `app.py`,
  `dictation.py`, `engine.py`, `updates.py`, `overlay.py`) that call each other
  via `subprocess.Popen([sys.executable, "<script>.py", ...])` or
  `hotkeys.python_for_gui()`. Confirmed launch sites: `lib/menubar.py:439,900`,
  `lib/tray.py:294,364,658`, `lib/clipcontrol.py:54`, `lib/hotkeys.py:667,766`,
  `lib/overlay.py:216`, `lib/updates.py:275,318`, `lib/engine.py:180`,
  `lib/dictation.py:567,858`, `setup-desktop.py:65,179,193,360,367,376,563`.
- `desktop.py` already centralizes cross-platform path/behavior differences
  (`platform_name()`, `install_prefix()`, `roots()`, `process_options()`) —
  the right home for the new "how do I relaunch a sibling part of this app"
  logic, so per-file launch sites change to calls into it rather than each
  growing its own frozen/source branch.
- The setup wizard (`lib/app.py`, `show_download()` at `app.py:2197`) already
  downloads the 148MB Whisper model with a real progress bar and pause/resume.
  Nothing here changes that; the packaged app just reaches it on first run
  instead of via install.sh's `MODEL_URL` step.
- `updates.py` self-updates today by downloading a **source** tarball and
  running `setup-desktop.py --prefix <install_prefix>` — a mechanism specific
  to the venv/source install. A packaged install needs a different update
  mechanism (§4.6); the existing one is untouched and still used by
  source installs.
- Linux is the only platform currently doing real system-package-manager work
  (`install.sh`'s apt/dnf/pacman/zypper branches) — this whole branch, and the
  sudo prompts it causes, disappears from the packaged Linux path since
  ffmpeg/whisper.cpp/Python are bundled instead.

## 3. Target architecture

### 3.1 Runtime launcher abstraction (`lib/desktop.py`)

Add two functions to `desktop.py`, the existing home for platform
differences:

```python
def frozen_root() -> Path | None:
    """The directory holding this build's sibling executables, or None when
    running from source (python3 lib/whatever.py)."""
    if not getattr(sys, "frozen", False):
        return None
    appdir = os.environ.get("APPDIR")  # Set while running inside an AppImage.
    return Path(appdir) / "usr" / "bin" if appdir else Path(sys.executable).parent

def relaunch(entry: str, *args: str) -> list[str]:
    """argv to start another part of this app ("dictation", "app", "tray" /
    "menubar", "updates") — a sibling frozen executable when packaged, the
    matching script under the right interpreter when running from source."""
    root = frozen_root()
    if root is not None:
        suffix = ".exe" if platform_name() == "windows" else ""
        return [str(root / f"{entry}{suffix}"), *args]
    lib = Path(__file__).resolve().parent
    return [python_for_gui(), str(lib / f"{entry}.py"), *args]
```

Every cross-entry-point launch site listed in §2 changes to call
`desktop.relaunch("dictation", *flags)` etc. instead of building
`[sys.executable, str(HERE / "dictation.py"), *flags]` by hand. The source
fallback always uses `python_for_gui()` rather than raw `sys.executable` —
`python_for_gui()` already degrades to `sys.executable` on macOS/Linux (no
`pythonw.exe` to find there), so this is a strict improvement over today's
mixed usage (`tray.py` already calls `python_for_gui()` for every cross-launch;
`menubar.py`'s plain `sys.executable` calls are harmless only because macOS
has no console-flash problem to hide from). `python_for_gui()` becomes an
implementation detail `relaunch()` calls internally rather than something
call sites reach for directly.

**Self-relaunches need no change.** `engine.py --supervise` and
`dictation.py --worker` re-invoke *themselves* via
`[sys.executable, str(Path(__file__).resolve()), ...]`. In a frozen onedir
build, each entry point is its own executable and `sys.executable` already
points at itself — that line keeps working unmodified in both source and
frozen builds. Only cross-entry-point calls (tray/menubar starting the
dictation engine or the GUI window, `updates.py` starting itself as an
installer — that one's a self-relaunch too, unaffected) go through the new
helper.

### 3.2 Build: PyInstaller, onedir, one spec per entry point

- **onedir, not onefile.** Onefile self-extracts to a temp directory on every
  launch (slow startup for a tray app that may launch a fresh dictation
  process per hotkey press) and is far more likely to trip antivirus
  heuristics. Onedir starts instantly and is the standard choice for
  tray/background apps.
- One PyInstaller `.spec` file per OS, each with multiple `Analysis`/`EXE`
  blocks (one per entry point: `menubar`/`tray`, `app`, `dictation`, `engine`,
  `updates`) sharing one `COLLECT`, producing a single output folder with all
  five binaries side by side — the standard PyInstaller pattern for
  multi-process Python apps.
- PyObjC (macOS only) bundles fine under PyInstaller; the spec explicitly
  collects the `objc`, `AppKit`, and `Foundation` bridge submodules PyInstaller's
  static analysis can miss (a known PyObjC + PyInstaller gotcha), verified by
  actually launching the frozen `menubar` binary in CI, not just building it.

### 3.3 Bundled native binaries (ffmpeg, whisper.cpp)

- Fetch prebuilt, statically-linked ffmpeg binaries per OS (e.g. the
  BtbN/FFmpeg-Builds or evermeet.cx macOS builds — same checksum-pinning
  discipline `install.sh` already applies to whisper.cpp) during the CI build
  step, and add them to each spec's `binaries=[...]`.
- Build whisper.cpp once per OS in CI (already pinned to
  `WHISPER_VERSION`/`WHISPER_COMMIT` in `install.sh` — reuse that pin) and
  bundle the resulting binary the same way.
- `desktop.py`'s existing command-building functions
  (`recorder_command`, etc.) gain a "look next to `frozen_root()` first, else
  fall back to PATH" resolution order, so the same code runs whether the
  binary came from the bundle or (source installs) from Homebrew/apt/winget.

### 3.4 Per-OS installer wrapping

Branding throughout this section uses the app's real, already-existing
assets — nothing here is a placeholder to fill in later:
`hotkeys.APP_NAME = "Clipboard+"` is the canonical name (installer titles,
window titles, Start Menu entry, `.desktop` `Name=`); `assets/AppIcon.icns`
is the macOS app/volume icon; `assets/icon.ico` is the Windows installer and
`.exe` icon; `assets/icon-1024.png` / `icon-recording-1024.png` are the
source PNGs for the Linux `.desktop` icon and any other size AppImage
packaging needs.

- **macOS**: assemble the PyInstaller onedir output into a standard
  `Clipboard+.app` bundle (`Contents/MacOS/<entry>` for each binary,
  `Contents/Resources/AppIcon.icns` copied from `assets/AppIcon.icns`, an
  `Info.plist` with `CFBundleName`/`CFBundleDisplayName` = "Clipboard+",
  `CFBundleIconFile` = `AppIcon`, naming `menubar` as the launched binary,
  and setting `LSUIElement` so no Dock icon flashes before the real
  menu-bar status item appears). Wrap it in a plain `Clipboard+.dmg` (via
  `hdiutil create`, volume name "Clipboard+") with a background image
  showing "drag to Applications" — no installer wizard needed on macOS,
  that's the platform-idiomatic pattern. First launch needs one right-click
  → Open (unsigned); document this once, clearly, on the download page.
- **Windows**: an Inno Setup script (free, MIT-licensed, the same tool VS
  Code's user-scope installer uses), `AppName=Clipboard+`,
  `SetupIconFile=assets\icon.ico`, that installs to
  `%LOCALAPPDATA%\Programs\Clipboard+` — a per-user directory, so the
  installer **never triggers UAC**. It creates a Start Menu shortcut named
  "Clipboard+", an optional desktop shortcut (one checkbox, default off),
  registers the uninstaller in "Apps & features" as "Clipboard+", and runs
  the app after install. The unsigned `Clipboard+-Setup.exe` shows one
  SmartScreen "More info → Run anyway" the first time it's downloaded from a
  browser (Mark-of-the-Web) — unavoidable without a certificate, and a
  single click, not a repeated prompt.
- **Linux**: a single-file `Clipboard+-x86_64.AppImage` (via `linuxdeploy` +
  `appimagetool`), with a `clipboardplus.desktop` file (`Name=Clipboard+`,
  `Icon=clipboardplus`) and `assets/icon-1024.png` installed as that icon,
  for file-manager integration. No sudo, no distro package manager, no
  dependency resolution — replaces the apt/dnf/pacman/zypper branch of
  `install.sh` entirely for the packaged path. First run needs `chmod +x`
  (or a file manager's "Allow executing" checkbox) — the standard,
  well-understood AppImage step, not a security warning wall.

### 3.5 First-run responsibilities (mostly unchanged)

GNOME shortcut registration (`gsettings`, already root-free), login-item
registration (`hotkeys.set_login_item`, already per-user on all three OSes),
and the model-download progress UI in `app.py` all keep working as-is —
they just now run from the installed packaged app's first launch instead of
from `install.sh`. No design change; call sites move, logic doesn't.

### 3.6 Self-update for frozen installs

The existing `updates.py` mechanism (download a source tarball, run
`setup-desktop.py --prefix ...`) is source-install-specific and stays for
that path unchanged. Frozen installs need a different, simpler mechanism:

1. `updates.check()` (unchanged — it already compares versions against
   GitHub Releases) additionally records which release *asset* matches the
   running OS.
2. `apply_update()`, when `desktop.frozen_root()` is not `None`, downloads
   that OS's release asset (the same onedir build the installer contains,
   zipped) instead of the source tarball, extracts it next to the current
   install as `<install>-new`, and does an atomic directory rename to swap it
   in (matching the "replace the install directory, don't re-run an
   installer" pattern most self-updating desktop apps use), then relaunches
   via `desktop.relaunch(...)`.
3. On Windows, the running executable can't overwrite itself while running;
   the swap renames the *old* directory aside first (`<install>-old`,
   deleted on next successful launch) rather than deleting in place.

### 3.7 CI / release pipeline

A GitHub Actions matrix (`macos-latest`, `windows-latest`, `ubuntu-latest`)
triggered on version tags:
1. Fetch/build the pinned ffmpeg + whisper.cpp binaries for that OS (cached
   between runs by version pin).
2. `pyinstaller` with that OS's spec → onedir output.
3. Wrap per §3.4 (`hdiutil` / Inno Setup CLI / `appimagetool`).
4. Upload the resulting `.dmg` / `Setup.exe` / `.AppImage` plus a
   `SHA256SUMS` file as release assets on the GitHub Release for that tag.

This reuses the "3-OS matrix" the project already runs for tests/lint (per
existing CI), just adding a build+package+upload job gated on tags rather
than every push.

### 3.8 Docs

README's "Quick install" section is rewritten to lead with three download
links (pointing at the latest GitHub Release's assets) and the one-line
Gatekeeper/SmartScreen/chmod+x note per OS. The current curl/`iwr` one-liner
section moves under a "Build from source" heading further down, kept for
contributors — not deleted.

## 4. Files touched (concrete)

- `lib/desktop.py` — add `frozen_root()`, `relaunch()`; extend binary-path
  resolution in `recorder_command`/equivalent ffmpeg/whisper lookups.
- `lib/menubar.py`, `lib/tray.py`, `lib/hotkeys.py`, `lib/clipcontrol.py`,
  `lib/overlay.py`, `lib/updates.py` — swap manual
  `[sys.executable, ...]`/`python_for_gui()` cross-entry-point launches for
  `desktop.relaunch(...)`.
- `lib/updates.py` — add the frozen-install update path (§3.6) alongside the
  existing source-install path.
- New: one `.spec` file per OS under a new `packaging/` directory, an Inno
  Setup `.iss` script, a macOS `Info.plist` template, an AppImage
  `.desktop`/AppDir layout.
- New: `.github/workflows/release.yml` (or an added job in the existing
  workflow) for §3.7.
- `README.md` — §3.8.
- No changes to `install.sh`, `bootstrap.sh`/`.ps1`, `setup-desktop.py`, or
  any `lib/clip*.py` / `dictation.py` internals beyond the launch-site swaps
  above.

## 5. Testing / verification

- Existing unit/integration tests (682 tests, 90% coverage floor, 3-OS CI
  matrix) are unaffected — they exercise the library code directly, not the
  frozen binaries, and keep running exactly as today.
- New, separate verification for the packaging work itself (not unit tests):
  a CI job that actually **launches** each frozen entry point binary
  (`--version`/a smoke-test flag) after building, catching PyInstaller
  import-collection gaps (especially the PyObjC case in §3.2) before they
  reach a release asset.
- Manual pre-release checklist (documented in `packaging/README.md`):
  install each OS's artifact on a clean VM/user account with no dev tools
  installed, confirm zero terminal windows appear, confirm the
  Gatekeeper/SmartScreen/chmod+x step is the *only* prompt, run through
  first-run setup end to end (model download progress, GNOME shortcut,
  login item), then trigger the frozen self-update path once a newer
  tagged release exists.

## 6. Risks / mitigations

- **PyObjC under PyInstaller** — known to need explicit hidden-imports;
  mitigated by the CI smoke-launch in §5, not just a successful build.
- **Antivirus false positives on freshly-built, unsigned executables** —
  inherent to the "no signing budget" decision; onedir (not onefile, §3.2)
  measurably reduces this risk versus the alternative.
- **AppImage needs FUSE on some minimal/container Linux setups** — document
  the `--appimage-extract-and-run` fallback in the download page's Linux note.
- **Self-update atomicity on Windows** (§3.6 point 3) — mitigated by the
  rename-aside-then-swap pattern instead of in-place overwrite.
- **Bundle size** — roughly 150–300MB per OS (Python + ffmpeg + whisper.cpp +
  a Whisper model is *not* bundled, still downloaded on first run to keep
  the installer itself smaller). Acceptable given the "no sudo/no package
  manager" goal was explicitly prioritized over download size.

## 7. Rollout phases

Sequential; each phase is independently testable before the next starts.

1. **Launcher & path plumbing** — §3.1, and the binary-path resolution half
   of §3.3, done and tested from source (no packaging yet) on all three OSes.
2. **PyInstaller bundling** — §3.2 + §3.3's binary fetch/bundle, producing a
   working onedir build per OS, run manually (not yet wrapped in an
   installer).
3. **Native installer wrapping + first-run parity** — §3.4 + §3.5 checked
   against §5's manual checklist.
4. **CI/release automation + frozen self-update + docs** — §3.6, §3.7, §3.8.

## 8. Open items deferred, not blocking

- Revisit code signing if budget changes (would remove the one remaining
  per-OS security click).
- `.deb`/Flatpak, Windows MSI/MSIX — only if AppImage/Inno Setup prove
  insufficient for some audience segment.
