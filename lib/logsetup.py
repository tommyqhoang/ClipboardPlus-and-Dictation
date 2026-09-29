"""Small shared logging setup: one rotating, owner-only log file per component.

    import logsetup
    log = logsetup.get_logger("clipservice")
    log.warning("something went wrong: %s", exc)

Files live in `log_dir()` (a `logs` folder in the app's cache folder, 0700), are capped
at 1 MB with three older copies kept, and are created readable by the owner only.
Logging never raises: when the folder cannot be written, the logger quietly drops
its lines. Do not log clipboard contents, transcripts, keys or paths of user files;
the log is for the app's own failures. Standard library only.
"""

from __future__ import annotations

import io
import logging
import logging.handlers
import os
import re
import sys
from pathlib import Path

import desktop

MAX_BYTES = 1_000_000
BACKUPS = 3
_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_NAME = re.compile(r"[A-Za-z0-9_.-]{1,60}")


def log_dir() -> Path:
    """The folder that holds the log files (created if needed, owner-only)."""
    folder = desktop.roots()[1] / "logs"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    desktop.restrict_to_owner(folder)
    return folder


class _PrivateRotatingHandler(logging.handlers.RotatingFileHandler):
    """A rotating file whose every (re)created file is 0600 from the first byte."""

    def _open(self) -> io.TextIOWrapper:
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.baseFilename, flags, 0o600)
        if sys.platform != "win32":
            os.fchmod(descriptor, 0o600)
        return io.TextIOWrapper(
            os.fdopen(descriptor, "ab"), encoding=self.encoding or "utf-8", errors=self.errors
        )


def get_logger(name: str) -> logging.Logger:
    """The logger for a component, writing to `<log_dir()>/<name>.log` (safe to call again)."""
    if not _NAME.fullmatch(name):
        raise ValueError("A log name is letters, digits, dots, dashes and underscores.")
    logger = logging.getLogger(f"clipboardplus.{name}")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        handler: logging.Handler = _PrivateRotatingHandler(
            log_dir() / f"{name}.log", maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter(_FORMAT))
    except OSError:
        handler = logging.NullHandler()
    logger.addHandler(handler)
    return logger


def open_stream(name: str) -> io.BufferedWriter | None:
    """An append-only, owner-only file for a child process's stdout and stderr.

    Handed to `subprocess.Popen(stdout=..., stderr=...)`, so a crash that never reached
    any logging (an import error, a traceback) is still written down. A handler cannot
    do that, and a pipe would break the child when its parent quits. The file is rotated
    here, when it is opened, once it has passed `MAX_BYTES`. None when it cannot be opened.
    """
    if not _NAME.fullmatch(name):
        raise ValueError("A log name is letters, digits, dots, dashes and underscores.")
    try:
        path = log_dir() / f"{name}.log"
        try:
            oversize = path.stat().st_size >= MAX_BYTES
        except OSError:
            oversize = False
        if oversize:
            for number in range(BACKUPS - 1, 0, -1):
                older = path.with_name(f"{path.name}.{number}")
                if older.exists():
                    os.replace(older, path.with_name(f"{path.name}.{number + 1}"))
            os.replace(path, path.with_name(f"{path.name}.1"))
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        if sys.platform != "win32":
            os.fchmod(descriptor, 0o600)
        return os.fdopen(descriptor, "ab")
    except OSError:
        return None
