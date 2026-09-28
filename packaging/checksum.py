"""Write a portable SHA-256 sidecar for a release artifact."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path


def write_checksum(path: Path) -> Path:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    sidecar = path.with_name(path.name + ".sha256")
    sidecar.write_text(f"{digest.hexdigest()}  {path.name}\n", encoding="ascii")
    return sidecar


if __name__ == "__main__":
    write_checksum(Path(sys.argv[1]))
