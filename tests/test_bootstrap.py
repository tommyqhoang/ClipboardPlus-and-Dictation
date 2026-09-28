"""Exercise bootstrap orchestration without installing host packages."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipIf(sys.platform == "win32", "Unix bootstrap")
class BootstrapTests(unittest.TestCase):
    def invoke(self, platform="Linux", ref="main", fail=False):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            # Shell functions substitute every external mutation. The real
            # mktemp/mkdir/cleanup path remains confined to this temporary root.
            script = f"""source {shlex.quote(str(ROOT / "bootstrap.sh"))}
uname() {{ echo {shlex.quote(platform)}; }}
curl() {{ printf 'download %s\\n' "$*"; {"return 22" if fail else ":"}; }}
tar() {{ :; }}
brew() {{ echo 'brew dependencies'; }}
bash() {{ echo "linux installer $*"; }}
python3() {{ echo "desktop installer $*"; }}
main
"""
            result = subprocess.run(
                ["bash", "-c", script],
                capture_output=True,
                text=True,
                env=os.environ | {"TMPDIR": str(root), "DICTATION_REF": ref},
            )
            leftovers = list(root.iterdir())
        return result, leftovers

    def test_linux_and_macos_dispatch_and_cleanup(self):
        for platform, expected in (("Linux", "linux installer"), ("Darwin", "desktop installer")):
            result, leftovers = self.invoke(platform)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(expected, result.stdout)
            self.assertIn("Clipboard+ is installed", result.stdout)
            self.assertNotIn("--launch-only", result.stdout)
            self.assertNotIn("--setup", result.stdout)
            self.assertEqual(leftovers, [])

    def test_invalid_ref_platform_and_download_failure(self):
        for kwargs in ({"ref": "../unsafe"}, {"platform": "Windows"}, {"fail": True}):
            result, leftovers = self.invoke(**kwargs)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("walkthrough", result.stdout)
            self.assertEqual(leftovers, [])

    def test_documented_bash_c_invocation_runs_main(self):
        # README usage: bash -c "$(curl ...)" leaves BASH_SOURCE empty.
        script = (ROOT / "bootstrap.sh").read_text(encoding="utf-8")
        result = subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            env=os.environ | {"DICTATION_REF": "../unsafe"},
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("DICTATION_REF must be", result.stderr)
        self.assertNotIn("unbound variable", result.stderr)

    def test_windows_engine_download_is_versioned_and_checksum_pinned(self):
        script = (ROOT / "bootstrap.ps1").read_text(encoding="utf-8")
        self.assertIn("releases/download/v1.8.7/whisper-bin-x64.zip", script)
        self.assertIn(
            "d9627486e1c34a03745880485593473e047294260ce9a3cb0aa8deaf15b99af6",
            script,
        )
        self.assertIn("whisper-1.8.7", script)

    def test_uninstall_keeps_unrelated_modules_in_shared_lib(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            shared = home / ".local/lib"
            shared.mkdir(parents=True)
            for name in ("dictation.py", "desktop.py", "app.py"):
                (shared / name).write_text("unrelated application", encoding="utf-8")
            script = "gsettings() { return 1; }; source " + shlex.quote(str(ROOT / "uninstall.sh"))
            result = subprocess.run(
                ["bash", "-c", script],
                capture_output=True,
                text=True,
                env=os.environ | {"HOME": str(home)},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            for name in ("dictation.py", "desktop.py", "app.py"):
                self.assertEqual(
                    (shared / name).read_text(encoding="utf-8"), "unrelated application"
                )

    def test_uninstall_keeps_mixed_ownership_in_shared_lib(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            shared = home / ".local/lib"
            shared.mkdir(parents=True)
            (shared / "dictation.py").write_text("# whisper-dictation old app", encoding="utf-8")
            (shared / "desktop.py").write_text("# whisper-dictation old desktop", encoding="utf-8")
            (shared / "app.py").write_text("unrelated application", encoding="utf-8")
            script = "gsettings() { return 1; }; source " + shlex.quote(str(ROOT / "uninstall.sh"))
            result = subprocess.run(
                ["bash", "-c", script],
                capture_output=True,
                text=True,
                env=os.environ | {"HOME": str(home)},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((shared / "dictation.py").exists())
            self.assertFalse((shared / "desktop.py").exists())
            self.assertEqual(
                (shared / "app.py").read_text(encoding="utf-8"), "unrelated application"
            )


if __name__ == "__main__":
    unittest.main()
