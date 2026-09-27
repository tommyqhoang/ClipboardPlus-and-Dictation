"""Per-user desktop installation and launcher registration."""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import desktop
import dictation
import hotkeys
import telemetry

REPOSITORY = Path(__file__).resolve().parent
SHORTCUT_SCRIPT = """
$ErrorActionPreference='Stop'
[Console]::InputEncoding=[Text.UTF8Encoding]::new()
[Console]::OutputEncoding=[Text.UTF8Encoding]::new()
$p=[Console]::In.ReadToEnd() | ConvertFrom-Json
$programs=[Environment]::GetFolderPath('Programs')
$path=Join-Path $programs $p.name
$shell=New-Object -ComObject WScript.Shell
# Earlier names of this shortcut are removed, but only if they are ours.
foreach ($old in $p.old_names) {
    $oldPath=Join-Path $programs $old
    if (Test-Path -LiteralPath $oldPath) {
        $oldLink=$shell.CreateShortcut($oldPath)
        if (($oldLink.Arguments -eq $p.arguments) -or ($p.legacy_arguments -contains $oldLink.Arguments)) {
            Remove-Item -LiteralPath $oldPath
        }
    }
}
$link=$shell.CreateShortcut($path)
if ((Test-Path -LiteralPath $path) -and ($link.Arguments -ne $p.arguments) -and ($p.legacy_arguments -notcontains $link.Arguments)) {
    throw 'An unrelated shortcut already uses this name.'
}
if ($p.remove) {
    if (Test-Path -LiteralPath $path) {Remove-Item -LiteralPath $path}
} else {
    $link.TargetPath=$p.python
    $link.Arguments=$p.arguments
    $link.WorkingDirectory=$p.directory
    $link.Hotkey=''
    $link.Description=$p.description
    if ($p.icon -and (Test-Path -LiteralPath $p.icon)) {$link.IconLocation=$p.icon}
    $link.Save()
}
"""


def windows_shortcut(module: Path, python: Path | None = None, remove: bool = False) -> None:
    """Start Menu entry for the tray app. The tray registers the global shortcut."""
    if python is None:
        python = Path(sys.executable).with_name("pythonw.exe")
        if not python.exists():
            python = Path(sys.executable)
    payload = {
        "name": hotkeys.APP_NAME + ".lnk",
        "description": "Open " + hotkeys.APP_NAME,
        "old_names": [
            "Whisper Dictation.lnk",
            "Whisper Dictation & Clipboard+.lnk",
            "Clipboard+ Desktop.lnk",
        ],
        "python": str(python),
        "arguments": subprocess.list2cmdline([str(module)]),
        # Earlier versions pointed the shortcut at these modules.
        "legacy_arguments": [
            subprocess.list2cmdline([str(module.with_name(name))])
            for name in ("app.py", "dictation.py")
        ],
        "directory": str(module.parent),
        "icon": str(module.with_name("whisper-dictation.ico")),
        "remove": remove,
    }
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", SHORTCUT_SCRIPT],
        input=json.dumps(payload).encode("utf-8"),
        check=True,
        timeout=15,
    )


ICONS = (
    (REPOSITORY / "lib/whisper-dictation.png", "whisper-dictation.png"),
    (REPOSITORY / "lib/menubar-icon.png", "menubar-icon.png"),
    (REPOSITORY / "lib/menubar-recording.png", "menubar-recording.png"),
    (REPOSITORY / "lib/tray-recording.png", "tray-recording.png"),
    (REPOSITORY / "assets/icon.ico", "whisper-dictation.ico"),
)
MODULES = (
    "telemetry.py",
    "dictation.py",
    "desktop.py",
    "onboarding.py",
    "rewriting.py",
    "workflow.py",
    "app_service.py",
    "app.py",
    "hotkeys.py",
    "menubar.py",
    "tray.py",
    "clipboardplus.py",
    "clipstore.py",
    "clipwatch.py",
    "clipwatch_linux.py",
    "clipwatch_macos.py",
    "clipwatch_windows.py",
    "clipservice.py",
    "clipsync.py",
    "clipcontrol.py",
    "clipui.py",
    "overlay.py",
)
# The menu bar (macOS, PyObjC) and tray (Windows/Linux, pystray) apps run from a
# private environment so the system or Homebrew Python is never modified.
GUI_REQUIREMENTS = {
    "macos": ("pyobjc-framework-Cocoa==12.2.2", "Pillow==12.3.0"),
    "linux": ("pystray==0.19.5", "Pillow==12.3.0", "python-xlib==0.33"),
    "windows": ("pystray==0.19.5", "Pillow==12.3.0"),
}


TK_HINT = {
    "linux": "install python3-tk, python3-tkinter or tk from your package manager",
    "macos": "run: brew install python-tk",
    "windows": "reinstall Python with the tcl/tk option",
}


def gui_python(prefix: Path) -> tuple[Path, Path]:
    """The private environment's Python and its no-console variant."""
    venv = prefix / "share/whisper-dictation/venv"
    if desktop.platform_name() == "windows":
        return venv / "Scripts/python.exe", venv / "Scripts/pythonw.exe"
    return venv / "bin/python", venv / "bin/python"


def base_python(platform: str) -> str:
    """An interpreter able to build the private environment.

    The running Python is not always the one the distribution's Tk and GTK
    packages were installed for (Homebrew, pyenv or conda first on PATH), so on
    Linux also try the system interpreter. Falls back to the running Python so
    the failure is reported by the environment build itself.
    """
    candidates = [sys.executable]
    if platform == "linux":
        candidates += ["/usr/bin/python3", shutil.which("python3") or ""]
    needed = "tkinter, venv" + (", gi" if platform == "linux" else "")
    tried = set()
    for candidate in candidates:
        if not candidate or candidate in tried or not os.path.isfile(candidate):
            continue
        tried.add(candidate)
        probe = subprocess.run(
            [candidate, "-c", f"import {needed}"], capture_output=True, check=False
        )
        if probe.returncode == 0:
            return candidate
    return sys.executable


def probe_code(platform: str) -> str:
    """Imports that prove the private environment is complete (one that predates the
    clipboard history lacks some, fails this, and is rebuilt)."""
    if platform == "macos":
        return "import AppKit, PIL, tkinter"
    # Importing pystray connects to the display, which an SSH or TTY install lacks,
    # so only check that it is installed.
    extra = ", Xlib" if platform == "linux" else ""
    return f"import importlib.util, PIL, tkinter{extra}; assert importlib.util.find_spec('pystray')"


def gui_environment(prefix: Path) -> Path:
    """Create the private environment; returns its windowed Python."""
    platform = desktop.platform_name()
    venv = prefix / "share/whisper-dictation/venv"
    python, windowed = gui_python(prefix)
    probe = [str(python), "-c", probe_code(platform)]
    if python.exists() and subprocess.run(probe, capture_output=True, check=False).returncode == 0:
        return windowed
    print("Installing the menu bar/tray component (one time)...", flush=True)
    create = [base_python(platform), "-m", "venv", "--clear", str(venv)]
    if platform == "linux":
        create.insert(3, "--system-site-packages")  # Sees the distribution's GTK bindings.
    requirements = GUI_REQUIREMENTS[platform]
    try:
        subprocess.run(create, check=True)
        subprocess.run(
            [str(python), "-m", "pip", "install", "--disable-pip-version-check", "--quiet"]
            + list(requirements),
            check=True,
            timeout=600,
        )
        subprocess.run(probe, check=True, capture_output=True, text=True)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        lines = (getattr(exc, "stderr", None) or "").strip().splitlines()
        detail = f" ({lines[-1]})" if lines else ""
        raise dictation.DictationError(
            f"Could not install the menu bar/tray component{detail}. Check your internet "
            f"connection and that your Python includes Tk ({TK_HINT[platform]}), then retry."
        ) from exc
    return windowed


def stop_menubar(paths: dictation.Paths) -> None:
    """Ask a running menu bar app to quit so an upgrade takes effect."""
    lock = paths.runtime / "menubar.lock"
    for attempt in range(50):
        fd = desktop.lock(lock)
        if fd is not None:
            os.close(fd)
            return
        if attempt == 0:
            dictation.atomic(paths.runtime / "menubar-quit", "quit")
        time.sleep(0.1)


def install(prefix: Path, shortcut: bool = True) -> Path:
    paths = dictation.Paths()
    if dictation.busy(paths):
        raise dictation.DictationError("Finish the current dictation before installing.")
    module = prefix / "lib/dictation.py"
    bindir = prefix / "bin"
    # Shared folders (often symlinked by dotfiles): create them, never re-permission
    # them. The files written into them are owner-only.
    module.parent.mkdir(parents=True, exist_ok=True)
    bindir.mkdir(parents=True, exist_ok=True)
    for filename in MODULES:
        source = REPOSITORY / "lib" / filename
        dictation.atomic(module.parent / filename, source.read_text(encoding="utf-8"))
    for source, name in ICONS:
        if source.is_file():
            shutil.copyfile(source, module.parent / name)
    if desktop.platform_name() == "windows":
        launcher = bindir / "dictate-toggle.cmd"
        python = str(Path(sys.executable)).replace("%", "%%")
        dictation.atomic(
            launcher,
            f'@echo off\nsetlocal DisableDelayedExpansion\n"{python}" -X utf8 "%~dp0..\\lib\\dictation.py" %*\n',
        )
    else:
        launcher = bindir / "dictate-toggle"
        content = f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(module))} "$@"\n'
        dictation.atomic(launcher, content)
        launcher.chmod(0o755)
        # A Finder-friendly launcher for installs opened from the user Applications folder.
        double_click = bindir / f"{hotkeys.APP_NAME}.command"
        (bindir / "Whisper Dictation.command").unlink(missing_ok=True)
        dictation.atomic(
            double_click,
            f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(module.with_name('app.py')))}\n",
        )
        double_click.chmod(0o755)
    dictation.private_dir(paths.config.parent)
    if not paths.config.exists():
        values = dictation.DEFAULTS.copy()
        for key, binary in (("ffmpeg", "ffmpeg"), ("whisper_bin", "whisper-cli")):
            resolved = shutil.which(binary)
            if resolved:
                values[key] = resolved
        with paths.config.open("x", encoding="utf-8") as stream:
            json.dump(values, stream, indent=2)
    # The receipt scopes uninstall to known application files, never models or recordings.
    previous_shortcut = dictation.read_json(prefix / ".dictation-install.json").get(
        "shortcut", False
    )
    dictation.atomic(
        prefix / ".dictation-install.json",
        json.dumps(
            {"shortcut": previous_shortcut or (shortcut and desktop.platform_name() == "windows")}
        ),
    )
    if shortcut:
        install_app_launcher(prefix)
    return launcher


def app_bundle(prefix: Path) -> Path:
    # ~/Applications is writable without an administrator password and is indexed by macOS.
    return prefix.parent / f"Applications/{hotkeys.APP_NAME}.app"


def tray_command(prefix: Path, python: Path) -> list[str]:
    return [str(python), str(prefix / "lib/tray.py")]


def install_app_launcher(prefix: Path) -> None:
    module = prefix / "lib/app.py"
    preferences = hotkeys.Preferences(dictation.Paths())
    if desktop.platform_name() != "macos":
        python = gui_environment(prefix)
        hotkeys.set_login_item(preferences.open_at_login(), tray_command(prefix, python))
    if desktop.platform_name() == "windows":
        windows_shortcut(prefix / "lib/tray.py", python)
    elif desktop.platform_name() == "macos":
        bundle = app_bundle(prefix)
        for old_name in ("Whisper Dictation.app", "Clipboard+ Desktop.app"):
            legacy = bundle.with_name(old_name)
            legacy_info = legacy / "Contents/Info.plist"
            legacy_launcher = legacy / "Contents/MacOS/WhisperDictation"
            if (
                not bundle.exists()
                and legacy_info.is_file()
                and plistlib.loads(legacy_info.read_bytes()).get("CFBundleIdentifier")
                == "org.whisperdictation.desktop"
                and legacy_launcher.is_file()
                and str(prefix / "lib/menubar.py") in legacy_launcher.read_text()
            ):
                legacy.rename(bundle)
                break
        info = bundle / "Contents/Info.plist"
        if bundle.exists() and (
            not info.exists()
            or plistlib.loads(info.read_bytes()).get("CFBundleIdentifier")
            != "org.whisperdictation.desktop"
        ):
            raise dictation.DictationError(
                f"An unrelated app already uses the name of {hotkeys.APP_NAME}'s app bundle."
            )
        executable = bundle / "Contents/MacOS/WhisperDictation"
        executable.parent.mkdir(parents=True, exist_ok=True)
        python = gui_environment(prefix)
        dictation.atomic(
            info,
            plistlib.dumps(
                {
                    "CFBundleIdentifier": "org.whisperdictation.desktop",
                    "CFBundleName": hotkeys.APP_NAME,
                    "CFBundleDisplayName": hotkeys.APP_NAME,
                    "CFBundleExecutable": "WhisperDictation",
                    "CFBundlePackageType": "APPL",
                    "CFBundleIconFile": "AppIcon",
                    "CFBundleVersion": "3",
                    # A menu bar app: no Dock icon or app switcher entry.
                    "LSUIElement": True,
                    "NSMicrophoneUsageDescription": "Record speech only when you press Record.",
                }
            ).decode("utf-8"),
        )
        dictation.atomic(
            executable,
            # The interpreter lives in another bundle, so name ours explicitly.
            f"#!/bin/sh\nexport WHISPER_DICTATION_BUNDLE={shlex.quote(str(bundle))}\n"
            f"exec {shlex.quote(str(python))} "
            f"{shlex.quote(str(module.with_name('menubar.py')))}\n",
        )
        executable.chmod(0o755)
        icon = REPOSITORY / "assets/AppIcon.icns"
        if icon.is_file():
            resources = bundle / "Contents/Resources"
            resources.mkdir(exist_ok=True)
            shutil.copyfile(icon, resources / "AppIcon.icns")
        # Finder caches bundle icons; a new modification time refreshes it.
        os.utime(bundle)
        hotkeys.set_login_item(preferences.open_at_login(), ["/usr/bin/open", str(bundle)])
    else:
        entry = prefix / "share/applications/whisper-dictation.desktop"
        entry.parent.mkdir(parents=True, exist_ok=True)

        def quote(value: str) -> str:
            return (
                '"'
                + value.replace("\\", "\\\\")
                .replace('"', '\\"')
                .replace("`", "\\`")
                .replace("$", "\\$")
                .replace("%", "%%")
                + '"'
            )

        dictation.atomic(
            entry,
            f"[Desktop Entry]\nType=Application\nName={hotkeys.APP_NAME}\nComment=Dictate anywhere and keep your clipboard history\nExec={quote(str(python))} {quote(str(prefix / 'lib/tray.py'))}\nIcon={module.with_name('whisper-dictation.png')}\nTerminal=false\nCategories=Utility;Audio;\nStartupWMClass=WhisperDictation\n",
        )


LAUNCH_CHECK_SECONDS = 3.0


def launch(prefix: Path) -> None:
    module = prefix / "lib/app.py"
    if not module.is_file():
        raise dictation.DictationError("The desktop app is not installed at this location.")
    if desktop.platform_name() == "macos":
        stop_menubar(dictation.Paths())
        bundle = app_bundle(prefix)
        if not (bundle / "Contents/MacOS/WhisperDictation").is_file():
            raise dictation.DictationError(
                f"{hotkeys.APP_NAME} was installed, but its macOS app bundle is missing. "
                "Run the installer again."
            )
        subprocess.run(["/usr/bin/open", str(bundle)], check=True, timeout=15)
    else:
        stop_menubar(dictation.Paths())
        windowed = gui_python(prefix)[1]
        command = (
            tray_command(prefix, windowed)
            if windowed.exists()
            else [hotkeys.python_for_gui(), str(module)]  # Installed with --no-shortcut.
        )
        started = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **desktop.process_options(detached=True),
        )
        # A missing display or library ends it at once; say so instead of "opening…".
        try:
            code = started.wait(timeout=LAUNCH_CHECK_SECONDS)
        except subprocess.TimeoutExpired:
            return
        if code:
            raise dictation.DictationError(
                f"{hotkeys.APP_NAME} didn't start. Open it from your application menu, or "
                f"run: {shlex.join(command)}"
            )


def stop_clipboard_service(paths: dictation.Paths) -> None:
    """Ask the clipboard service to quit and clear its runtime files.

    The clipboard history itself is kept, like every other user file: it is removed
    only by "Delete all clipboard data" in Settings.
    """
    dictation.private_dir(paths.runtime)
    lock = paths.runtime / "clipservice.lock"
    for attempt in range(50):
        fd = desktop.lock(lock)
        if fd is not None:
            os.close(fd)
            break
        if attempt == 0:
            dictation.atomic(paths.runtime / "clip-quit", "quit")
        time.sleep(0.1)
    for name in ("clip-quit", "clip-status.json", "clip-sync-now", "clip-ignore.json"):
        (paths.runtime / name).unlink(missing_ok=True)


def uninstall(prefix: Path) -> None:
    receipt = prefix / ".dictation-install.json"
    if not receipt.is_file():
        raise dictation.DictationError("No desktop installation receipt found at this prefix.")
    if dictation.busy(dictation.Paths()):
        raise dictation.DictationError("Finish or cancel the active session before uninstalling.")
    if dictation.read_json(receipt).get("shortcut") and desktop.platform_name() == "windows":
        windows_shortcut(prefix / "lib/tray.py", gui_python(prefix)[1], remove=True)
    stop_clipboard_service(dictation.Paths())
    if desktop.platform_name() != "macos":
        stop_menubar(dictation.Paths())
        windowed = gui_python(prefix)[1]
        if windowed.exists():
            hotkeys.set_login_item(False, tray_command(prefix, windowed))
    for relative in (
        "lib/telemetry.py",
        "lib/dictation.py",
        "lib/desktop.py",
        "lib/onboarding.py",
        "lib/rewriting.py",
        "lib/workflow.py",
        "lib/app.py",
        "lib/app_service.py",
        "lib/hotkeys.py",
        "lib/menubar.py",
        "lib/tray.py",
        "lib/clipboardplus.py",
        "lib/clipstore.py",
        "lib/clipwatch.py",
        "lib/clipwatch_linux.py",
        "lib/clipwatch_macos.py",
        "lib/clipwatch_windows.py",
        "lib/clipservice.py",
        "lib/clipsync.py",
        "lib/clipcontrol.py",
        "lib/clipui.py",
        "lib/overlay.py",
        "lib/tray-recording.png",
        "lib/menubar-icon.png",
        "lib/menubar-recording.png",
        "lib/whisper-dictation.png",
        "lib/whisper-dictation.ico",
        "share/applications/whisper-dictation.desktop",
        "bin/dictate-toggle",
        "bin/dictate-toggle.cmd",
        "bin/Whisper Dictation.command",
        f"bin/{hotkeys.APP_NAME}.command",
    ):
        (prefix / relative).unlink(missing_ok=True)
    cache = prefix / "lib/__pycache__"
    for name in MODULES:
        for compiled in cache.glob(Path(name).stem + ".*.pyc"):
            compiled.unlink()
    if cache.is_dir() and not any(cache.iterdir()):
        cache.rmdir()
    if desktop.platform_name() == "linux":
        hotkeys.gnome_remove(prefix / "bin/dictate-toggle")
        hotkeys.gnome_remove(
            hotkeys.history_command(prefix / "lib", str(gui_python(prefix)[1])),
            path=hotkeys.GNOME_HISTORY_PATH,
        )
    receipt.unlink()
    venv = prefix / "share/whisper-dictation/venv"
    # Only a directory this installer created (it holds pyvenv.cfg) is removed.
    if desktop.platform_name() == "macos":
        # Quit the menu bar app before deleting the Python it runs from.
        stop_menubar(dictation.Paths())
    if (venv / "pyvenv.cfg").is_file():
        shutil.rmtree(venv)
    if desktop.platform_name() == "macos":
        agent = hotkeys.agent_path()
        # Only remove the login item that launches this installation's bundle.
        if agent.is_file() and str(app_bundle(prefix)) in plistlib.loads(agent.read_bytes()).get(
            "ProgramArguments", []
        ):
            agent.unlink()
        bundle = app_bundle(prefix)
        info = bundle / "Contents/Info.plist"
        if (
            info.is_file()
            and plistlib.loads(info.read_bytes()).get("CFBundleIdentifier")
            == "org.whisperdictation.desktop"
        ):
            (bundle / "Contents/MacOS/WhisperDictation").unlink(missing_ok=True)
            (bundle / "Contents/Resources/AppIcon.icns").unlink(missing_ok=True)
            info.unlink()
            # Remove only known, now-empty directories. Never recursively erase a bundle.
            for folder in (
                bundle / "Contents/MacOS",
                bundle / "Contents/Resources",
                bundle / "Contents",
                bundle,
            ):
                if folder.exists() and not any(folder.iterdir()):
                    folder.rmdir()


def main() -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", type=Path)
    parser.add_argument("--no-shortcut", action="store_true")
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--launch-only", action="store_true")
    args = parser.parse_args()
    default = (
        Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
        / "WhisperDictation/App"
        if desktop.platform_name() == "windows"
        else Path.home() / ".local"
    )
    prefix = (args.prefix or default).expanduser().resolve()
    try:
        if args.launch_only:
            launch(prefix)
        elif args.uninstall:
            uninstall(prefix)
            telemetry.event("app_uninstalled", wait=True)
            print(
                "Removed desktop application files. Settings, models, transcripts and clipboard history were retained."
            )
        else:
            launcher = install(prefix, not args.no_shortcut)
            # How many installs, where: anonymous, and off with DO_NOT_TRACK=1.
            telemetry.set_component("installer")
            telemetry.event(
                "app_installed",
                wait=True,
                method="quick" if os.environ.get("DICTATION_QUICK_INSTALL") else "manual",
            )
            # Normal installation should be immediately usable. On a first run the menu
            # bar app opens the setup window; on later runs it opens Settings.
            if not args.no_shortcut:
                launch(prefix)
            if not os.environ.get("DICTATION_QUICK_INSTALL"):  # It says what happens next.
                print(f"Installed: {launcher}")
                print(f"Settings: {dictation.Paths().config}")
                print(f"Opened {hotkeys.APP_NAME} to finish setup.")
        return 0
    except (OSError, dictation.DictationError, subprocess.SubprocessError) as exc:
        print(f"Setup did not complete: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
