"""The real clipboard service process against a real (virtual) X server."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import clipstore
import hotkeys
from support import make_png

ROOT = Path(__file__).resolve().parents[1]

try:
    from xowner import XOwner

    HAS_XLIB = True
except ImportError:
    HAS_XLIB = False


@unittest.skipUnless(HAS_XLIB, "python-xlib is not installed")
@unittest.skipUnless(
    os.environ.get("WWD_PRIVATE_DISPLAY"),
    "runs only through tests/with-xvfb.sh, never against a real desktop clipboard",
)
class ServiceProcessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        folder = Path(temporary.name)
        self.env = os.environ | {
            "XDG_CONFIG_HOME": str(folder / "config"),
            "XDG_CACHE_HOME": str(folder / "cache"),
            "XDG_RUNTIME_DIR": str(folder / "runtime"),
        }
        self.env.pop("WAYLAND_DISPLAY", None)  # Force the X11 watcher.
        self.runtime = folder / "runtime" / f"dictation-{os.getuid()}"
        self.config = folder / "config" / "dictation"
        self.config.mkdir(parents=True)
        isolated = patch.dict(
            os.environ,
            {
                key: self.env[key]
                for key in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR")
            },
        )
        isolated.start()
        self.addCleanup(isolated.stop)
        import dictation as d

        self.paths = d.Paths()
        hotkeys.Preferences(self.paths).save(features=hotkeys.Features(True, True))
        self.process = subprocess.Popen(
            [sys.executable, str(ROOT / "lib/clipservice.py")],
            env=self.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        self.addCleanup(self.stop)
        self.owner = XOwner()
        self.addCleanup(self.owner.close)
        self.wait_for(
            lambda: (self.paths.runtime / "clip-status.json").exists(), "the service to start"
        )
        time.sleep(0.5)  # The watcher is subscribed before the first copy.

    def stop(self):
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(5)
        if self.process.stderr:
            self.process.stderr.close()

    def wait_for(self, condition, what, timeout=10.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if condition():
                return
            time.sleep(0.1)
        self.fail(f"Timed out waiting for {what}")

    def items(self):
        store = clipstore.Store(self.paths.clipboard)
        try:
            return store.list(limit=100)
        finally:
            store.close()

    def test_copies_from_another_application_are_kept_and_quit_is_clean(self):
        self.owner.copy({"UTF8_STRING": "héllo from another app".encode()})
        self.wait_for(lambda: any(i.text for i in self.items()), "the text to be stored")
        png = make_png(20, 20, noise=True)
        self.owner.copy({"image/png": png})
        self.wait_for(
            lambda: any(i.kind == "image" for i in self.items()), "the image to be stored"
        )
        kinds = sorted(i.kind for i in self.items())
        self.assertEqual(kinds, ["image", "text"])
        self.assertEqual(
            [i.text for i in self.items() if i.kind == "text"], ["héllo from another app"]
        )
        # A password manager's copy is never stored.
        self.owner.copy({"UTF8_STRING": b"hunter2", "x-kde-passwordManagerHint": b"secret"})
        time.sleep(1.0)
        self.assertNotIn("hunter2", [i.text for i in self.items()])
        (self.paths.runtime / "clip-quit").write_text("quit")
        self.assertEqual(self.process.wait(10), 0)
        self.assertFalse((self.paths.runtime / "clip-quit").exists())


if __name__ == "__main__":
    unittest.main()
