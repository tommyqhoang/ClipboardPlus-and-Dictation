"""Checksums for release artifacts must work on every OS runner."""

from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packaging"))
import checksum


class ChecksumTests(unittest.TestCase):
    def test_sidecar_hashes_binary_content_and_uses_the_asset_filename(self):
        with tempfile.TemporaryDirectory() as folder:
            asset = Path(folder) / "Clipboard+-Setup.exe"
            asset.write_bytes(b"\x00release\xff")
            sidecar = checksum.write_checksum(asset)
            digest = hashlib.sha256(asset.read_bytes()).hexdigest()
            self.assertEqual(sidecar.read_text(encoding="ascii"), f"{digest}  {asset.name}\n")


if __name__ == "__main__":
    unittest.main()
