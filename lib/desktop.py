"""OS adapters. Command arguments stay separate from user-provided text."""

from __future__ import annotations

import errno
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


# Shown in crash reports and statistics; raise it with every release.
APP_VERSION = "1.2.1"


def platform_name() -> str:
    if sys.platform == "darwin":
        return "macos"
    if sys.platform == "win32":
        return "windows"
    return "linux"


def user_id() -> int:
    """Return a stable per-user suffix on Unix without breaking Windows typing."""
    return int(getattr(os, "getuid", os.getpid)())


def has_display() -> bool:
    """Whether windows and tray icons can be shown here: always on macOS and Windows;
    on Linux only inside a desktop session (not over SSH or in a container)."""
    return platform_name() != "linux" or bool(
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    )


def install_prefix(module: Path) -> Path:
    """The prefix an installed module lives under: …/lib/whisper-dictation, or …/lib."""
    folder = module.resolve().parent
    if folder.name == "whisper-dictation" and folder.parent.name == "lib":
        return folder.parents[1]
    return folder.parent


def roots() -> tuple[Path, Path, Path]:
    home = Path.home()
    system = platform_name()
    if system == "windows":
        base = (
            Path(os.environ.get("LOCALAPPDATA", str(home / "AppData/Local"))) / "WhisperDictation"
        )
        defaults = (base / "Config", base / "Cache", base / "Runtime")
    elif system == "macos":
        defaults = (
            home / "Library/Application Support/WhisperDictation",
            home / "Library/Caches/WhisperDictation",
            Path(tempfile.gettempdir()) / f"dictation-{user_id()}",
        )
    else:
        defaults = (
            home / ".config/dictation",
            home / ".cache/dictation",
            Path(tempfile.gettempdir()) / f"dictation-{user_id()}",
        )
    # Explicit XDG overrides keep existing integrations and isolated tests usable.
    return (
        Path(os.environ["XDG_CONFIG_HOME"]) / "dictation"
        if "XDG_CONFIG_HOME" in os.environ
        else defaults[0],
        Path(os.environ["XDG_CACHE_HOME"]) / "dictation"
        if "XDG_CACHE_HOME" in os.environ
        else defaults[1],
        Path(os.environ["XDG_RUNTIME_DIR"])
        / ("dictation" if sys.platform == "win32" else f"dictation-{user_id()}")
        if "XDG_RUNTIME_DIR" in os.environ
        else defaults[2],
    )


def lock(path: Path) -> int | None:
    if path.is_symlink():
        raise OSError("Lock files must not be symlinks.")
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if sys.platform == "win32":
            if not os.fstat(fd).st_size:
                os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
            return None
        raise
    return fd


def executable(command: str) -> list[str]:
    # Python hooks are useful for custom integrations and portable subprocess fixtures.
    return [sys.executable, command] if command.endswith(".py") else [command]


def available(command: str) -> bool:
    return Path(command).is_file() if command.endswith(".py") else shutil.which(command) is not None


def audio_backend(values: dict[str, Any]) -> str:
    backend = str(values["audio_backend"])
    if backend != "auto":
        return backend
    return {"linux": "alsa", "macos": "avfoundation", "windows": "dshow"}[platform_name()]


def recorder_command(values: dict[str, Any], listing: bool = False) -> list[str]:
    backend = audio_backend(values)
    device = str(values["device"])
    if backend == "alsa":
        args = executable(str(values["arecord"]))
        return args + (
            ["-L"]
            if listing
            else [
                "-q",
                "-D",
                device,
                "-t",
                "raw",
                "-f",
                "S16_LE",
                "-r",
                "16000",
                "-c",
                "1",
                "-d",
                str(values["max_seconds"]),
            ]
        )
    args = executable(str(values["ffmpeg"])) + ["-hide_banner"]
    if listing:
        return args + [
            "-f",
            backend,
            "-list_devices",
            "true",
            "-i",
            "" if backend == "avfoundation" else "dummy",
        ]
    source = f"none:{device}" if backend == "avfoundation" else f"audio={device}"
    return args + [
        "-loglevel",
        "error",
        "-f",
        backend,
        "-i",
        source,
        "-t",
        str(values["max_seconds"]),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-acodec",
        "pcm_s16le",
        "-f",
        "s16le",
        "-flush_packets",
        "1",
        "pipe:1",
    ]


def windows_image_script(path: Path) -> str:
    """PowerShell that puts a PNG file on the clipboard as an image."""
    quoted = str(path).replace("'", "''")
    return (
        "$ErrorActionPreference='Stop'; "
        "Add-Type -AssemblyName System.Windows.Forms,System.Drawing; "
        f"$i=[Drawing.Image]::FromFile('{quoted}'); "
        "[Windows.Forms.Clipboard]::SetImage($i); $i.Dispose()"
    )


def copy_image(values: dict[str, Any], path: Path, run: Any = subprocess.run) -> None:
    """Put a PNG file on the system clipboard as an image. Raises OSError on failure."""
    system = platform_name()
    try:
        if system == "macos":
            script = (
                "on run argv\n"
                "set the clipboard to (read (POSIX file (item 1 of argv)) as «class PNGf»)\n"
                "end run"
            )
            run(
                ["/usr/bin/osascript", "-e", script, str(path)],
                timeout=10,
                check=True,
                capture_output=True,
            )
        elif system == "windows":
            command = [
                str(values["powershell"]),
                "-NoProfile",
                "-NonInteractive",
                "-STA",
                "-Command",
                windows_image_script(path),
            ]
            run(command, timeout=15, check=True, capture_output=True, **process_options())
        else:
            backend = str(values.get("clipboard_backend", "auto"))
            use_wayland = backend == "wayland" or (
                backend == "auto" and bool(os.environ.get("WAYLAND_DISPLAY"))
            )
            command = (
                executable(str(values["wl_copy"])) + ["--type", "image/png"]
                if use_wayland
                else ["xclip", "-selection", "clipboard", "-in", "-t", "image/png"]
            )
            # xclip forks to keep ownership of the X11 selection. Its child keeps
            # inherited pipes open, so capturing output would wait until timeout.
            output = (
                {"capture_output": True}
                if use_wayland
                else {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
            )
            run(command, input=path.read_bytes(), timeout=5, check=True, **output)
    except subprocess.SubprocessError as exc:
        raise OSError("Could not copy the image to the clipboard.") from exc


def clipboard_command(values: dict[str, Any]) -> list[str]:
    backend = str(values["clipboard_backend"])
    if backend == "auto":
        backend = {
            "linux": "wayland" if os.environ.get("WAYLAND_DISPLAY") else "x11",
            "macos": "pbcopy",
            "windows": "powershell",
        }[platform_name()]
    if backend == "wayland":
        return executable(str(values["wl_copy"])) + ["--type", "text/plain;charset=utf-8"]
    if backend == "x11":
        return ["xclip", "-selection", "clipboard", "-in", "-t", "UTF8_STRING"]
    if backend == "pbcopy":
        return ["/usr/bin/pbcopy"]
    return [
        str(values["powershell"]),
        "-NoProfile",
        "-NonInteractive",
        "-STA",
        "-Command",
        "$ErrorActionPreference='Stop'; [Console]::InputEncoding=[Text.UTF8Encoding]::new(); "
        "Set-Clipboard -Value ([Console]::In.ReadToEnd())",
    ]


# Notifications carry the product name (hotkeys.APP_NAME; desktop cannot import it).
NOTIFY_NAME = "Clipboard+"
DESKTOP_ENTRY_ID = "whisper-dictation"


def notification_command(values: dict[str, Any]) -> list[str]:
    if values["notify"]:
        return executable(str(values["notify"])) + ["-a", NOTIFY_NAME, "-t", "4000", NOTIFY_NAME]
    if platform_name() == "linux":
        return [
            "notify-send",
            "-a",
            NOTIFY_NAME,
            "-h",
            f"string:desktop-entry:{DESKTOP_ENTRY_ID}",
            "-t",
            "4000",
            NOTIFY_NAME,
        ]
    if platform_name() == "macos":
        return [
            "/usr/bin/osascript",
            "-e",
            f'on run argv\ndisplay notification (item 1 of argv) with title "{NOTIFY_NAME}"\nend run',
        ]
    # A short-lived notification-area balloon; the message arrives over stdin.
    return [
        str(values["powershell"]),
        "-NoProfile",
        "-NonInteractive",
        "-STA",
        "-Command",
        "$ErrorActionPreference='Stop'; [Console]::InputEncoding=[Text.UTF8Encoding]::new(); "
        "Add-Type -AssemblyName System.Windows.Forms; Add-Type -AssemblyName System.Drawing; "
        "$n=New-Object System.Windows.Forms.NotifyIcon; "
        "$n.Icon=[System.Drawing.SystemIcons]::Information; $n.Visible=$true; "
        f"try {{$n.ShowBalloonTip(1000,'{NOTIFY_NAME}',[Console]::In.ReadToEnd(),"
        "[System.Windows.Forms.ToolTipIcon]::Info); Start-Sleep -Milliseconds 1100} "
        "finally {$n.Dispose()}",
    ]


def process_options(detached: bool = False) -> dict[str, Any]:
    if sys.platform == "win32":
        # Numeric constants avoid importing Windows-only subprocess names on Unix.
        return {"creationflags": 0x08000000 | (0x00000200 if detached else 0)}
    return {"start_new_session": True} if detached else {}
