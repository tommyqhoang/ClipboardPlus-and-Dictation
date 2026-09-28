"""Launch each frozen entry point on CI and detect missing bundled imports."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ENTRIES = ("tray", "menubar", "app", "dictation", "engine", "overlay", "updates", "clipservice")
IMPORT_ERRORS = ("Traceback (most recent call last)", "ModuleNotFoundError", "ImportError")


def main() -> int:
    folder = Path("dist/clipboardplus")
    suffix = ".exe" if os.name == "nt" else ""
    found = 0
    failures = 0
    for entry in ENTRIES:
        binary = folder / (entry + suffix)
        if not binary.is_file():
            continue
        found += 1
        command = [str(binary), "--status"] if entry == "dictation" else [str(binary)]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=5)
            output = result.stdout + result.stderr
            crashed = any(marker in output for marker in IMPORT_ERRORS)
            print(f"{'FAIL' if crashed else 'OK'}: {entry} (exit {result.returncode})")
            if crashed:
                print(output)
                failures += 1
        except subprocess.TimeoutExpired:
            print(f"OK: {entry} (still running after 5 seconds)")
    if found != 7:
        print(f"FAIL: expected 7 frozen entry points, found {found}")
        failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
