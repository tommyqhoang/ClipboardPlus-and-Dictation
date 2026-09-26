"""macOS and Windows watchers, exercised through fakes of the platform APIs.

The real pasteboard / Win32 layers are thin wrappers; everything that decides what is
captured lives in the watcher classes tested here. Smoke tests against the real API run
only on the matching operating system.
"""

from __future__ import annotations

import contextlib
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import clipwatch
import clipwatch_macos as mac
import clipwatch_windows as windows
from support import make_png


class FakeClock:
    """Time that only moves when a watcher sleeps, so polling loops run instantly."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakePasteboard:
    def __init__(self) -> None:
        self.count = 1
        self.contents: dict[str, str | bytes] = {}
        self.requested: list[str] = []

    def copy(self, **contents: str | bytes) -> None:
        self.count += 1
        self.contents = {name.replace("__", "."): value for name, value in contents.items()}

    def changeCount(self) -> int:  # noqa: N802 - the Cocoa name
        return self.count

    def types(self) -> list[str]:
        return list(self.contents)

    def stringForType_(self, kind: str) -> str | None:  # noqa: N802
        self.requested.append(kind)
        value = self.contents.get(kind)
        return value if isinstance(value, str) else None

    def dataForType_(self, kind: str) -> bytes | None:  # noqa: N802
        self.requested.append(kind)
        value = self.contents.get(kind)
        return value if isinstance(value, bytes) else None


class MacTests(unittest.TestCase):
    def setUp(self):
        self.board = FakePasteboard()
        self.clock = FakeClock()
        self.pools = 0

        @contextlib.contextmanager
        def pool():
            self.pools += 1
            yield

        self.pool = pool

    def watcher(self, converter=lambda tiff: b"", **limits):
        return mac.MacWatcher(
            self.board,
            converter,
            pool=self.pool,
            clock=self.clock,
            sleep=self.clock.sleep,
            **limits,
        )

    def test_content_already_on_the_clipboard_at_start_is_not_captured(self):
        self.board.copy(**{"public__utf8-plain-text": "before"})
        watcher = self.watcher()
        self.assertIsNone(watcher.next_change(1))

    def test_a_copy_is_captured_once(self):
        watcher = self.watcher()
        self.board.copy(**{"public__utf8-plain-text": "héllo"})
        self.assertEqual(watcher.next_change(1), clipwatch.Clip(text="héllo"))
        self.assertIsNone(watcher.next_change(1))
        self.assertGreaterEqual(self.pools, 1)  # Cocoa objects are released per read.

    def test_waiting_polls_until_the_timeout_then_reports_no_change(self):
        watcher = self.watcher()
        self.assertIsNone(watcher.next_change(2.0))
        self.assertAlmostEqual(self.clock.now, 2.0, delta=0.6)
        self.assertTrue(all(step <= 0.5 for step in self.clock.sleeps))

    def test_concealed_types_are_reported_and_content_is_never_read(self):
        for marker in mac.CONCEALED_TYPES:
            with self.subTest(marker=marker):
                board = FakePasteboard()
                watcher = mac.MacWatcher(
                    board, lambda t: b"", pool=self.pool, clock=self.clock, sleep=self.clock.sleep
                )
                board.copy(**{"public__utf8-plain-text": "secret", marker.replace(".", "__"): b"1"})
                self.assertEqual(watcher.next_change(1), clipwatch.Clip(concealed=True))
                self.assertEqual(board.requested, [])

    def test_png_images_are_taken_as_they_are(self):
        watcher = self.watcher()
        png = make_png()
        self.board.copy(**{"public__png": png})
        self.assertEqual(watcher.next_change(1), clipwatch.Clip(image_png=png))

    def test_tiff_images_are_converted_to_png(self):
        seen: list[bytes] = []

        def convert(tiff: bytes) -> bytes:
            seen.append(tiff)
            return b"converted"

        watcher = self.watcher(convert)
        self.board.copy(**{"public__tiff": b"II*\x00fake"})
        self.assertEqual(watcher.next_change(1), clipwatch.Clip(image_png=b"converted"))
        self.assertEqual(seen, [b"II*\x00fake"])

    def test_an_image_that_cannot_be_converted_is_skipped(self):
        watcher = self.watcher(lambda tiff: b"")
        self.board.copy(**{"public__tiff": b"garbage"})
        self.assertIsNone(watcher.next_change(1))

    def test_text_wins_over_an_image_offered_with_it(self):
        watcher = self.watcher()
        self.board.copy(**{"public__utf8-plain-text": "cells", "public__png": make_png()})
        self.assertEqual(watcher.next_change(1), clipwatch.Clip(text="cells"))

    def test_oversize_text_and_images_are_dropped(self):
        watcher = self.watcher(max_text=10, max_image=100)
        self.board.copy(**{"public__utf8-plain-text": "x" * 11})
        self.assertIsNone(watcher.next_change(1))
        self.board.copy(**{"public__png": b"x" * 101})
        self.assertIsNone(watcher.next_change(1))

    def test_a_huge_tiff_is_not_even_converted(self):
        converted: list[int] = []
        watcher = self.watcher(lambda t: converted.append(len(t)) or b"png", max_image=10)
        self.board.copy(**{"public__tiff": b"x" * (mac.TIFF_FACTOR * 10 + 1)})
        self.assertIsNone(watcher.next_change(1))
        self.assertEqual(converted, [])

    def test_unrelated_types_are_ignored(self):
        watcher = self.watcher()
        self.board.copy(**{"public__file-url": "file:///tmp/x", "public__html": "<b>x</b>"})
        self.assertIsNone(watcher.next_change(1))

    def test_close_is_harmless(self):
        self.watcher().close()

    def test_without_pyobjc_the_watcher_is_unavailable(self):
        with patch.object(mac, "import_module", side_effect=ImportError("AppKit")):
            with self.assertRaises(clipwatch.Unavailable):
                mac.create()


class FakeWin32:
    """The slice of the Win32 clipboard API the watcher uses."""

    def __init__(self) -> None:
        self.sequence_number = 10
        self.formats: dict[int | str, bytes | str] = {}
        self.busy = 0  # OpenClipboard fails this many times first.
        self.opens = 0
        self.closes = 0
        self.change_on_read = False

    def copy(self, formats: dict[int | str, bytes | str]) -> None:
        self.sequence_number += 1
        self.formats = formats

    def sequence(self) -> int:
        return self.sequence_number

    def open(self) -> bool:
        if self.busy:
            self.busy -= 1
            return False
        self.opens += 1
        return True

    def close(self) -> None:
        self.closes += 1

    def has(self, kind: int | str) -> bool:
        return kind in self.formats

    def text(self) -> str | None:
        value = self.formats.get(windows.CF_UNICODETEXT)
        if self.change_on_read:
            self.change_on_read = False
            self.sequence_number += 1
        return value if isinstance(value, str) else None

    def data(self, kind: int | str) -> bytes | None:
        value = self.formats.get(kind)
        return value if isinstance(value, bytes) else None

    def dword(self, kind: str) -> int | None:
        value = self.formats.get(kind)
        return int.from_bytes(value, "little") if isinstance(value, bytes) else None


class WindowsTests(unittest.TestCase):
    def setUp(self):
        self.win32 = FakeWin32()
        self.clock = FakeClock()

    def watcher(self, dib_to_png=lambda dib: b"", **limits):
        return windows.WindowsWatcher(
            self.win32, dib_to_png, clock=self.clock, sleep=self.clock.sleep, **limits
        )

    def test_content_already_on_the_clipboard_at_start_is_not_captured(self):
        self.win32.copy({windows.CF_UNICODETEXT: "before"})
        self.assertIsNone(self.watcher().next_change(1))

    def test_a_copy_is_captured_once_and_the_clipboard_is_released(self):
        watcher = self.watcher()
        self.win32.copy({windows.CF_UNICODETEXT: "héllo 🙂"})
        self.assertEqual(watcher.next_change(1), clipwatch.Clip(text="héllo 🙂"))
        self.assertIsNone(watcher.next_change(1))
        self.assertEqual(self.win32.opens, self.win32.closes)
        self.assertGreater(self.win32.opens, 0)

    def test_waiting_polls_until_the_timeout(self):
        watcher = self.watcher()
        self.assertIsNone(watcher.next_change(1.0))
        self.assertAlmostEqual(self.clock.now, 1.0, delta=0.3)

    def test_a_busy_clipboard_is_retried_briefly_then_read(self):
        watcher = self.watcher()
        self.win32.copy({windows.CF_UNICODETEXT: "late"})
        self.win32.busy = 3
        self.assertEqual(watcher.next_change(1), clipwatch.Clip(text="late"))

    def test_a_clipboard_that_stays_busy_is_picked_up_on_a_later_poll(self):
        watcher = self.watcher()
        self.win32.copy({windows.CF_UNICODETEXT: "eventually"})
        self.win32.busy = windows.OPEN_ATTEMPTS  # The first read gives up.
        self.assertEqual(watcher.next_change(2), clipwatch.Clip(text="eventually"))
        self.assertEqual(self.win32.opens, self.win32.closes)

    def test_the_clipboard_is_released_even_when_reading_fails(self):
        watcher = self.watcher()
        self.win32.copy({windows.CF_UNICODETEXT: "x"})
        with patch.object(self.win32, "text", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                watcher.next_change(1)
        self.assertEqual(self.win32.opens, self.win32.closes)

    def test_concealed_clips_are_reported_without_reading_content(self):
        cases = {
            "exclude flag": {windows.EXCLUDE_FORMAT: b"\x01\x00\x00\x00"},
            "not for history": {windows.HISTORY_FORMAT: (0).to_bytes(4, "little")},
            "not for cloud": {windows.CLOUD_FORMAT: (0).to_bytes(4, "little")},
        }
        for name, flags in cases.items():
            with self.subTest(name):
                win32 = FakeWin32()
                watcher = windows.WindowsWatcher(
                    win32, lambda d: b"", clock=self.clock, sleep=self.clock.sleep
                )
                win32.copy({windows.CF_UNICODETEXT: "secret", **flags})
                with patch.object(win32, "text") as read:
                    self.assertEqual(watcher.next_change(1), clipwatch.Clip(concealed=True))
                read.assert_not_called()

    def test_an_allowed_history_flag_does_not_conceal(self):
        watcher = self.watcher()
        self.win32.copy(
            {windows.CF_UNICODETEXT: "fine", windows.HISTORY_FORMAT: (1).to_bytes(4, "little")}
        )
        self.assertEqual(watcher.next_change(1), clipwatch.Clip(text="fine"))

    def test_png_is_preferred_over_a_bitmap(self):
        watcher = self.watcher()
        png = make_png()
        self.win32.copy({windows.PNG_FORMAT: png, windows.CF_DIB: b"bitmap"})
        self.assertEqual(watcher.next_change(1), clipwatch.Clip(image_png=png))

    def test_png_padding_from_an_oversized_memory_block_is_removed(self):
        watcher = self.watcher()
        png = make_png()
        self.win32.copy({windows.PNG_FORMAT: png + b"\x00" * 5})
        self.assertEqual(watcher.next_change(1), clipwatch.Clip(image_png=png))

    def test_a_bitmap_is_converted_to_png(self):
        seen: list[bytes] = []

        def convert(dib: bytes) -> bytes:
            seen.append(dib)
            return b"converted"

        watcher = self.watcher(convert)
        self.win32.copy({windows.CF_DIB: b"dib bytes"})
        self.assertEqual(watcher.next_change(1), clipwatch.Clip(image_png=b"converted"))
        self.assertEqual(seen, [b"dib bytes"])

    def test_text_wins_over_an_image_offered_with_it(self):
        watcher = self.watcher()
        self.win32.copy({windows.CF_UNICODETEXT: "cells", windows.PNG_FORMAT: make_png()})
        self.assertEqual(watcher.next_change(1), clipwatch.Clip(text="cells"))

    def test_oversize_content_is_dropped(self):
        watcher = self.watcher(max_text=5, max_image=50)
        self.win32.copy({windows.CF_UNICODETEXT: "x" * 6})
        self.assertIsNone(watcher.next_change(1))
        self.win32.copy({windows.PNG_FORMAT: b"x" * 51})
        self.assertIsNone(watcher.next_change(1))

    def test_a_change_during_the_read_is_read_again(self):
        watcher = self.watcher()
        self.win32.copy({windows.CF_UNICODETEXT: "first"})
        self.win32.change_on_read = True
        self.assertEqual(watcher.next_change(1).text, "first")
        # The sequence moved while reading, so the next poll reads the clipboard again.
        self.win32.formats = {windows.CF_UNICODETEXT: "second"}
        self.assertEqual(watcher.next_change(1).text, "second")

    def test_a_bitmap_conversion_failure_is_skipped(self):
        watcher = self.watcher(lambda dib: b"")
        self.win32.copy({windows.CF_DIB: b"broken"})
        self.assertIsNone(watcher.next_change(1))

    @unittest.skipIf(sys.platform == "win32", "needs a platform without Win32")
    def test_off_windows_the_watcher_is_unavailable(self):
        with self.assertRaises(clipwatch.Unavailable):
            windows.create()

    @unittest.skipUnless(sys.platform == "win32", "Windows only")
    def test_the_real_clipboard_reports_a_sequence_number(self):
        win32 = windows.Win32Clipboard()
        self.assertIsInstance(win32.sequence(), int)
        self.assertTrue(win32.open())
        win32.close()

    @unittest.skipUnless(sys.platform == "win32", "Windows only")
    def test_dib_data_converts_to_png_with_pillow(self):
        from PIL import Image

        image = Image.new("RGB", (4, 3), (10, 20, 30))
        import io

        buffer = io.BytesIO()
        image.save(buffer, "DIB")
        png = windows.dib_to_png(buffer.getvalue())
        self.assertTrue(png.startswith(b"\x89PNG"))


class DispatchTests(unittest.TestCase):
    def test_create_watcher_selects_the_platform_module(self):
        for platform, module in (("macos", mac), ("windows", windows)):
            with self.subTest(platform=platform):
                sentinel = object()
                with patch.object(module, "create", return_value=sentinel):
                    self.assertIs(clipwatch.create_watcher(platform), sentinel)


if __name__ == "__main__":
    unittest.main()
