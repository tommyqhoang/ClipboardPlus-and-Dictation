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
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path, PurePosixPath, PureWindowsPath
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
MAX_EXTRACTED_BYTES = 200 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 10_000
VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")
ALLOWED_HOSTS = {"api.github.com", "github.com", "codeload.github.com"}


class UpdateError(d.DictationError):
    """A download or installation step failed; messages are safe to show."""


class HTTPSRedirect(urllib.request.HTTPRedirectHandler):
    """A release download must stay encrypted, and on GitHub's hosts, through every redirect."""

    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> urllib.request.Request | None:
        parsed = urllib.parse.urlsplit(newurl)
        if parsed.scheme != "https":
            raise UpdateError("The update download redirected to an insecure address.")
        if parsed.hostname not in ALLOWED_HOSTS:
            raise UpdateError("The update download redirected to an unexpected address.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


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
    """The newest release, or None when the repository has no published release."""
    try:
        info = fetch(RELEASES_URL)
    except urllib.error.HTTPError as exc:
        exc.close()
        if exc.code == 404:
            return None
        raise UpdateError("Couldn’t check for updates. Try again when you’re online.") from exc
    except UpdateError:
        raise
    except (OSError, ValueError) as exc:
        raise UpdateError("Couldn’t check for updates. Try again when you’re online.") from exc
    if not isinstance(info, dict):
        raise UpdateError("The update service returned an unexpected response.")
    tag = info.get("tag_name")
    if not isinstance(tag, str) or not re.fullmatch(r"v?\d+\.\d+\.\d+", tag):
        raise UpdateError("The update service returned an unrecognized version.")
    url = info.get("tarball_url") or TARBALL_URL.format(tag=tag)
    if not isinstance(url, str):
        raise UpdateError("The update service returned an invalid download address.")
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError as exc:
        raise UpdateError("The update service returned an invalid download address.") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname not in ALLOWED_HOSTS
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise UpdateError("The update service returned an invalid download address.")
    return {"version": tag.lstrip("v"), "tag": tag, "url": url}


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
    if not force:
        if not hotkeys.Preferences(paths).auto_updates():
            return None
        if not due(paths, clock):
            remembered = read_state(paths).get("offered")
            if (
                isinstance(remembered, dict)
                and isinstance(remembered.get("version"), str)
                and isinstance(remembered.get("tag"), str)
                and isinstance(remembered.get("url"), str)
                and newer(remembered["version"], desktop.APP_VERSION)
            ):
                return {
                    "version": remembered["version"],
                    "tag": remembered["tag"],
                    "url": remembered["url"],
                }
            return None
    found = release(fetch)
    previous = read_state(paths)
    progress: dict[str, Any] = (
        {key: previous[key] for key in ("status", "target", "started") if key in previous}
        if previous.get("status") == "installing"
        else {}
    )
    write_state(
        paths,
        {**progress, "checked": clock(), "offered": found or {}, "published": found is not None},
    )
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
            urllib.request.build_opener(HTTPSRedirect()).open(request, timeout=60) as response,
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
            members = tar.getmembers()
            if (
                len(members) > MAX_ARCHIVE_MEMBERS
                or sum(m.size for m in members) > MAX_EXTRACTED_BYTES
            ):
                raise UpdateError(
                    "The downloaded update contained too many files or too much data."
                )
            for member in members:
                name = PurePosixPath(member.name)
                if (
                    not member.name
                    or "\\" in member.name
                    or name.is_absolute()
                    or bool(PureWindowsPath(member.name).drive)
                    or ".." in name.parts
                    or not (member.isfile() or member.isdir())
                ):
                    raise UpdateError("The downloaded update contained an unsafe file path.")
            tar.extractall(folder, members=members)
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
        state = read_state(paths)
        state.pop("error", None)  # A new attempt supersedes an earlier failure.
        write_state(
            paths, {**state, "status": "installing", "target": version, "started": time.time()}
        )
        if desktop.frozen_root() is not None:
            # In-app updates for packaged installs aren't implemented yet — the
            # asset a release actually needs (a zipped onedir build), permission-
            # preserving extraction, and a safe per-OS directory swap (the AppImage
            # mount is read-only; Windows can't overwrite its own running exe) are
            # all still open work. Fail clearly rather than attempt a swap that
            # would corrupt the install. See docs/superpowers/specs/
            # 2026-09-28-packaged-installers-design.md §3.6 for the real design.
            raise UpdateError(
                "In-app updates aren't available for this build yet. Download the "
                "latest installer from the Releases page instead."
            )
        else:
            with tempfile.TemporaryDirectory(prefix="update-", dir=paths.cache) as work:
                archive = Path(work) / "release.tar.gz"
                _download(url, archive)
                source = _extract(archive, Path(work))
                # This module's own file is the running installation; a custom
                # --prefix install must self-update into the same place, not the
                # installer's default.
                install_prefix = desktop.install_prefix(Path(__file__).resolve())
                setup = subprocess.run(
                    [
                        sys.executable,
                        str(source / "setup-desktop.py"),
                        "--prefix",
                        str(install_prefix),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=1800,
                    **desktop.process_options(),
                )
                log.write_text(setup.stdout + setup.stderr, encoding="utf-8", errors="replace")
                if setup.returncode:
                    raise UpdateError("The update was downloaded but could not be installed.")
        write_state(
            paths,
            {**read_state(paths), "status": "installed", "applied": version, "at": time.time()},
        )
    except UpdateError as exc:
        with contextlib.suppress(OSError):
            write_state(paths, {**read_state(paths), "status": "failed", "error": str(exc)})
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        with contextlib.suppress(OSError):
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text(str(exc), encoding="utf-8", errors="replace")
        with contextlib.suppress(OSError):
            write_state(
                paths,
                {
                    **read_state(paths),
                    "status": "failed",
                    "error": "The update did not complete. Try again, or re-run the installer.",
                },
            )
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
