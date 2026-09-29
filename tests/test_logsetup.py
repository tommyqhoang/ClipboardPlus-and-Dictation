"""The shared rotating log: private files, size cap, and use for a child's output."""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import logsetup


class LogTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        environment = patch.dict(os.environ, {"XDG_CACHE_HOME": temporary.name})
        environment.start()
        self.addCleanup(environment.stop)
        self.root = Path(temporary.name)

    def test_the_log_folder_is_under_the_cache_folder_and_private(self):
        folder = logsetup.log_dir()
        self.assertEqual(folder, self.root / "dictation" / "logs")
        if sys.platform != "win32":
            self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)

    def test_a_logger_writes_a_private_file_and_is_reusable(self):
        log = logsetup.get_logger("unit-test-a")
        self.addCleanup(lambda: [log.removeHandler(h) or h.close() for h in list(log.handlers)])
        log.warning("something %s", "happened")
        self.assertIs(logsetup.get_logger("unit-test-a"), log)
        self.assertEqual(len(log.handlers), 1)
        path = logsetup.log_dir() / "unit-test-a.log"
        self.assertIn("something happened", path.read_text())
        if sys.platform != "win32":
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_names_cannot_escape_the_folder(self):
        for bad in ("../x", "a/b", "", "a b"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                logsetup.get_logger(bad)
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                logsetup.open_stream(bad)

    def test_a_stream_is_rotated_when_it_passes_the_cap_and_keeps_three_copies(self):
        path = logsetup.log_dir() / "child.log"
        for round_ in range(5):
            path.write_bytes(b"x" * logsetup.MAX_BYTES + str(round_).encode())
            stream = logsetup.open_stream("child")
            assert stream is not None
            stream.write(b"fresh\n")
            stream.close()
            self.assertEqual(path.read_bytes(), b"fresh\n")
        names = sorted(p.name for p in path.parent.iterdir())
        self.assertEqual(names, ["child.log", "child.log.1", "child.log.2", "child.log.3"])
        if sys.platform != "win32":
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_an_unwritable_folder_never_raises(self):
        with patch.object(logsetup, "log_dir", side_effect=PermissionError):
            self.assertIsNone(logsetup.open_stream("nowhere"))
            logsetup.get_logger("nowhere").error("dropped quietly")


if __name__ == "__main__":
    unittest.main()
