"""Checksums for release artifacts must work on every OS runner."""

from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packaging"))
import checksum

REPO_ROOT = Path(__file__).resolve().parents[1]


class ChecksumTests(unittest.TestCase):
    def test_sidecar_hashes_binary_content_and_uses_the_asset_filename(self):
        with tempfile.TemporaryDirectory() as folder:
            asset = Path(folder) / "Clipboard+-Setup.exe"
            asset.write_bytes(b"\x00release\xff")
            sidecar = checksum.write_checksum(asset)
            digest = hashlib.sha256(asset.read_bytes()).hexdigest()
            self.assertEqual(sidecar.read_text(encoding="ascii"), f"{digest}  {asset.name}\n")


class MacBundleSigningTests(unittest.TestCase):
    """The DMG must not ship unsigned: Apple Silicon shows a quarantined
    unsigned app as "damaged and can't be opened" with no bypass on macOS
    15+, and dylibbundler invalidates Homebrew ffmpeg's signatures."""

    def test_build_dmg_adhoc_signs_and_verifies_every_macho(self):
        script = (REPO_ROOT / "packaging" / "macos" / "build-dmg.sh").read_text(encoding="utf-8")
        self.assertIn("codesign --force --sign -", script)
        self.assertIn('codesign --verify --deep --strict --verbose=2 "$APP"', script)
        # The per-file signing loop must re-sign dylibbundler-modified
        # binaries, i.e. iterate Contents/MacOS, not just the top-level exe.
        self.assertIn('find "$APP/Contents/MacOS"', script)
        bundle_sign_pos = script.index('codesign --force --sign - "$APP"')
        verify_pos = script.index("codesign --verify")
        self.assertLess(bundle_sign_pos, verify_pos, "verification must follow bundling signing")

    def test_dmg_ships_a_double_click_installer_that_strips_quarantine(self):
        script = (REPO_ROOT / "packaging" / "macos" / "build-dmg.sh").read_text(encoding="utf-8")
        self.assertIn("Install Clipboard+.command", script)
        self.assertIn("xattr -dr com.apple.quarantine", script)
        # Gatekeeper does not assess .command scripts — the helper is the
        # unsigned build's first-launch story, so it must land in the DMG
        # staging folder, next to the app.
        helper_pos = script.index("Install Clipboard+.command")
        hdiutil_pos = script.index("hdiutil create")
        self.assertLess(
            helper_pos, hdiutil_pos, "installer helper must be staged before the DMG is created"
        )


if __name__ == "__main__":
    unittest.main()
