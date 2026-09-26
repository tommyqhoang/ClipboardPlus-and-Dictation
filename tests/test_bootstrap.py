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
            self.assertIn("--launch-only", result.stdout)
            self.assertNotIn("--setup", result.stdout)
            self.assertEqual(leftovers, [])

    def test_invalid_ref_platform_and_download_failure(self):
        for kwargs in ({"ref": "../unsafe"}, {"platform": "Windows"}, {"fail": True}):
            result, leftovers = self.invoke(**kwargs)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("walkthrough", result.stdout)
            self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
