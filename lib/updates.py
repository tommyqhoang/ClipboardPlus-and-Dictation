"""Checking for app updates, and applying them when the user asks.

Once a day the app asks GitHub for its latest published release and compares the
version with its own. The request carries nothing but the URL: no clipboard
content, transcripts, keys or usage details are ever sent (telemetry stays as
it is). Updates are never installed on their own — the user starts them from
the tray menu or Settings, and the regular installer does the work again.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

import desktop
import dictation as d
import hotkeys

REPOSITORY = "tommyqhoang/ClipboardPlus-and-Dictation"
RELEASES_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
TARBALL_URL = f"https://github.com/{REPOSITORY}/archive/refs/tags/{{tag}}.tar.gz"
CHECK_SECONDS = 24 * 3600.0  # At most one check a day.
TIMEOUT = 10  # Seconds; a slow connection must not hold anything open.
MAX_RELEASE_BYTES = 50 * 1024 * 1024  # The source download is a few MB.
VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")


class UpdateError(Exception):
    """A download or installation step failed; messages are safe to show."""


def parse_version(text: str) -> tuple[int, int, int] | None:
    """The version in `text` (a tag, title or file name), or None when absent."""
    match = VERSION.search(text)
    return (int(match[1]), int(match[2]), int(match[3])) if match else None


def newer(latest: str, current: str) -> bool:
    """Whether `latest` is a real step past `current` (1.10.0 beats 1.9.4)."""
    found, running = parse_version(latest), parse_version(current)
    return found is not None and running is not None and found > running


# -- checking -----------------------------------------------------------


def _github(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": desktop.APP_VERSION})
    with urllib.request.build_opener(d.NoRedirect()).open(request, timeout=TIMEOUT) as response:
        data = response.read(1024 * 1024 + 1)
    if len(data) > 1024 * 1024:
        raise UpdateError("The update check response was too large.")
    parsed = json.loads(data)
    if not isinstance(parsed, dict):
        raise UpdateError("The update check response was not what GitHub usually sends.")
    return parsed


def release(fetch: Callable[[str], dict[str, Any]] = _github) -> dict[str, str] | None:
    """The newest published release, or None when there is none (or no network)."""
    try:
        info = fetch(RELEASES_URL)
        tag = str(info.get("tag_name", ""))
        url = str(info.get("tarball_url", "")) or TARBALL_URL.format(tag=tag)
        return {"version": tag.lstrip("v"), "tag": tag, "url": url} if tag else None
    except (OSError, ValueError, urllib.error.URLError):
        return None  # Offline or rate-limited: try again tomorrow.


def state_file(paths: d.Paths) -> Path:
    return paths.config.parent / "update.json"


def read_state(paths: d.Paths) -> dict[str, Any]:
    try:
        state = d.read_json(state_file(paths))
    except (d.DictationError, OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def write_state(paths: d.Paths, values: dict[str, Any]) -> None:
    d.private_dir(paths.config.parent)
    d.atomic(state_file(paths), json.dumps(values))


def due(paths: d.Paths, clock: Callable[[], float] = time.time) -> bool:
    """Whether a check would be useful now (at most once a day)."""
    checked = read_state(paths).get("checked")
    return not isinstance(checked, (int, float)) or clock() - float(checked) >= CHECK_SECONDS


def check(
    paths: d.Paths,
    force: bool = False,
    clock: Callable[[], float] = time.time,
    fetch: Callable[[str], dict[str, Any]] = _github,
) -> dict[str, str] | None:
    """A newer release to offer, or None. Remembers the answer for a day."""
    if not force and (not hotkeys.Preferences(paths).auto_updates() or not due(paths, clock)):
        return None
    found = release(fetch)
    write_state(paths, {"checked": clock(), "offered": found or {}})
    if found is not None and newer(found["version"], desktop.APP_VERSION):
        return found
    return None


# -- applying -----------------------------------------------------------


def _download(url: str, destination: Path) -> None:
    if not url.startswith("https://"):
        raise UpdateError("The update download address must be secure (https).")
    request = urllib.request.Request(url, headers={"User-Agent": desktop.APP_VERSION})
    try:
        with (
            urllib.request.build_opener().open(request, timeout=60) as response,
            destination.open("wb") as output,
        ):
            written = 0
            while True:
                block = response.read(256 * 1024)
                if not block:
                    break
                written += len(block)
                if written > MAX_RELEASE_BYTES:
                    raise UpdateError("The update download was larger than expected.")
                output.write(block)
    except urllib.error.URLError as exc:
        raise UpdateError("The update could not be downloaded. Check your connection.") from exc


def _extract(archive: Path, folder: Path) -> Path:
    """Unpack the release source; returns its single top-level directory."""
    folder.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(archive) as tar:
            safely = {name for name in tar.getnames() if not name.startswith(("/", ".."))}
            tar.extractall(folder, members=[m for m in tar.getmembers() if m.name in safely])
    except (tarfile.TarError, OSError) as exc:
        raise UpdateError("The downloaded update could not be unpacked.") from exc
    top = [entry for entry in folder.iterdir() if entry.is_dir()]
    if len(top) != 1 or not (top[0] / "setup-desktop.py").is_file():
        raise UpdateError("The downloaded update did not contain the installer.")
    return top[0]


def apply_update(paths: d.Paths, version: str, url: str) -> None:
    """Download and install a release, then leave the restart to its launcher.

    Runs as its own process (see main) so the tray stays responsive; a lock keeps
    a second click from racing the first.
    """
    d.private_dir(paths.runtime)
    lock = desktop.lock(paths.runtime / "update.lock")
    if lock is None:
        return
    log = paths.cache / "update.log"
    try:
        d.private_dir(paths.cache)
        with tempfile.TemporaryDirectory(prefix="update-", dir=paths.cache) as work:
            archive = Path(work) / "release.tar.gz"
            _download(url, archive)
            source = _extract(archive, Path(work))
            setup = subprocess.run(
                [sys.executable, str(source / "setup-desktop.py")],
                capture_output=True,
                text=True,
                timeout=1800,
                **desktop.process_options(),
            )
            log.write_text(setup.stdout + setup.stderr, encoding="utf-8", errors="replace")
            if setup.returncode:
                raise UpdateError("The update was downloaded but could not be installed.")
        write_state(paths, {**read_state(paths), "applied": version, "at": time.time()})
    except (OSError, subprocess.SubprocessError) as exc:
        with contextlib.suppress(OSError):
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text(str(exc), encoding="utf-8", errors="replace")
        raise UpdateError(f"The update did not complete: {exc}") from exc
    finally:
        os.close(lock)


def start_updater(version: str, url: str) -> None:
    """Run apply_update in its own process; the tray reports progress from state."""
    subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--apply",
            version,
            url,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **desktop.process_options(detached=True),
    )


def main() -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", nargs=2, metavar=("VERSION", "URL"))
    args = parser.parse_args()
    if not args.apply:
        parser.print_usage()
        return 2
    version, url = args.apply
    paths = d.Paths()
    try:
        apply_update(paths, version, url)
        config = d.Config(paths)
        d.notify(config, f"{hotkeys.APP_NAME} {version} is installed and ready.")
        return 0
    except (UpdateError, d.DictationError) as exc:
        try:
            d.notify(d.Config(paths), f"The update to {version} failed. {exc}")
        except d.DictationError:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
