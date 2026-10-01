"""Exercise bootstrap orchestration without installing host packages."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipIf(sys.platform == "win32", "Unix bootstrap")
class BootstrapTests(unittest.TestCase):
    def invoke(self, platform="Linux", ref="", fail=False, mode="ok", nobrew=False):
        """Run main with a fake GitHub. mode: ok | tampered | nosums | offline."""
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            # Shell functions substitute every external mutation. The real
            # mktemp/mkdir/cleanup path remains confined to this temporary root.
            fail_line = "return 22" if fail else ":"
            no_brew = (
                "command() { if [[ \"$1 $2\" == '-v brew' ]]; then return 1; fi; "
                'builtin command "$@"; }'
                if nobrew
                else ""
            )
            script = f"""source {shlex.quote(str(ROOT / "bootstrap.sh"))}
MODE={mode}
uname() {{ echo {shlex.quote(platform)}; }}
curl() {{
  local out="" url="" prev="" a
  for a in "$@"; do
    [[ "$prev" == -o ]] && out="$a"
    [[ "$a" == https://* ]] && url="$a"
    prev="$a"
  done
  printf 'download %s\\n' "$url" >&2
  {fail_line}
  case "$url" in
    */releases/latest)
      [[ "$MODE" == offline ]] && return 22
      echo '{{"tag_name": "v9.9.9"}}' > "$out" ;;
    */SHA256SUMS)
      [[ "$MODE" == nosums ]] && return 22
      local t="${{url%/SHA256SUMS}}"; t="${{t##*/}}"
      echo "$(printf tarball | sha256sum | cut -d' ' -f1)  $t.tar.gz" > "$out" ;;
    */clipboardplus-source-*) return 22 ;;
    *.tar.gz)
      if [[ "$MODE" == tampered ]]; then printf tampered > "$out"; else printf tarball > "$out"; fi ;;
    *) return 22 ;;
  esac
}}
tar() {{ :; }}
brew() {{ echo 'brew dependencies'; }}
{no_brew}
bash() {{ echo "linux installer $*"; }}
python3() {{ echo "desktop installer $*"; }}
main
"""
            env = os.environ | {"TMPDIR": str(root)}
            env.pop("DICTATION_REF", None)
            if ref:
                env["DICTATION_REF"] = ref
            result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)
            leftovers = list(root.iterdir())
        return result, leftovers

    def test_default_is_the_latest_release_tag_not_main(self):
        result, _ = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Installing release v9.9.9", result.stdout)
        self.assertIn("refs/tags/v9.9.9.tar.gz", result.stderr)
        self.assertNotIn("/main", result.stderr)

    def test_pinned_tag_is_used_when_the_api_is_unreachable(self):
        result, _ = self.invoke(mode="offline")
        pinned = re.search(r'^PINNED_TAG="([^"]+)"', (ROOT / "bootstrap.sh").read_text(), re.M)
        assert pinned is not None
        self.assertIn(f"Installing release {pinned.group(1)}", result.stdout)

    def test_unix_and_windows_fallback_tags_match_the_app_version(self):
        version = re.search(
            r'^APP_VERSION = "([^"]+)"', (ROOT / "lib/desktop.py").read_text(), re.M
        )
        unix = re.search(r'^PINNED_TAG="([^"]+)"', (ROOT / "bootstrap.sh").read_text(), re.M)
        windows = re.search(r"^\$PinnedTag = '([^']+)'", (ROOT / "bootstrap.ps1").read_text(), re.M)
        assert version is not None and unix is not None and windows is not None
        expected = f"v{version.group(1)}"
        self.assertEqual(unix.group(1), expected)
        self.assertEqual(windows.group(1), expected)

    def test_checksum_mismatch_or_missing_checksum_fails_closed(self):
        for mode in ("tampered", "nosums"):
            with self.subTest(mode=mode):
                result, leftovers = self.invoke(mode=mode)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("linux installer", result.stdout)
                self.assertEqual(leftovers, [])

    def test_missing_homebrew_is_never_installed_from_a_piped_script(self):
        result, _ = self.invoke("Darwin", nobrew=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("brew.sh", result.stderr)
        self.assertNotIn("raw.githubusercontent.com", result.stderr)
        self.assertNotIn("Homebrew/install", (ROOT / "bootstrap.sh").read_text())

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

    def test_windows_app_download_is_release_pinned_and_hash_verified(self):
        script = (ROOT / "bootstrap.ps1").read_text(encoding="utf-8")
        self.assertIn("[string]$Ref = ''", script)
        self.assertNotIn("'main'", script)
        self.assertIn("releases/latest", script)
        self.assertIn("$PinnedTag", script)
        self.assertIn("SHA256SUMS", script)
        self.assertIn("refusing to install", script)
        # The hash check must come before the archive is expanded.
        self.assertLess(
            script.index("did not match its published"),
            script.index("Expand-Archive -LiteralPath $archive"),
        )
        self.assertIn("ffmpeg", script.lower())

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
