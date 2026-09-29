"""Change the version everywhere it is written, then let CI do the rest.

    python tools/bump_version.py 1.4.0

Merging the result to main makes `.github/workflows/release.yml` build and publish
v1.4.0 by itself: no tag to push and nothing to upload.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def replace(path: str, pattern: str, replacement: str) -> None:
    file = ROOT / path
    text = file.read_text(encoding="utf-8")
    updated, count = re.subn(pattern, replacement, text, count=1, flags=re.MULTILINE)
    if count != 1:
        raise SystemExit(f"{path}: could not find the version to change")
    file.write_text(updated, encoding="utf-8")


def bump(version: str) -> None:
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise SystemExit("The version must look like 1.4.0")
    replace("lib/desktop.py", r'^APP_VERSION = "[^"]+"', f'APP_VERSION = "{version}"')
    replace("pyproject.toml", r'^version = "[^"]+"', f'version = "{version}"')
    # Turn the running "Unreleased" notes into this version's section.
    replace(
        "CHANGELOG.md",
        r"^## \[Unreleased\][^\n]*",
        f"## [Unreleased]\n\n## [{version}] - {__import__('datetime').date.today()}",
    )


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    bump(sys.argv[1])
    print(f"Version is now {sys.argv[1]}. Commit and merge to main to release it.")
