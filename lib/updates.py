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
import hashlib
import hmac
import json
import os
import platform as host
import re
import shutil
import stat
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
RELEASE_TAG_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/tags/{{tag}}"
TARBALL_URL = f"https://github.com/{REPOSITORY}/archive/refs/tags/{{tag}}.tar.gz"
# release.yml publishes SHA256SUMS ("<hex>  <name>" per line) plus a `<asset>.sha256`
# sidecar per installer (packaging/checksum.py); the source archive is listed in the
# manifest as either this uploaded asset or GitHub's own `<tag>.tar.gz`.
MANIFEST_NAME = "SHA256SUMS"
SOURCE_ASSET = "clipboardplus-source-{tag}.tar.gz"
MAX_ASSET_BYTES = 1024 * 1024 * 1024  # A packaged installer is a few hundred MB.
CHECK_SECONDS = 24 * 3600.0  # At most one check a day.
TIMEOUT = 10  # Seconds; a slow connection must not hold anything open.
MAX_RELEASE_BYTES = 50 * 1024 * 1024  # The source download is a few MB.
MAX_EXTRACTED_BYTES = 200 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 10_000
VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")
ALLOWED_HOSTS = {
    "api.github.com",
    "github.com",
    "codeload.github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
}


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
    if not isinstance(url, str) or not _trusted_url(url):
        raise UpdateError("The update service returned an invalid download address.")
    return {"version": tag.lstrip("v"), "tag": tag, "url": url}


def _trusted_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname in ALLOWED_HOSTS
        and not parsed.username
        and not parsed.password
        and not parsed.fragment
    )


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


def _download(url: str, destination: Path, limit: int | None = None) -> None:
    limit = MAX_RELEASE_BYTES if limit is None else limit
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
                if written > limit:
                    raise UpdateError("The update download was larger than expected.")
                output.write(block)
    except urllib.error.URLError as exc:
        raise UpdateError("The update could not be downloaded. Check your connection.") from exc


def _fetch_text(url: str) -> str:
    """A small text asset (a checksum file), fetched over the same guarded channel."""
    if not url.startswith("https://"):
        raise UpdateError("The update download address must be secure (https).")
    request = urllib.request.Request(url, headers={"User-Agent": desktop.APP_VERSION})
    try:
        with urllib.request.build_opener(HTTPSRedirect()).open(request, timeout=TIMEOUT) as reply:
            data: bytes = reply.read(1024 * 1024 + 1)
    except (urllib.error.URLError, OSError) as exc:
        raise UpdateError("The update's checksum could not be downloaded.") from exc
    if len(data) > 1024 * 1024:
        raise UpdateError("The update's checksum file was too large.")
    return data.decode("utf-8", errors="replace")


# -- integrity ----------------------------------------------------------


def _release_assets(tag: str, fetch: Callable[[str], dict[str, Any]] = _github) -> dict[str, str]:
    """The published assets (name -> https URL) of exactly the release tagged `tag`."""
    info = fetch(RELEASE_TAG_URL.format(tag=tag))
    if not isinstance(info, dict) or info.get("tag_name") != tag:
        raise UpdateError("The release could not be matched to its exact tag.")
    assets: dict[str, str] = {}
    for asset in info.get("assets") or []:
        if not isinstance(asset, dict):
            continue
        name, link = asset.get("name"), asset.get("browser_download_url")
        if isinstance(name, str) and isinstance(link, str) and _trusted_url(link):
            assets[name] = link
    return assets


def resolve_tag(
    version: str, fetch: Callable[[str], dict[str, Any]] = _github
) -> tuple[str, dict[str, str]]:
    """The exact tag (v1.2.3 or 1.2.3) that published `version`, and its assets."""
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise UpdateError("The update has an unrecognized version.")
    for tag in (f"v{version}", version):
        try:
            return tag, _release_assets(tag, fetch)
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code != 404:
                raise UpdateError("Couldn’t look up the release. Try again later.") from exc
        except UpdateError:
            raise
        except (OSError, ValueError) as exc:
            raise UpdateError("Couldn’t look up the release. Try again later.") from exc
    raise UpdateError(f"Release {version} was not found on GitHub.")


def parse_checksums(text: str) -> dict[str, str]:
    """`<sha256>  <name>` lines (a sidecar or a SHA256SUMS manifest) as {name: hex}."""
    found: dict[str, str] = {}
    for line in text.splitlines():
        match = re.fullmatch(r"([0-9a-fA-F]{64})\s+\*?(.+)", line.strip())
        if match:
            found[match[2].strip()] = match[1].lower()
    return found


def _sanitized(name: str) -> str:
    # GitHub rewrites unusual characters in uploaded names (Clipboard+ -> Clipboard.).
    return re.sub(r"[^A-Za-z0-9._-]", ".", name)


def expected_checksum(
    name: str, assets: dict[str, str], fetch_text: Callable[[str], str] = _fetch_text
) -> str:
    """The published SHA-256 for `name`; refuses (raises) when none, or conflicting ones."""
    digests: set[str] = set()
    sources = [assets.get(MANIFEST_NAME), assets.get(name + ".sha256")]
    for source in sources:
        if not source:
            continue
        entries = parse_checksums(fetch_text(source))
        for candidate, digest in entries.items():
            if candidate == name or _sanitized(candidate) == _sanitized(name):
                digests.add(digest)
    if not digests:
        raise UpdateError(
            "This release has no published checksum for the download, so it was not installed."
        )
    if len(digests) > 1:
        raise UpdateError("The release's checksum files disagree, so nothing was installed.")
    return digests.pop()


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_checksum(path: Path, expected: str) -> None:
    if not hmac.compare_digest(sha256_of(path), expected.lower()):
        raise UpdateError(
            "The downloaded update did not match its published checksum, so it was not installed."
        )


# -- unpacking ----------------------------------------------------------


def _safe_extract(tar: tarfile.TarFile, folder: Path, members: list[tarfile.TarInfo]) -> None:
    if getattr(tarfile, "data_filter", None) is not None:  # 3.12+ and the 3.8-3.11 backports
        tar.extractall(folder, members=members, filter="data")
        return
    # No `data` filter: members are already limited to files and directories with
    # no absolute or `..` paths; here modes are normalized, ownership is not
    # restored, and every destination must resolve inside the folder.
    root = folder.resolve()
    for member in members:
        target = (folder / member.name).resolve()
        if target != root and root not in target.parents:
            raise UpdateError("The downloaded update contained an unsafe file path.")
        if member.isdir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        source = tar.extractfile(member)
        if source is None:
            raise UpdateError("The downloaded update contained an unsafe file path.")
        with source, target.open("wb") as output:
            shutil.copyfileobj(source, output)
        executable = bool(member.mode & stat.S_IXUSR)
        target.chmod(0o755 if executable else 0o644)


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
            _safe_extract(tar, folder, members)
    except (tarfile.TarError, OSError) as exc:
        raise UpdateError("The downloaded update could not be unpacked.") from exc
    top = [entry for entry in folder.iterdir() if entry.is_dir()]
    if len(top) != 1 or not (top[0] / "setup-desktop.py").is_file():
        raise UpdateError("The downloaded update did not contain the installer.")
    return top[0]


# -- atomic swap with rollback -----------------------------------------


def app_folder(module: Path) -> Path | None:
    """The app's own install folder (…/lib/whisper-dictation), or None for a legacy layout."""
    folder = module.resolve().parent
    return folder if folder.name == "whisper-dictation" and folder.parent.name == "lib" else None


def clean_stale_backups(app: Path) -> None:
    """Backups and half-swapped folders left behind by an interrupted update."""
    for stale in [*app.parent.glob(app.name + ".bak-*"), *app.parent.glob(app.name + ".failed-*")]:
        shutil.rmtree(stale, ignore_errors=True)


def install_with_rollback(app: Path | None, install: Callable[[], None]) -> None:
    """Run `install`, restoring the previous app folder if it fails or times out.

    The current folder is copied to a sibling first, so the running app is never
    left without a working install; on success the backup is deleted.
    """
    if app is None or not app.is_dir():
        install()
        return
    clean_stale_backups(app)
    backup = app.with_name(f"{app.name}.bak-{os.getpid()}")
    shutil.copytree(app, backup, symlinks=True)
    try:
        install()
    except BaseException:
        _restore(app, backup)
        raise
    shutil.rmtree(backup, ignore_errors=True)


def _restore(app: Path, backup: Path) -> None:
    broken = app.with_name(f"{app.name}.failed-{os.getpid()}")
    try:
        if app.exists():
            os.replace(app, broken)
        os.replace(backup, app)
    except OSError:
        # Swapping directories failed (a locked file on Windows): copy the old files back.
        shutil.copytree(backup, app, symlinks=True, dirs_exist_ok=True)
        shutil.rmtree(backup, ignore_errors=True)
    shutil.rmtree(broken, ignore_errors=True)


def _install_source(
    paths: d.Paths, tag: str, assets: dict[str, str], log: Path, fetch_text: Callable[[str], str]
) -> str:
    name = SOURCE_ASSET.format(tag=tag)
    if name in assets:
        url = assets[name]
    else:
        name, url = f"{tag}.tar.gz", TARBALL_URL.format(tag=tag)
    expected = expected_checksum(name, assets, fetch_text)
    with tempfile.TemporaryDirectory(prefix="update-", dir=paths.cache) as work:
        archive = Path(work) / "release.tar.gz"
        _download(url, archive)
        verify_checksum(archive, expected)  # Nothing is unpacked, let alone run, before this.
        source = _extract(archive, Path(work))
        # This module's own file is the running installation; a custom
        # --prefix install must self-update into the same place, not the
        # installer's default.
        module = Path(__file__).resolve()
        install_prefix = desktop.install_prefix(module)

        def run_setup() -> None:
            setup = subprocess.run(
                [sys.executable, str(source / "setup-desktop.py"), "--prefix", str(install_prefix)],
                capture_output=True,
                text=True,
                timeout=1800,
                **desktop.process_options(),
            )
            log.write_text(setup.stdout + setup.stderr, encoding="utf-8", errors="replace")
            if setup.returncode:
                raise UpdateError("The update was downloaded but could not be installed.")

        install_with_rollback(app_folder(module), run_setup)
    return f"{hotkeys.APP_NAME} {tag.lstrip('v')} is installed and ready."


# -- packaged (frozen) builds ------------------------------------------


def platform_asset(
    assets: dict[str, str], system: str | None = None, machine: str | None = None
) -> str:
    """The name of this platform's installer among a release's assets."""
    system = system or sys.platform
    machine = (machine or host.machine()).lower()
    names = sorted(n for n in assets if not n.endswith(".sha256") and n != MANIFEST_NAME)
    if system.startswith("linux"):
        if machine not in ("x86_64", "amd64"):
            raise UpdateError("The Linux download is for x86_64 computers only.")
        found = [n for n in names if n.lower().endswith(".appimage") and "x86_64" in n.lower()]
    elif system == "darwin":
        architecture = {
            "arm64": "arm64",
            "aarch64": "arm64",
            "x86_64": "x86_64",
            "amd64": "x86_64",
        }.get(machine)
        if architecture is None:
            raise UpdateError("Mac downloads are available for Intel and Apple silicon only.")
        found = [n for n in names if n.lower().endswith(f"-macos-{architecture}.dmg")]
    elif system in ("win32", "cygwin"):
        found = [n for n in names if n.lower().endswith("setup.exe")]
    else:
        raise UpdateError("In-app updates aren't available on this system.")
    if not found:
        raise UpdateError("This release has no download for this computer yet.")
    return found[0]


def downloads_folder(paths: d.Paths) -> Path:
    folder = Path.home() / "Downloads"
    return folder if folder.is_dir() else paths.cache / "updates"


def _fetch_verified(url: str, name: str, expected: str, folder: Path, final: str) -> Path:
    """Download beside its destination, verify, then rename: a partial or unverified
    file never has the final name."""
    folder.mkdir(parents=True, exist_ok=True)
    part = folder / (final + ".part")
    try:
        _download(url, part, limit=MAX_ASSET_BYTES)
        verify_checksum(part, expected)
        destination = folder / final
        os.replace(part, destination)
    finally:
        part.unlink(missing_ok=True)
    return destination


def _replace_appimage(new: Path, target: Path) -> None:
    """Atomic in-place upgrade of an AppImage, keeping the previous one as `.old`."""
    new.chmod(0o755)
    old = target.with_name(target.name + ".old")
    old.unlink(missing_ok=True)
    os.replace(target, old)
    try:
        os.replace(new, target)
    except OSError:
        os.replace(old, target)
        raise


def _open_installer(path: Path, system: str) -> None:
    if system == "darwin":
        subprocess.run(["open", str(path)], check=True, timeout=30)
    elif system in ("win32", "cygwin"):
        startfile = getattr(os, "startfile", None)
        if startfile is not None:
            startfile(str(path))
        else:
            subprocess.Popen([str(path)], **desktop.process_options(detached=True))


def _install_frozen(
    paths: d.Paths,
    version: str,
    assets: dict[str, str],
    fetch_text: Callable[[str], str],
    system: str | None = None,
    machine: str | None = None,
) -> str:
    system = system or sys.platform
    name = platform_asset(assets, system, machine)
    expected = expected_checksum(name, assets, fetch_text)
    appimage = os.environ.get("APPIMAGE") if system.startswith("linux") else None
    if appimage and Path(appimage).is_file():
        target = Path(appimage)
        new = _fetch_verified(assets[name], name, expected, target.parent, target.name + ".new")
        _replace_appimage(new, target)
        return (
            f"{hotkeys.APP_NAME} {version} is installed. Restart it to use the new version "
            f"(the previous one is kept as {target.name}.old)."
        )
    destination = _fetch_verified(assets[name], name, expected, downloads_folder(paths), name)
    if system in ("darwin", "win32", "cygwin"):
        _open_installer(destination, system)
        step = (
            "Drag Clipboard+ to Applications in the window that opened, then restart it."
            if system == "darwin"
            else "Finish the installer that opened, then restart Clipboard+."
        )
        return f"{hotkeys.APP_NAME} {version} was downloaded and verified. {step}"
    return (
        f"{hotkeys.APP_NAME} {version} was downloaded and verified: {destination}. "
        "Run it to replace this version."
    )


def apply_update(
    paths: d.Paths,
    version: str,
    url: str,
    fetch: Callable[[str], dict[str, Any]] = _github,
    fetch_text: Callable[[str], str] = _fetch_text,
) -> str:
    """Download, verify and install a release; returns a message fit to show the user.

    The release is resolved to its exact tag and nothing is unpacked or run until
    the download matches the checksum the release published. Runs as its own
    process (see main) so the tray stays responsive; a lock keeps a second click
    from racing the first. `url` (from the check) must be a trusted https address;
    the download itself is always taken from the resolved tag.
    """
    d.private_dir(paths.runtime)
    lock = desktop.lock(paths.runtime / "update.lock")
    if lock is None:
        return ""
    log = paths.cache / "update.log"
    try:
        d.private_dir(paths.cache)
        state = read_state(paths)
        state.pop("error", None)  # A new attempt supersedes an earlier failure.
        state.pop("message", None)
        write_state(
            paths, {**state, "status": "installing", "target": version, "started": time.time()}
        )
        if not _trusted_url(url):
            raise UpdateError("The update download address is not one the app trusts.")
        tag, assets = resolve_tag(version, fetch)
        if desktop.frozen_root() is not None:
            message = _install_frozen(paths, version, assets, fetch_text)
        else:
            message = _install_source(paths, tag, assets, log, fetch_text)
        write_state(
            paths,
            {
                **read_state(paths),
                "status": "installed",
                "applied": version,
                "at": time.time(),
                "message": message,
            },
        )
        return message
    except UpdateError as exc:
        with contextlib.suppress(OSError):
            write_state(paths, {**read_state(paths), "status": "failed", "error": str(exc)})
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        with contextlib.suppress(OSError):
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text(str(exc), encoding="utf-8", errors="replace")
        timed_out = isinstance(exc, subprocess.TimeoutExpired)
        with contextlib.suppress(OSError):
            write_state(
                paths,
                {
                    **read_state(paths),
                    "status": "failed",
                    "error": (
                        "The update took too long and was rolled back."
                        if timed_out
                        else "The update did not complete. Try again, or re-run the installer."
                    ),
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
        message = apply_update(paths, version, url)
        if message:  # Empty when another update already held the lock.
            d.notify(d.Config(paths), message)
        return 0
    except (UpdateError, d.DictationError) as exc:
        try:
            d.notify(d.Config(paths), f"The update to {version} failed. {exc}")
        except d.DictationError:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
