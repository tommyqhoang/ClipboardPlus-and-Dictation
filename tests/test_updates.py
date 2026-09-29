"""Tests for update checking and applying (lib/updates.py)."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

import desktop
import dictation as d
import hotkeys
import updates


def run_main(argv: list[str]) -> int:
    """updates.py --apply … exactly as the detached updater runs it."""
    with patch.object(sys, "argv", ["updates.py", *argv]):
        return updates.main()


def release(tag: str, **extra: Any) -> dict[str, Any]:
    return {"tag_name": tag, **extra}


class UpdateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("DICTATION_")}
        self.env.update(
            {
                "XDG_CONFIG_HOME": str(self.root / "config"),
                "XDG_CACHE_HOME": str(self.root / "cache"),
                "XDG_RUNTIME_DIR": str(self.root / "runtime"),
            }
        )
        self.patch = patch.dict(os.environ, self.env, clear=True)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.paths = d.Paths()
        d.private_dir(self.paths.config.parent)
        updates.write_state(self.paths, {})

    # -- versions ---------------------------------------------------------
    def test_versions_compare_as_numbers(self):
        self.assertTrue(updates.newer("1.3.0", "1.2.0"))
        self.assertTrue(updates.newer("v1.10.0", "1.9.4"))  # 10 > 9, not string order.
        self.assertFalse(updates.newer("1.2.0", "1.2.0"))
        self.assertFalse(updates.newer("1.1.9", "1.2.0"))
        self.assertFalse(updates.newer("nope", "1.2.0"))
        self.assertFalse(updates.newer("1.3.0", "unknown"))

    def test_a_tag_without_a_version_is_ignored(self):
        self.assertIsNone(updates.parse_version("latest"))

    # -- checking ---------------------------------------------------------
    def test_a_newer_release_is_offered(self):
        found = updates.check(
            self.paths, force=True, clock=lambda: 1000.0, fetch=lambda _: release("v9.9.9")
        )
        assert found is not None
        self.assertEqual(found["version"], "9.9.9")
        self.assertIn("checked", updates.read_state(self.paths))

    def test_the_same_version_is_not_offered(self):
        found = updates.check(
            self.paths,
            force=True,
            clock=lambda: 1000.0,
            fetch=lambda _: release("v" + desktop.APP_VERSION),
        )
        self.assertIsNone(found)

    def test_a_check_happens_at_most_once_a_day(self):
        calls: list[str] = []

        def fetch(url: str) -> dict[str, Any]:
            calls.append(url)
            return release("v9.9.9")

        updates.check(self.paths, clock=lambda: 1000.0, fetch=fetch)
        updates.check(self.paths, clock=lambda: 1000.0 + 3600.0, fetch=fetch)
        self.assertEqual(len(calls), 1)
        updates.check(self.paths, clock=lambda: 1000.0 + 25 * 3600.0, fetch=fetch)
        self.assertEqual(len(calls), 2)

    def test_a_cached_newer_release_remains_available_after_restart(self):
        calls: list[str] = []

        def fetch(url: str) -> dict[str, Any]:
            calls.append(url)
            return release("v9.9.9")

        first = updates.check(self.paths, clock=lambda: 1000.0, fetch=fetch)
        cached = updates.check(self.paths, clock=lambda: 2000.0, fetch=fetch)
        self.assertEqual(cached, first)
        self.assertEqual(len(calls), 1)

    def test_a_cached_release_at_the_current_version_is_not_offered(self):
        updates.write_state(
            self.paths,
            {
                "checked": 1000.0,
                "offered": {
                    "version": desktop.APP_VERSION,
                    "tag": "v" + desktop.APP_VERSION,
                    "url": "https://example.com",
                },
            },
        )
        self.assertIsNone(updates.check(self.paths, clock=lambda: 2000.0))

    def test_a_check_during_install_keeps_its_progress_visible(self):
        updates.write_state(
            self.paths,
            {"status": "installing", "target": "9.9.9", "started": 900.0},
        )
        updates.check(
            self.paths, force=True, clock=lambda: 1000.0, fetch=lambda _: release("v9.9.9")
        )
        state = updates.read_state(self.paths)
        self.assertEqual(state["status"], "installing")
        self.assertEqual(state["target"], "9.9.9")
        self.assertEqual(state["offered"]["version"], "9.9.9")

    def test_turning_updates_off_stops_the_daily_check(self):
        hotkeys.Preferences(self.paths).save(auto_updates=False)
        self.assertIsNone(
            updates.check(self.paths, clock=lambda: 2000.0, fetch=lambda _: release("v9.9.9"))
        )

    def test_the_offered_release_is_remembered_for_the_settings_page(self):
        updates.check(
            self.paths,
            force=True,
            clock=lambda: 1000.0,
            fetch=lambda _: release("v9.9.9", tarball_url="https://api.github.com/t.tar.gz"),
        )
        offered = updates.read_state(self.paths).get("offered")
        assert isinstance(offered, dict)
        self.assertEqual(offered["version"], "9.9.9")

    def test_failed_check_does_not_claim_current_or_delay_the_next_try(self):
        before = updates.read_state(self.paths)
        with self.assertRaisesRegex(updates.UpdateError, "Couldn’t check"):
            updates.check(
                self.paths,
                force=True,
                fetch=lambda _: (_ for _ in ()).throw(OSError("offline")),
            )
        self.assertEqual(updates.read_state(self.paths), before)
        self.assertTrue(updates.due(self.paths))

    def test_no_published_release_is_distinct_from_a_failed_check(self):
        def no_release(url: str) -> dict[str, Any]:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

        self.assertIsNone(updates.check(self.paths, force=True, fetch=no_release))
        state = updates.read_state(self.paths)
        self.assertIs(state["published"], False)
        self.assertIn("checked", state)

    def test_release_rejects_unexpected_version_and_download_host(self):
        for response in (
            release("v9.9.9", tarball_url="http://api.github.com/release.tar.gz"),
            release("v9.9.9", tarball_url="https://example.com/release.tar.gz"),
            release("../9.9.9"),
        ):
            with self.subTest(response=response), self.assertRaises(updates.UpdateError):
                updates.release(lambda _: response)

    def test_release_preserves_a_specific_response_error(self):
        def too_large(_url: str) -> dict[str, Any]:
            raise updates.UpdateError("The update check response was too large.")

        with self.assertRaisesRegex(updates.UpdateError, "too large"):
            updates.release(too_large)

    def test_update_check_rejects_oversized_and_non_object_responses(self):
        for body, message in (
            (b"x" * (1024 * 1024 + 1), "too large"),
            (b"[]", "not what GitHub usually sends"),
        ):
            with self.subTest(message=message):
                opener = Mock()
                opener.open.return_value.__enter__ = Mock(return_value=io.BytesIO(body))
                opener.open.return_value.__exit__ = Mock(return_value=False)
                with (
                    patch.object(urllib.request, "build_opener", return_value=opener),
                    self.assertRaisesRegex(updates.UpdateError, message),
                ):
                    updates._github(updates.RELEASES_URL)

    # -- downloads --------------------------------------------------------
    def test_only_https_download_addresses_are_allowed(self):
        with self.assertRaises(updates.UpdateError):
            updates._download("http://example.com", self.root / "out.tar.gz")

    def test_download_refuses_a_response_larger_than_the_release_limit(self):
        opener = Mock()
        opener.open.return_value.__enter__ = Mock(return_value=io.BytesIO(b"abcd"))
        opener.open.return_value.__exit__ = Mock(return_value=False)
        destination = self.root / "out.tar.gz"
        with (
            patch.object(urllib.request, "build_opener", return_value=opener),
            patch.object(updates, "MAX_RELEASE_BYTES", 3),
            self.assertRaisesRegex(updates.UpdateError, "larger than expected"),
        ):
            updates._download("https://github.com/release.tar.gz", destination)
        self.assertEqual(destination.read_bytes(), b"")

    def test_download_refuses_insecure_redirect(self):
        handler = updates.HTTPSRedirect()
        request = urllib.request.Request("https://example.com/release.tar.gz")
        with self.assertRaisesRegex(updates.UpdateError, "insecure address"):
            handler.redirect_request(
                request, None, 302, "Found", {}, "http://example.com/release.tar.gz"
            )

    def test_download_refuses_redirect_off_the_allowed_hosts(self):
        handler = updates.HTTPSRedirect()
        request = urllib.request.Request("https://github.com/release.tar.gz")
        with self.assertRaisesRegex(updates.UpdateError, "unexpected address"):
            handler.redirect_request(
                request, None, 302, "Found", {}, "https://evil.example.com/release.tar.gz"
            )

    def test_the_updater_runs_as_its_own_detached_process(self):
        with patch.object(updates.subprocess, "Popen") as popen:
            updates.start_updater("9.9.9", "https://example.com/t.tar.gz")
        command = popen.call_args.args[0]
        self.assertEqual(command[command.index("--apply") + 1], "9.9.9")
        self.assertTrue(popen.call_args.kwargs.get("start_new_session") or True)

    def test_a_release_is_read_from_a_real_http_response(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class GitHub(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                body = json.dumps({"tag_name": "v8.8.8"}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), GitHub)
        self.addCleanup(server.server_close)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        with patch.object(updates, "RELEASES_URL", f"http://127.0.0.1:{server.server_address[1]}"):
            found = updates.release()
        assert found is not None
        self.assertEqual(found["version"], "8.8.8")
        self.assertIn("8.8.8", found["url"])

    def test_an_undownloadable_release_reports_a_clean_error(self):
        with self.assertRaises(updates.UpdateError):
            updates._download("https://127.0.0.1:1/release.tar.gz", self.root / "x.tar.gz")

    def test_the_updater_command_applies_and_notifies(self):
        with (
            patch.object(updates, "apply_update") as apply,
            patch.object(d, "notify") as notify,
        ):
            code = run_main(["--apply", "9.9.9", "https://example.com/t.tar.gz"])
        apply.assert_called_once()
        notify.assert_called_once()
        self.assertEqual(code, 0)

    def test_the_updater_command_reports_a_failed_apply(self):
        with (
            patch.object(updates, "apply_update", side_effect=updates.UpdateError("nope")),
            patch.object(d, "notify") as notify,
        ):
            code = run_main(["--apply", "9.9.9", "https://example.com/t.tar.gz"])
        self.assertEqual(code, 1)
        self.assertIn("failed", notify.call_args.args[1])

    # -- applying: integrity, atomic swap, rollback ----------------------

    def source_release(self, tag="v9.9.9", setup="pass", sums="manifest", tamper=False):
        """A fake GitHub: (fetch, fetch_text, download) for a source release."""
        src = self.root / "src" / f"ClipboardPlus-{tag}"
        src.mkdir(parents=True, exist_ok=True)
        (src / "setup-desktop.py").write_text(setup, encoding="utf-8")
        archive = self.root / "src" / "release.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(src, arcname=src.name)
        blob = archive.read_bytes()
        digest = hashlib.sha256(blob).hexdigest()
        served = b"tampered" + blob if tamper else blob
        name = f"{tag}.tar.gz"
        base = "https://github.com/o/r/releases/download/" + tag
        assets = {}
        texts = {}
        if sums == "manifest":
            assets["SHA256SUMS"] = f"{base}/SHA256SUMS"
            texts[assets["SHA256SUMS"]] = f"{digest}  {name}\n{'0' * 64}  other.bin\n"
        elif sums == "sidecar":
            assets[name + ".sha256"] = f"{base}/{name}.sha256"
            texts[assets[name + ".sha256"]] = f"{digest}  {name}\n"
        elif sums == "wrong":
            assets["SHA256SUMS"] = f"{base}/SHA256SUMS"
            texts[assets["SHA256SUMS"]] = f"{'1' * 64}  {name}\n"

        def fetch(url: str) -> dict[str, Any]:
            return {
                "tag_name": tag,
                "assets": [{"name": n, "browser_download_url": u} for n, u in assets.items()],
            }

        def download(url: str, destination: Path, limit: int = 0) -> None:
            self.assertEqual(url, updates.TARBALL_URL.format(tag=tag))
            destination.write_bytes(served)

        return fetch, texts.__getitem__, download

    URL = "https://github.com/o/r/archive/refs/tags/v9.9.9.tar.gz"

    def apply(self, fetch, fetch_text, download, **patches):
        with patch.object(updates, "_download", download):
            return updates.apply_update(self.paths, "9.9.9", self.URL, fetch, fetch_text)

    def test_a_verified_release_is_installed_and_recorded(self):
        marker = self.root / "installed.txt"
        setup = f"from pathlib import Path\nPath({str(marker)!r}).write_text('ok')\nprint('done')\n"
        for sums in ("manifest", "sidecar"):
            marker.unlink(missing_ok=True)
            with self.subTest(sums=sums):
                message = self.apply(*self.source_release(setup=setup, sums=sums))
                self.assertEqual(marker.read_text(), "ok")
                self.assertIn("9.9.9", message)
                state = updates.read_state(self.paths)
                self.assertEqual((state["status"], state["applied"]), ("installed", "9.9.9"))
                self.assertEqual(state["message"], message)
                self.assertIn("done", (self.paths.cache / "update.log").read_text())

    def test_a_checksum_mismatch_is_refused_before_anything_is_unpacked_or_run(self):
        marker = self.root / "ran.txt"
        setup = f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n"
        for kwargs in ({"tamper": True}, {"sums": "wrong"}):
            with self.subTest(**kwargs), patch.object(updates, "_extract") as extract:
                with self.assertRaisesRegex(updates.UpdateError, "did not match"):
                    self.apply(*self.source_release(setup=setup, **kwargs))
                extract.assert_not_called()
                self.assertFalse(marker.exists())
                self.assertEqual(updates.read_state(self.paths)["status"], "failed")
                self.assertNotIn("applied", updates.read_state(self.paths))

    def test_a_release_without_a_checksum_is_refused(self):
        marker = self.root / "ran.txt"
        setup = f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n"
        with self.assertRaisesRegex(updates.UpdateError, "no published checksum"):
            self.apply(*self.source_release(setup=setup, sums="none"))
        self.assertFalse(marker.exists())

    def test_conflicting_checksum_files_are_refused(self):
        assets = {
            "SHA256SUMS": "https://github.com/a/SHA256SUMS",
            "x.bin.sha256": "https://github.com/a/x.bin.sha256",
        }
        texts = {
            assets["SHA256SUMS"]: "1" * 64 + "  x.bin\n",
            assets["x.bin.sha256"]: "2" * 64 + "  x.bin\n",
        }
        with self.assertRaisesRegex(updates.UpdateError, "disagree"):
            updates.expected_checksum("x.bin", assets, texts.__getitem__)

    def test_checksum_lookup_tolerates_github_renaming_the_asset(self):
        assets = {"SHA256SUMS": "https://github.com/a/SHA256SUMS"}
        text = {assets["SHA256SUMS"]: "a" * 64 + "  Clipboard+-Setup.exe\n"}
        found = updates.expected_checksum("Clipboard.-Setup.exe", assets, text.__getitem__)
        self.assertEqual(found, "a" * 64)

    def test_the_release_is_resolved_to_an_exact_tag(self):
        calls: list[str] = []

        def fetch(url: str) -> dict[str, Any]:
            calls.append(url)
            if url.endswith("/v9.9.9"):
                raise urllib.error.HTTPError(url, 404, "nf", {}, None)  # type: ignore[arg-type]
            return {"tag_name": "9.9.9", "assets": []}

        tag, _ = updates.resolve_tag("9.9.9", fetch)
        self.assertEqual(tag, "9.9.9")
        self.assertEqual(len(calls), 2)
        with self.assertRaises(updates.UpdateError):
            updates.resolve_tag("9.9.9", lambda url: {"tag_name": "v1.0.0", "assets": []})
        with self.assertRaises(updates.UpdateError):
            updates.resolve_tag("main", fetch)

    def test_an_untrusted_download_address_is_refused(self):
        with self.assertRaisesRegex(updates.UpdateError, "trusts"):
            updates.apply_update(self.paths, "9.9.9", "https://evil.example/x.tar.gz")

    def make_app(self) -> Path:
        app = self.root / "prefix" / "lib" / "whisper-dictation"
        app.mkdir(parents=True)
        (app / "tray.py").write_text("old version", encoding="utf-8")
        return app

    def test_a_failed_install_rolls_back_to_the_previous_app(self):
        app = self.make_app()
        setup = (
            "from pathlib import Path\n"
            f"Path({str(app / 'tray.py')!r}).write_text('half written')\n"
            f"Path({str(app / 'extra.py')!r}).write_text('new')\n"
            "raise SystemExit(1)\n"
        )
        with patch.object(updates, "app_folder", return_value=app):
            with self.assertRaises(updates.UpdateError):
                self.apply(*self.source_release(setup=setup))
        self.assertEqual((app / "tray.py").read_text(), "old version")
        self.assertFalse((app / "extra.py").exists())
        self.assertEqual([p.name for p in app.parent.iterdir()], ["whisper-dictation"])
        self.assertNotIn("applied", updates.read_state(self.paths))

    def test_a_timed_out_install_rolls_back_too(self):
        app = self.make_app()
        real_run = subprocess.run

        def hang(command, **kwargs):
            (app / "tray.py").write_text("half written", encoding="utf-8")
            raise subprocess.TimeoutExpired(command, 1800)

        with (
            patch.object(updates, "app_folder", return_value=app),
            patch.object(updates.subprocess, "run", hang),
        ):
            with self.assertRaisesRegex(updates.UpdateError, "did not complete"):
                self.apply(*self.source_release())
        self.assertIs(updates.subprocess.run, real_run)
        self.assertEqual((app / "tray.py").read_text(), "old version")
        self.assertEqual([p.name for p in app.parent.iterdir()], ["whisper-dictation"])
        self.assertIn("rolled back", updates.read_state(self.paths)["error"])

    def test_a_successful_install_swaps_and_removes_its_backup(self):
        app = self.make_app()
        stale = app.parent / "whisper-dictation.bak-1"
        stale.mkdir()
        (stale / "junk").write_text("x")
        setup = f"from pathlib import Path\nPath({str(app / 'tray.py')!r}).write_text('new')\n"
        with patch.object(updates, "app_folder", return_value=app):
            self.apply(*self.source_release(setup=setup))
        self.assertEqual((app / "tray.py").read_text(), "new")
        self.assertEqual([p.name for p in app.parent.iterdir()], ["whisper-dictation"])

    def test_a_filesystem_failure_records_a_retryable_update_error(self):
        fetch, texts, _ = self.source_release()
        with patch.object(updates, "_download", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(updates.UpdateError, "disk full"):
                updates.apply_update(self.paths, "9.9.9", self.URL, fetch, texts)
        state = updates.read_state(self.paths)
        self.assertEqual(state["status"], "failed")
        self.assertIn("re-run the installer", state["error"])
        self.assertIn("disk full", (self.paths.cache / "update.log").read_text())
        self.assertNotIn("applied", state)

    def test_retry_clears_the_previous_error(self):
        updates.write_state(self.paths, {"status": "failed", "error": "old failure"})
        fetch, texts, _ = self.source_release()

        def interrupted(_url: str, _destination: Path, limit: int = 0) -> None:
            self.assertNotIn("error", updates.read_state(self.paths))
            raise updates.UpdateError("new failure")

        with patch.object(updates, "_download", interrupted):
            with self.assertRaisesRegex(updates.UpdateError, "new failure"):
                updates.apply_update(self.paths, "9.9.9", self.URL, fetch, texts)
        self.assertEqual(updates.read_state(self.paths)["error"], "new failure")

    def test_a_second_update_click_waits_for_the_first(self):
        holder = desktop.lock(self.paths.runtime / "update.lock")
        self.addCleanup(os.close, holder)
        with patch.object(updates, "_download") as download:
            self.assertEqual(updates.apply_update(self.paths, "9.9.9", self.URL), "")
        download.assert_not_called()
        self.assertNotIn("applied", updates.read_state(self.paths))


class ExtractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.work = Path(tempfile.mkdtemp(prefix="updates-extract-"))
        self.addCleanup(shutil.rmtree, self.work, ignore_errors=True)

    def test_a_release_tarball_is_unpacked_to_its_folder(self):
        source = self.work / "ClipboardPlus-and-Dictation-1.3.0"
        (source / "lib").mkdir(parents=True)
        (source / "setup-desktop.py").write_text("# setup\n", encoding="utf-8")
        archive = self.work / "release.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(source, arcname=source.name)
        folder = updates._extract(archive, self.work / "unpack")
        self.assertEqual((folder / "setup-desktop.py").read_text(encoding="utf-8"), "# setup\n")

    def test_paths_leaving_the_unpack_folder_are_refused(self):
        evil = self.work / "evil.tar.gz"
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode="w:gz") as tar:
            info = tarfile.TarInfo("../../escaped.txt")
            payload = b"no"
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
        evil.write_bytes(data.getvalue())
        with self.assertRaises(updates.UpdateError):
            updates._extract(evil, self.work / "unpack2")

    def test_nested_traversal_links_and_windows_paths_are_refused(self):
        for name, kind in (
            ("release/../../escaped.txt", tarfile.REGTYPE),
            ("release/link", tarfile.SYMTYPE),
            ("release/hardlink", tarfile.LNKTYPE),
            ("C:/escaped.txt", tarfile.REGTYPE),
            ("release\\escaped.txt", tarfile.REGTYPE),
        ):
            with self.subTest(name=name):
                archive = self.work / "unsafe.tar.gz"
                with tarfile.open(archive, "w:gz") as tar:
                    member = tarfile.TarInfo(name)
                    member.type = kind
                    member.linkname = "../../escaped.txt"
                    member.size = 2 if kind == tarfile.REGTYPE else 0
                    tar.addfile(member, io.BytesIO(b"no") if member.size else None)
                with self.assertRaisesRegex(updates.UpdateError, "unsafe file path"):
                    updates._extract(archive, self.work / "unpack-unsafe")
                self.assertFalse((self.work / "escaped.txt").exists())

    def test_decompressed_archive_size_is_bounded(self):
        archive = self.work / "large.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            member = tarfile.TarInfo("release/large.bin")
            member.size = 3
            tar.addfile(member, io.BytesIO(b"abc"))
        with (
            patch.object(updates, "MAX_EXTRACTED_BYTES", 2),
            self.assertRaisesRegex(updates.UpdateError, "too many files or too much data"),
        ):
            updates._extract(archive, self.work / "unpack-large")

    @unittest.skipIf(sys.platform == "win32", "Windows files carry no Unix mode bits")
    def test_fallback_extraction_normalizes_modes_without_the_data_filter(self):
        archive = self.work / "modes.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            for name, mode in (("release/setup-desktop.py", 0o4777), ("release/data.txt", 0o666)):
                member = tarfile.TarInfo(name)
                member.mode, member.size = mode, 1
                tar.addfile(member, io.BytesIO(b"x"))
        with patch.object(updates.tarfile, "data_filter", None):
            top = updates._extract(archive, self.work / "fallback")
        self.assertEqual((top / "setup-desktop.py").stat().st_mode & 0o7777, 0o755)
        self.assertEqual((top / "data.txt").stat().st_mode & 0o7777, 0o644)

    def test_extraction_uses_the_data_filter_when_available(self):
        if getattr(tarfile, "data_filter", None) is None:
            self.skipTest("no tarfile data filter")
        archive = self.work / "f.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            member = tarfile.TarInfo("release/setup-desktop.py")
            member.size = 1
            tar.addfile(member, io.BytesIO(b"x"))
        with patch.object(tarfile.TarFile, "extractall", autospec=True) as extract:
            with self.assertRaises(updates.UpdateError):  # Nothing extracted -> no installer.
                updates._extract(archive, self.work / "filtered")
        self.assertEqual(extract.call_args.kwargs["filter"], "data")


class FrozenUpdateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("DICTATION_")}
        self.env.update(
            {
                "XDG_CONFIG_HOME": str(self.root / "config"),
                "XDG_CACHE_HOME": str(self.root / "cache"),
                "XDG_RUNTIME_DIR": str(self.root / "runtime"),
            }
        )
        self.patch = patch.dict(os.environ, self.env, clear=True)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.paths = d.Paths()
        d.private_dir(self.paths.config.parent)
        updates.write_state(self.paths, {})

    ASSETS = {
        "Clipboard.-x86_64.AppImage": "https://github.com/o/r/releases/download/v9/a.AppImage",
        "Clipboard.-macOS-arm64.dmg": "https://github.com/o/r/releases/download/v9/a.dmg",
        "Clipboard.-Setup.exe": "https://github.com/o/r/releases/download/v9/a.exe",
        "SHA256SUMS": "https://github.com/o/r/releases/download/v9/SHA256SUMS",
    }

    def test_each_platform_selects_its_own_installer(self):
        pick = updates.platform_asset
        self.assertEqual(pick(self.ASSETS, "linux", "x86_64"), "Clipboard.-x86_64.AppImage")
        self.assertEqual(pick(self.ASSETS, "darwin", "arm64"), "Clipboard.-macOS-arm64.dmg")
        self.assertEqual(pick(self.ASSETS, "win32", "AMD64"), "Clipboard.-Setup.exe")
        for system, machine in (("linux", "aarch64"), ("darwin", "x86_64"), ("freebsd", "x86_64")):
            with self.subTest(system=system), self.assertRaises(updates.UpdateError):
                pick(self.ASSETS, system, machine)
        with self.assertRaisesRegex(updates.UpdateError, "no download"):
            pick({"notes.txt": "https://github.com/x"}, "linux", "x86_64")

    def frozen_setup(self, system, blob=b"NEW-APP", good=True):
        digest = hashlib.sha256(blob).hexdigest() if good else "0" * 64
        name = updates.platform_asset(
            self.ASSETS, system, "arm64" if system == "darwin" else "x86_64"
        )
        text = {self.ASSETS["SHA256SUMS"]: f"{digest}  {name}\n"}

        def download(url: str, destination: Path, limit: int = 0) -> None:
            self.assertEqual(url, self.ASSETS[name])
            destination.write_bytes(blob)

        return name, text.__getitem__, download

    def test_an_appimage_is_replaced_in_place_and_the_old_one_kept(self):
        target = self.root / "Clipboard+-x86_64.AppImage"
        target.write_bytes(b"OLD-APP")
        _, texts, download = self.frozen_setup("linux")
        with (
            patch.dict(os.environ, {"APPIMAGE": str(target)}),
            patch.object(updates, "_download", download),
        ):
            message = updates._install_frozen(
                self.paths, "9.9.9", self.ASSETS, texts, "linux", "x86_64"
            )
        self.assertEqual(target.read_bytes(), b"NEW-APP")
        self.assertTrue(os.access(target, os.X_OK))
        self.assertEqual((self.root / (target.name + ".old")).read_bytes(), b"OLD-APP")
        self.assertEqual(sorted(p.suffix for p in self.root.glob("*.new")), [])
        self.assertIn("Restart", message)

    def test_a_bad_checksum_leaves_the_running_appimage_untouched(self):
        target = self.root / "Clipboard+-x86_64.AppImage"
        target.write_bytes(b"OLD-APP")
        _, texts, download = self.frozen_setup("linux", good=False)
        with (
            patch.dict(os.environ, {"APPIMAGE": str(target)}),
            patch.object(updates, "_download", download),
            self.assertRaisesRegex(updates.UpdateError, "did not match"),
        ):
            updates._install_frozen(self.paths, "9.9.9", self.ASSETS, texts, "linux", "x86_64")
        self.assertEqual(target.read_bytes(), b"OLD-APP")
        self.assertEqual(sorted(p.name for p in self.root.iterdir() if p.is_file()), [target.name])

    def test_a_failed_swap_restores_the_appimage(self):
        target = self.root / "app.AppImage"
        target.write_bytes(b"OLD")
        new = self.root / "app.AppImage.new"
        new.write_bytes(b"NEW")
        real = os.replace

        def flaky(src, dst):
            if str(src).endswith(".new"):
                raise OSError("busy")
            real(src, dst)

        with patch.object(updates.os, "replace", flaky), self.assertRaises(OSError):
            updates._replace_appimage(new, target)
        self.assertEqual(target.read_bytes(), b"OLD")

    def test_a_dmg_is_verified_before_it_is_opened(self):
        downloads = self.root / "Downloads"
        downloads.mkdir()
        for good in (False, True):
            name, texts, download = self.frozen_setup("darwin", good=good)
            with (
                patch.object(updates, "downloads_folder", return_value=downloads),
                patch.object(updates, "_download", download),
                patch.object(updates.subprocess, "run") as run,
            ):
                if good:
                    message = updates._install_frozen(
                        self.paths, "9.9.9", self.ASSETS, texts, "darwin", "arm64"
                    )
                    run.assert_called_once()
                    self.assertEqual(run.call_args.args[0], ["open", str(downloads / name)])
                    self.assertIn("Applications", message)
                else:
                    with self.assertRaises(updates.UpdateError):
                        updates._install_frozen(
                            self.paths, "9.9.9", self.ASSETS, texts, "darwin", "arm64"
                        )
                    run.assert_not_called()
                    self.assertEqual(list(downloads.iterdir()), [])

    def test_windows_setup_is_launched_only_after_verification(self):
        downloads = self.root / "Downloads"
        downloads.mkdir()
        name, texts, download = self.frozen_setup("win32")
        launched: list[str] = []
        with (
            patch.object(updates, "downloads_folder", return_value=downloads),
            patch.object(updates, "_download", download),
            patch.object(updates.os, "startfile", launched.append, create=True),
        ):
            message = updates._install_frozen(
                self.paths, "9.9.9", self.ASSETS, texts, "win32", "x86_64"
            )
        self.assertEqual(launched, [str(downloads / name)])
        self.assertIn("installer", message)

    def test_an_unpackaged_linux_download_is_saved_verified_not_run(self):
        downloads = self.root / "Downloads"
        downloads.mkdir()
        name, texts, download = self.frozen_setup("linux")
        env = {k: v for k, v in os.environ.items() if k != "APPIMAGE"}
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(updates, "downloads_folder", return_value=downloads),
            patch.object(updates, "_download", download),
            patch.object(updates.subprocess, "run") as run,
        ):
            message = updates._install_frozen(
                self.paths, "9.9.9", self.ASSETS, texts, "linux", "x86_64"
            )
        run.assert_not_called()
        self.assertEqual((downloads / name).read_bytes(), b"NEW-APP")
        self.assertIn(str(downloads / name), message)

    def test_a_frozen_update_without_a_checksum_downloads_nothing(self):
        install = self.root / "Clipboard+"
        install.mkdir()
        fetch = lambda url: {  # noqa: E731
            "tag_name": "v9.9.9",
            "assets": [
                {
                    "name": "Clipboard+-Setup.exe",
                    "browser_download_url": "https://github.com/o/a.exe",
                },
                {
                    "name": "Clipboard+-x86_64.AppImage",
                    "browser_download_url": "https://github.com/o/a.AppImage",
                },
                {
                    "name": "Clipboard+-macOS-arm64.dmg",
                    "browser_download_url": "https://github.com/o/a.dmg",
                },
            ],
        }
        with (
            patch.object(updates.desktop, "frozen_root", return_value=install),
            patch.object(updates, "_download") as download,
            patch.object(updates.subprocess, "Popen") as popen,
            patch.object(
                updates.host,
                "machine",
                return_value="x86_64" if sys.platform != "darwin" else "arm64",
            ),
        ):
            with self.assertRaises(updates.UpdateError):
                updates.apply_update(self.paths, "9.9.9", "https://github.com/o/r/a.tar.gz", fetch)
        download.assert_not_called()
        popen.assert_not_called()
        self.assertEqual(updates.read_state(self.paths).get("status"), "failed")


if __name__ == "__main__":
    unittest.main()
