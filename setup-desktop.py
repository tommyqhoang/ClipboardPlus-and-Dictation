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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import desktop
import dictation

SHORTCUT_SCRIPT = """
$ErrorActionPreference='Stop'
[Console]::InputEncoding=[Text.UTF8Encoding]::new()
[Console]::OutputEncoding=[Text.UTF8Encoding]::new()
$p=[Console]::In.ReadToEnd() | ConvertFrom-Json
$path=Join-Path ([Environment]::GetFolderPath('Programs')) 'Whisper Dictation.lnk'
$shell=New-Object -ComObject WScript.Shell
$link=$shell.CreateShortcut($path)
if ((Test-Path -LiteralPath $path) -and ($link.Arguments -ne $p.arguments) -and ($link.Arguments -ne $p.legacy_arguments)) {
    throw 'An unrelated shortcut already uses this name.'
}
if ($p.remove) {
    if (Test-Path -LiteralPath $path) {Remove-Item -LiteralPath $path}
} else {
    $link.TargetPath=$p.python
    $link.Arguments=$p.arguments
    $link.WorkingDirectory=$p.directory
    $link.Hotkey='CTRL+ALT+D'
    $link.Description='Open Whisper Dictation'
    $link.Save()
}
"""


def windows_shortcut(module: Path, remove: bool = False) -> None:
    python = Path(sys.executable).with_name("pythonw.exe")
    if not python.exists():
        python = Path(sys.executable)
    payload = {
        "python": str(python),
        "arguments": subprocess.list2cmdline([str(module)]),
        "legacy_arguments": subprocess.list2cmdline([str(module.with_name("dictation.py"))]),
        "directory": str(module.parent),
        "remove": remove,
    }
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", SHORTCUT_SCRIPT],
        input=json.dumps(payload).encode("utf-8"),
        check=True,
        timeout=15,
    )


def install(prefix: Path, shortcut: bool = True) -> Path:
    paths = dictation.Paths()
    if dictation.busy(paths):
        raise dictation.DictationError("Finish the current dictation before installing.")
    module = prefix / "lib/dictation.py"
    bindir = prefix / "bin"
    dictation.private_dir(module.parent)
    dictation.private_dir(bindir)
    for filename in (
        "dictation.py",
        "desktop.py",
        "onboarding.py",
        "rewriting.py",
        "workflow.py",
        "app_service.py",
        "app.py",
    ):
        source = Path(__file__).resolve().parent / "lib" / filename
        dictation.atomic(module.parent / filename, source.read_text(encoding="utf-8"))
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
        double_click = bindir / "Whisper Dictation.command"
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
    return prefix.parent / "Applications/Whisper Dictation.app"


def install_app_launcher(prefix: Path) -> None:
    module = prefix / "lib/app.py"
    if desktop.platform_name() == "windows":
        windows_shortcut(module)
    elif desktop.platform_name() == "macos":
        bundle = app_bundle(prefix)
        info = bundle / "Contents/Info.plist"
        if bundle.exists() and (
            not info.exists()
            or plistlib.loads(info.read_bytes()).get("CFBundleIdentifier")
            != "org.whisperdictation.desktop"
        ):
            raise dictation.DictationError(
                "An unrelated app already uses the Whisper Dictation name."
            )
        executable = bundle / "Contents/MacOS/WhisperDictation"
        executable.parent.mkdir(parents=True, exist_ok=True)
        dictation.atomic(
            info,
            plistlib.dumps(
                {
                    "CFBundleIdentifier": "org.whisperdictation.desktop",
                    "CFBundleName": "Whisper Dictation",
                    "CFBundleDisplayName": "Whisper Dictation",
                    "CFBundleExecutable": "WhisperDictation",
                    "CFBundlePackageType": "APPL",
                    "CFBundleVersion": "1",
                    "NSMicrophoneUsageDescription": "Record speech only when you press Record.",
                }
            ).decode("utf-8"),
        )
        dictation.atomic(
            executable,
            f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(module))}\n",
        )
        executable.chmod(0o755)
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
            f"[Desktop Entry]\nType=Application\nName=Whisper Dictation\nComment=Speak, stop, and paste\nExec={quote(sys.executable)} {quote(str(module))}\nTerminal=false\nCategories=Utility;Audio;\n",
        )


def launch(prefix: Path) -> None:
    module = prefix / "lib/app.py"
    if not module.is_file():
        raise dictation.DictationError("The desktop app is not installed at this location.")
    if desktop.platform_name() == "macos":
        subprocess.run(["/usr/bin/open", str(app_bundle(prefix))], check=True, timeout=15)
    else:
        python = Path(sys.executable)
        if desktop.platform_name() == "windows" and python.with_name("pythonw.exe").exists():
            python = python.with_name("pythonw.exe")
        subprocess.Popen(
            [str(python), str(module)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **desktop.process_options(detached=True),
        )


def uninstall(prefix: Path) -> None:
    receipt = prefix / ".dictation-install.json"
    if not receipt.is_file():
        raise dictation.DictationError("No desktop installation receipt found at this prefix.")
    if dictation.busy(dictation.Paths()):
        raise dictation.DictationError("Finish or cancel the active session before uninstalling.")
    if dictation.read_json(receipt).get("shortcut") and desktop.platform_name() == "windows":
        windows_shortcut(prefix / "lib/app.py", remove=True)
    for relative in (
        "lib/dictation.py",
        "lib/desktop.py",
        "lib/onboarding.py",
        "lib/rewriting.py",
        "lib/workflow.py",
        "lib/app.py",
        "lib/app_service.py",
        "share/applications/whisper-dictation.desktop",
        "bin/dictate-toggle",
        "bin/dictate-toggle.cmd",
        "bin/Whisper Dictation.command",
    ):
        (prefix / relative).unlink(missing_ok=True)
    receipt.unlink()
    if desktop.platform_name() == "macos":
        bundle = app_bundle(prefix)
        info = bundle / "Contents/Info.plist"
        if (
            info.is_file()
            and plistlib.loads(info.read_bytes()).get("CFBundleIdentifier")
            == "org.whisperdictation.desktop"
        ):
            (bundle / "Contents/MacOS/WhisperDictation").unlink(missing_ok=True)
            info.unlink()
            # Remove only known, now-empty directories. Never recursively erase a bundle.
            for folder in (bundle / "Contents/MacOS", bundle / "Contents", bundle):
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
            print(
                "Removed desktop application files. Settings, models and transcripts were retained."
            )
        else:
            launcher = install(prefix, not args.no_shortcut)
            print(f"Installed: {launcher}")
            print(f"Settings: {dictation.Paths().config}")
            print(
                "Open Whisper Dictation from Applications or your application menu to finish setup."
            )
        return 0
    except (OSError, dictation.DictationError, subprocess.SubprocessError) as exc:
        print(f"Setup did not complete: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
