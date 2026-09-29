"""Private folders and files: race-safe runtime folder, owner-only writes, Windows ACLs."""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import desktop

POSIX = sys.platform != "win32"


@unittest.skipUnless(POSIX, "POSIX ownership and mode bits")
class PrivateDirTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)

    def test_a_new_folder_is_made_private(self):
        target = self.base / "run"
        self.assertTrue(desktop.make_private_dir(target))
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)
        self.assertTrue(desktop.make_private_dir(target))  # Ours already: accepted.

    def test_a_umask_of_zero_still_gives_0700(self):
        previous = os.umask(0)
        try:
            self.assertTrue(desktop.make_private_dir(self.base / "run"))
        finally:
            os.umask(previous)
        self.assertEqual(stat.S_IMODE((self.base / "run").stat().st_mode), 0o700)

    def test_a_folder_planted_with_open_permissions_is_refused(self):
        target = self.base / "run"
        target.mkdir(mode=0o755)
        target.chmod(0o755)
        self.assertFalse(desktop.make_private_dir(target))

    def test_a_planted_symbolic_link_is_refused_not_followed(self):
        elsewhere = self.base / "attacker"
        elsewhere.mkdir(mode=0o700)
        link = self.base / "run"
        link.symlink_to(elsewhere, target_is_directory=True)
        self.assertFalse(desktop.make_private_dir(link))

    def test_a_folder_owned_by_someone_else_is_refused(self):
        target = self.base / "run"
        target.mkdir(mode=0o700)
        with patch.object(desktop.os, "getuid", return_value=os.getuid() + 1):
            self.assertFalse(desktop.make_private_dir(target))

    def test_a_file_in_the_way_is_refused(self):
        target = self.base / "run"
        target.write_text("x")
        self.assertFalse(desktop.make_private_dir(target))

    def test_the_fallback_moves_under_the_cache_folder_when_the_temp_name_is_taken(self):
        temp = self.base / "tmp"
        temp.mkdir()
        (temp / f"dictation-{desktop.user_id()}").mkdir(mode=0o755)
        (temp / f"dictation-{desktop.user_id()}").chmod(0o755)
        cache = self.base / "cache" / "dictation"
        with (
            patch.dict(os.environ, {"TMPDIR": str(temp)}),
            patch.object(desktop.tempfile, "gettempdir", return_value=str(temp)),
        ):
            found = desktop._runtime_fallback(cache)
        self.assertEqual(found, cache / "run")
        self.assertEqual(stat.S_IMODE(found.stat().st_mode), 0o700)

    def test_the_fallback_prefers_the_per_user_temp_folder(self):
        user_temp = self.base / "user-tmp"
        user_temp.mkdir(mode=0o700)
        with patch.dict(os.environ, {"TMPDIR": str(user_temp)}):
            found = desktop._runtime_fallback(self.base / "cache")
        self.assertEqual(found, user_temp / f"dictation-{desktop.user_id()}")

    def test_roots_uses_xdg_runtime_dir_before_any_temp_folder(self):
        with (
            patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.base)}),
            patch.object(desktop, "_runtime_fallback") as fallback,
        ):
            runtime = desktop.roots()[2]
        fallback.assert_not_called()
        self.assertEqual(runtime, self.base / f"dictation-{desktop.user_id()}")


class WritePrivateTests(unittest.TestCase):
    def test_the_file_is_owner_only_whatever_the_umask_and_replaces_atomically(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "secret"
            previous = os.umask(0)
            try:
                desktop.write_private(path, "one")
                desktop.write_private(path, b"two")
            finally:
                os.umask(previous)
            self.assertEqual(path.read_bytes(), b"two")
            if POSIX:
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual([p.name for p in Path(folder).iterdir()], ["secret"])


class WindowsAclTests(unittest.TestCase):
    def setUp(self):
        desktop._restricted.clear()

    def run_restrict(self, path: Path, code: int = 0) -> tuple[bool, Mock]:
        with (
            patch.object(sys, "platform", "win32"),
            patch.dict(os.environ, {"USERNAME": "tester", "USERDOMAIN": "PC"}),
            patch.object(desktop.subprocess, "run", return_value=Mock(returncode=code)) as run,
        ):
            return desktop.restrict_to_owner(path), run

    def test_a_folder_gets_an_inherited_owner_only_grant(self):
        with tempfile.TemporaryDirectory() as folder:
            ok, run = self.run_restrict(Path(folder))
        self.assertTrue(ok)
        argv = run.call_args.args[0]
        self.assertEqual(argv[0], "icacls")
        self.assertIn("/inheritance:r", argv)
        self.assertIn("PC\\tester:(OI)(CI)F", argv)

    def test_a_file_gets_a_plain_grant_and_repeats_are_skipped(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "key"
            path.write_text("x")
            ok, run = self.run_restrict(path)
            self.assertIn("PC\\tester:F", run.call_args.args[0])
            self.assertTrue(ok)
            ok, run = self.run_restrict(path)
            run.assert_not_called()

    def test_a_failed_or_missing_icacls_is_reported_not_raised(self):
        with tempfile.TemporaryDirectory() as folder:
            ok, _ = self.run_restrict(Path(folder), code=5)
            self.assertFalse(ok)
            with (
                patch.object(sys, "platform", "win32"),
                patch.dict(os.environ, {"USERNAME": "tester"}),
                patch.object(desktop.subprocess, "run", side_effect=FileNotFoundError),
            ):
                self.assertFalse(desktop.restrict_to_owner(Path(folder)))

    @unittest.skipUnless(POSIX, "POSIX modes")
    def test_posix_restriction_is_a_chmod(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "f"
            path.write_text("x")
            path.chmod(0o666)
            self.assertTrue(desktop.restrict_to_owner(path))
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertTrue(desktop.restrict_to_owner(Path(folder)))
            self.assertEqual(stat.S_IMODE(Path(folder).stat().st_mode), 0o700)


if __name__ == "__main__":
    unittest.main()
