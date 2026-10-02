"""Control files (quit, sync now): claimed atomically, validated, single instance, logging."""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import clipservice
import desktop

QUIT = frozenset({"quit"})


class ConsumeSignalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.path = self.folder / "clip-quit"

    def leftovers(self):
        return sorted(p.name for p in self.folder.iterdir())

    def test_a_valid_request_is_claimed_and_removed(self):
        self.path.write_text("quit")
        self.assertTrue(clipservice.consume_signal(self.path, QUIT))
        self.assertEqual(self.leftovers(), [])
        self.assertFalse(clipservice.consume_signal(self.path, QUIT))  # Gone.

    def test_whitespace_is_ignored_and_empty_can_be_allowed(self):
        self.path.write_text("quit\n")
        self.assertTrue(clipservice.consume_signal(self.path, QUIT))
        self.path.write_text("")
        self.assertTrue(clipservice.consume_signal(self.path, frozenset({"", "1"})))
        self.path.write_text("")
        self.assertFalse(clipservice.consume_signal(self.path, QUIT))

    def test_invalid_content_is_discarded_and_not_honoured(self):
        for content in ("stop", "quit" * 20, "\x00\xff"):
            with self.subTest(content=content[:8]):
                self.path.write_bytes(content.encode("latin-1"))
                self.assertFalse(clipservice.consume_signal(self.path, QUIT))
                self.assertEqual(self.leftovers(), [])

    def test_a_folder_or_a_link_is_not_a_request(self):
        self.path.mkdir()
        self.assertFalse(clipservice.consume_signal(self.path, QUIT))
        self.assertEqual(self.leftovers(), [])
        target = self.folder / "target"
        target.write_text("quit")
        try:
            self.path.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("no symlinks")
        self.assertFalse(clipservice.consume_signal(self.path, QUIT))
        self.assertTrue(target.exists())  # The link's target is never read or removed.

    @unittest.skipIf(sys.platform == "win32", "ownership")
    def test_a_file_owned_by_someone_else_is_ignored(self):
        self.path.write_text("quit")
        with patch.object(clipservice.os, "getuid", return_value=os.getuid() + 1):
            self.assertFalse(clipservice.consume_signal(self.path, QUIT))
        self.assertEqual(self.leftovers(), [])

    def test_a_windows_sharing_violation_is_retried_not_dropped(self):
        self.path.write_text("quit")
        real_open = os.open
        calls = []

        def flaky(path, flags, *args):
            calls.append(path)
            if len(calls) == 1:
                raise PermissionError(13, "sharing violation")
            return real_open(path, flags, *args)

        with (
            patch.object(clipservice.sys, "platform", "win32"),
            patch.object(clipservice.os, "open", flaky),
            patch.object(clipservice.time, "sleep"),
        ):
            self.assertTrue(clipservice.consume_signal(self.path, QUIT))
        self.assertGreater(len(calls), 1)

    def test_only_one_of_many_racing_readers_gets_the_request(self):
        for _ in range(20):
            self.path.write_text("quit")
            wins: list[bool] = []
            barrier = threading.Barrier(6)

            def reader():
                barrier.wait()
                wins.append(clipservice.consume_signal(self.path, QUIT))

            threads = [threading.Thread(target=reader) for _ in range(6)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(wins.count(True), 1)
            self.assertEqual(self.leftovers(), [])


class SingleInstanceTests(unittest.TestCase):
    def test_a_second_lock_is_refused_while_the_first_is_held(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "clipservice.lock"
            first = desktop.lock(path)
            self.assertIsNotNone(first)
            self.assertIsNone(desktop.lock(path))
            os.close(first)
            again = desktop.lock(path)
            self.assertIsNotNone(again)
            os.close(again)
            if sys.platform != "win32":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
