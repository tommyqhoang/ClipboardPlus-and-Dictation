"""OS adapters. Command arguments stay separate from user-provided text."""

from __future__ import annotations

import errno
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


# Shown in crash reports and statistics; raise it with every release.
APP_VERSION = "1.6.8"


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


def python_for_gui() -> str:
    """pythonw on Windows so no console window flashes; sys.executable elsewhere."""
    python = Path(sys.executable)
    windowed = python.with_name("pythonw.exe")
    return str(windowed if windowed.exists() else python)


def overlay_python() -> str:
    """A Python that can draw windows: the app's private environment when installed
    (a desktop shortcut may run a system Python without Tk), otherwise `python_for_gui()`.
    Also `relaunch()`'s source-mode fallback, so every cross-process launch this app
    makes from source gets the same Tk-capable interpreter, not just the two call
    sites (`dictation.py`'s recording pill and app window) that originally needed it.
    """
    venv = install_prefix(Path(__file__)) / "share/whisper-dictation/venv"
    private = venv / "Scripts/pythonw.exe" if platform_name() == "windows" else venv / "bin/python"
    return str(private) if private.is_file() else python_for_gui()


def frozen_root() -> Path | None:
    """The directory holding this build's sibling executables, or None when running
    from source (`python3 lib/whatever.py`)."""
    if not getattr(sys, "frozen", False):
        return None
    appdir = os.environ.get("APPDIR")  # Set only while running inside an AppImage.
    return Path(appdir) / "usr" / "bin" if appdir else Path(sys.executable).parent


WINDOWS_MAIN_EXE = "Clipboard+"  # The packaged tray; keep in sync with the .spec/.iss.


def relaunch(entry: str, *args: str) -> list[str]:
    """argv to start another part of this app ("dictation", "app", "tray"/"menubar",
    "overlay", "engine", "updates", "clipservice") — a sibling frozen executable when
    packaged, the matching script under a window-capable interpreter when running
    from source."""
    root = frozen_root()
    if root is not None:
        if platform_name() == "windows":
            # The main program is "Clipboard+.exe", so Task Manager and the
            # firewall prompt show the product name, not "tray.exe".
            entry = WINDOWS_MAIN_EXE if entry == "tray" else entry
            entry += ".exe"
        return [str(root / entry), *args]
    lib = Path(__file__).resolve().parent
    return [overlay_python(), str(lib / f"{entry}.py"), *args]


def persistent_relaunch(entry: str, *args: str) -> list[str]:
    """Like relaunch(), but safe to write to disk for a later, separate process to
    run — a GNOME custom keybinding, a Linux autostart entry. Inside an AppImage,
    frozen_root() points into a mount that disappears once every running instance
    of it exits, so a persisted command built from relaunch() would go dead; this
    re-invokes the stable $APPIMAGE file with `entry` as its first argument
    instead, which AppRun dispatches to the matching sibling binary. Everywhere
    else (a normal frozen install, or running from source) relaunch()'s own path
    is already stable, so this is identical to it."""
    appimage = os.environ.get("APPIMAGE")
    if appimage:
        return [appimage, entry, *args]
    return relaunch(entry, *args)


def bundled_binary(name: str) -> Path | None:
    """A native binary this build ships (ffmpeg, whisper.cpp's server), or None to
    fall back to the system PATH (source installs, which rely on brew/apt/winget)."""
    root = frozen_root()
    if root is None:
        return None
    suffix = ".exe" if platform_name() == "windows" else ""
    candidate = root / f"{name}{suffix}"
    return candidate if candidate.is_file() else None


def macos_bundle() -> str:
    """This app's .app bundle path on macOS, for the login-item command: the
    source-install wrapper's WHISPER_DICTATION_BUNDLE env var, or (for a packaged
    build, which sets no such env var) derived from frozen_root() — two levels up
    from Contents/MacOS. Empty when neither applies (source install, not macOS)."""
    env = os.environ.get("WHISPER_DICTATION_BUNDLE", "")
    if env:
        return env
    if platform_name() != "macos":
        return ""
    root = frozen_root()
    return str(root.parent.parent) if root is not None else ""


def _windows_account() -> str:
    """DOMAIN\\user for the signed-in Windows account (what icacls expects)."""
    user = os.environ.get("USERNAME", "")
    domain = os.environ.get("USERDOMAIN", "")
    if not user:
        try:
            import getpass

            user = getpass.getuser()
        except (ImportError, KeyError, OSError):
            return ""
    return f"{domain}\\{user}" if domain else user


_restricted: set[str] = set()


def restrict_to_owner(path: Path) -> bool:
    """Make `path` (a file or folder) readable and writable by the current user only.

    POSIX: mode 0700 for folders, 0600 for files. Windows, where mode bits mean nothing:
    inheritance from the parent is cut and the account alone is granted full control
    (through `icacls`; a folder's grant is inherited by what is created inside it later).
    True when the restriction is in place; failing never raises, because a storage
    location that cannot be locked down is still better than losing the data.
    """
    try:
        if sys.platform != "win32":
            path.chmod(0o700 if path.is_dir() else 0o600)
            return True
        key = str(path)
        if key in _restricted:
            return True
        account = _windows_account()
        if not account:
            return False
        grant = f"{account}:(OI)(CI)F" if path.is_dir() else f"{account}:F"
        done = subprocess.run(
            ["icacls", key, "/inheritance:r", "/grant:r", grant],
            capture_output=True,
            timeout=15,
            check=False,
            **process_options(),
        )
        if done.returncode == 0:
            _restricted.add(key)
        return done.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def write_private(path: Path, data: str | bytes) -> None:
    """Atomically write `path` so it is owner-only from its first byte.

    The temporary file is created exclusively with mode 0600 (the umask can only make it
    stricter) and, on Windows, restricted before any content is written; it then replaces
    `path` by rename, so a reader sees the old file or the whole new one.
    """
    raw = data.encode("utf-8") if isinstance(data, str) else data
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{os.urandom(4).hex()}.part")
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            if sys.platform == "win32":
                restrict_to_owner(temporary)
            else:
                os.fchmod(stream.fileno(), 0o600)
            stream.write(raw)
        for attempt in range(20):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if sys.platform != "win32" or attempt == 19:
                    raise
                time.sleep(0.01)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _private_dir_ok(path: Path) -> bool:
    """Whether `path` is a real folder (not a link) that only this user can enter."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    if not stat.S_ISDIR(info.st_mode):
        return False
    if sys.platform == "win32":
        return True  # No owner or mode bits; the ACL is applied by whoever creates it.
    return info.st_uid == os.getuid() and not info.st_mode & 0o077


def make_private_dir(path: Path) -> bool:
    """Create `path` as a private folder without trusting a shared parent such as /tmp.

    The folder is made with mode 0700 and never with `exist_ok`, so an attacker's
    folder or link that was planted first is noticed, not adopted. An existing folder
    is accepted only when it is a real directory owned by this user and closed to
    everyone else (checked with lstat, which does not follow links). False means refuse.
    """
    for _ in range(4):
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            return _private_dir_ok(path)
        except FileNotFoundError:
            return False
        except OSError:
            return False
        if _private_dir_ok(path):
            return True
        # The umask stripped nothing we asked for, so this is unexpected: look again.
        try:
            os.rmdir(path)
        except OSError:
            return False
    return False


def _runtime_fallback(cache_root: Path) -> Path:
    """A private per-user runtime folder when the system provides no XDG_RUNTIME_DIR.

    A per-user temporary folder (macOS's $TMPDIR, or a TMPDIR the user set) is preferred
    to the shared /tmp. Whichever it is, the folder is verified; if something else owns
    that name, the runtime folder moves under the user's own cache folder instead.
    """
    name = f"dictation-{user_id()}"
    bases: list[Path] = []
    configured = os.environ.get("TMPDIR", "")
    if configured and os.path.isabs(configured):
        bases.append(Path(configured))
    bases.append(Path(tempfile.gettempdir()))
    for base in dict.fromkeys(bases):
        candidate = base / name
        if make_private_dir(candidate):
            return candidate
    try:
        cache_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError:
        return cache_root / "run"
    fallback = cache_root / "run"
    make_private_dir(fallback)
    return fallback


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
            home,  # Replaced below, once the cache folder is known.
        )
    else:
        defaults = (home / ".config/dictation", home / ".cache/dictation", home)
    # Explicit XDG overrides keep existing integrations and isolated tests usable.
    config = (
        Path(os.environ["XDG_CONFIG_HOME"]) / "dictation"
        if "XDG_CONFIG_HOME" in os.environ
        else defaults[0]
    )
    cache = (
        Path(os.environ["XDG_CACHE_HOME"]) / "dictation"
        if "XDG_CACHE_HOME" in os.environ
        else defaults[1]
    )
    if "XDG_RUNTIME_DIR" in os.environ:
        runtime = Path(os.environ["XDG_RUNTIME_DIR"]) / (
            "dictation" if sys.platform == "win32" else f"dictation-{user_id()}"
        )
    elif system == "windows":
        runtime = defaults[2]
    else:
        runtime = _runtime_fallback(cache)
    return config, cache, runtime


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


def recorder_command(
    values: dict[str, Any], listing: bool = False, device: str | None = None
) -> list[str]:
    backend = audio_backend(values)
    device = str(values["device"]) if device is None else device
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
    ffmpeg = str(values["ffmpeg"])
    if ffmpeg == "ffmpeg":  # The unmodified default; never override an explicit user path.
        bundled = bundled_binary("ffmpeg")
        if bundled is not None:
            ffmpeg = str(bundled)
    args = executable(ffmpeg) + ["-hide_banner"]
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
DESKTOP_ENTRY_ID = "clipboardplus"
FORMER_DESKTOP_ENTRY_IDS = ("whisper-dictation",)


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
