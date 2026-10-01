"""Change the app version everywhere it is written, then let CI do the rest.

    python tools/bump_version.py 1.4.0

Merging the result to main makes `.github/workflows/release.yml` build and publish
v1.4.0 by itself: no tag to push and nothing to upload.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def replace(path: str, pattern: str, replacement: str) -> tuple[Path, str]:
    file = ROOT / path
    text = file.read_text(encoding="utf-8")
    updated, count = re.subn(pattern, replacement, text, count=1, flags=re.MULTILINE)
    if count != 1:
        raise SystemExit(f"{path}: could not find the version to change")
    return file, updated


def bump(version: str) -> None:
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise SystemExit("The version must look like 1.4.0")
    changes = [
        replace("lib/desktop.py", r'^APP_VERSION = "[^"]+"', f'APP_VERSION = "{version}"'),
        replace("pyproject.toml", r'^version = "[^"]+"', f'version = "{version}"'),
    ]
    # These are offline fallbacks used only if GitHub's release API is unavailable.
    # Keep each platform's bootstrap installer pointed at this same app release.
    changes.extend(
        (
            replace(
                "bootstrap.sh",
                r'^PINNED_TAG="v?\d+\.\d+\.\d+"',
                f'PINNED_TAG="v{version}"',
            ),
            replace(
                "bootstrap.ps1",
                r"^\$PinnedTag = 'v?\d+\.\d+\.\d+'",
                f"$PinnedTag = 'v{version}'",
            ),
        )
    )
    # Turn the running "Unreleased" notes into this version's section.
    changes.append(
        replace(
            "CHANGELOG.md",
            r"^## \[Unreleased\][^\n]*",
            f"## [Unreleased]\n\n## [{version}] - {__import__('datetime').date.today()}",
        )
    )
    # Validate every destination first; a missing marker cannot leave a partial bump.
    for file, updated in changes:
        file.write_text(updated, encoding="utf-8")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    bump(sys.argv[1])
    print(f"Version is now {sys.argv[1]}. Commit and merge to main to release it.")
