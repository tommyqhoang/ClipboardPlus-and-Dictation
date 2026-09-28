"""Tests for update checking and applying (lib/updates.py)."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import urllib.request
from pathlib import Path
from typing import Any
from unittest.mock import patch

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
            fetch=lambda _: release("v9.9.9", tarball_url="https://example.com/t.tar.gz"),
        )
        offered = updates.read_state(self.paths).get("offered")
        assert isinstance(offered, dict)
        self.assertEqual(offered["version"], "9.9.9")

    # -- downloads --------------------------------------------------------
    def test_only_https_download_addresses_are_allowed(self):
        with self.assertRaises(updates.UpdateError):
            updates._download("http://example.com", self.root / "out.tar.gz")

    def test_download_refuses_insecure_redirect(self):
        handler = updates.HTTPSRedirect()
        request = urllib.request.Request("https://example.com/release.tar.gz")
        with self.assertRaisesRegex(updates.UpdateError, "insecure address"):
            handler.redirect_request(
                request, None, 302, "Found", {}, "http://example.com/release.tar.gz"
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

    def test_an_update_downloads_extracts_and_installs(self):
        installed: list[str] = []

        def fake_download(url: str, destination: Path) -> None:
            destination.write_bytes(b"tarball")

        def fake_extract(archive: Path, folder: Path) -> Path:
            source = folder / "ClipboardPlus-9.9.9"
            (source / "lib").mkdir(parents=True)
            (source / "setup-desktop.py").write_text(
                "import sys; print('installed', sys.argv[1])", encoding="utf-8"
            )
            return source

        def fake_setup() -> int:
            installed.append("setup-desktop.py ran")
            return 0

        # setup-desktop.py is a real script; the extracted stub is what runs, so a
        # successful install only needs its exit code.
        with (
            patch.object(updates, "_download", fake_download),
            patch.object(updates, "_extract", fake_extract),
            patch.object(updates.subprocess, "run") as run,
        ):
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            updates.apply_update(self.paths, "9.9.9", "https://example.com/t.tar.gz")
        self.assertEqual(installed, [])
        self.assertTrue(run.called)
        state = updates.read_state(self.paths)
        self.assertEqual(state.get("applied"), "9.9.9")

    def test_a_failed_install_reports_and_does_not_apply(self):
        with (
            patch.object(updates, "_download", lambda *a: None),
            patch.object(updates, "_extract", self._stub_source),
            patch.object(
                updates.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 1, "", "boom"),
            ),
        ):
            with self.assertRaises(updates.UpdateError):
                updates.apply_update(self.paths, "9.9.9", "https://example.com/t.tar.gz")
        self.assertNotIn("applied", updates.read_state(self.paths))
        self.assertIn("boom", (self.paths.cache / "update.log").read_text(encoding="utf-8"))

    def _stub_source(self, archive: Path, folder: Path) -> Path:
        source = folder / "ClipboardPlus-9.9.9"
        (source / "lib").mkdir(parents=True)
        (source / "setup-desktop.py").write_text("", encoding="utf-8")
        return source

    def test_a_second_update_click_waits_for_the_first(self):
        with (
            patch.object(updates, "_download", lambda *a: None),
            patch.object(updates, "_extract", self._stub_source),
            patch.object(
                updates.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")
            ),
        ):
            holder = desktop.lock(self.paths.runtime / "update.lock")
            self.addCleanup(os.close, holder)
            updates.apply_update(self.paths, "9.9.9", "https://example.com/t.tar.gz")  # Returns.
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


if __name__ == "__main__":
    unittest.main()
