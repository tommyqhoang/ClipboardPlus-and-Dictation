"""Clipboard watchers: shared limits, target choice, wl-paste and a real X11 server."""

from __future__ import annotations

import os
import socket
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import clipwatch
import clipwatch_linux as linux
from support import make_png

try:
    from Xlib import display as xdisplay
    from xowner import XOwner

    HAS_XLIB = True
except ImportError:
    HAS_XLIB = False

try:
    import PIL  # noqa: F401

    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False


class SharedTests(unittest.TestCase):
    def test_limit_clip_drops_oversize_parts_and_empty_clips(self):
        text = clipwatch.Clip(text="hello")
        self.assertEqual(clipwatch.limit_clip(text), text)
        self.assertIsNone(clipwatch.limit_clip(clipwatch.Clip(text="x" * 11), max_text=10))
        self.assertIsNone(clipwatch.limit_clip(clipwatch.Clip(image_png=b"x" * 11), max_image=10))
        self.assertIsNone(clipwatch.limit_clip(clipwatch.Clip()))
        self.assertIsNone(clipwatch.limit_clip(clipwatch.Clip(text="  \n\t")))
        both = clipwatch.Clip(text="cells", image_png=b"png")
        self.assertEqual(clipwatch.limit_clip(both), both)
        kept = clipwatch.limit_clip(clipwatch.Clip(text="x" * 50, image_png=b"png"), max_text=10)
        self.assertEqual(kept, clipwatch.Clip(image_png=b"png"))

    def test_the_limit_is_in_bytes_not_characters(self):
        self.assertIsNone(clipwatch.limit_clip(clipwatch.Clip(text="é" * 6), max_text=10))
        self.assertIsNotNone(clipwatch.limit_clip(clipwatch.Clip(text="é" * 5), max_text=10))

    def test_a_concealed_clip_never_carries_content(self):
        secret = clipwatch.Clip(text="hunter2", image_png=b"png", concealed=True)
        self.assertEqual(clipwatch.limit_clip(secret), clipwatch.Clip(concealed=True))

    def test_copied_api_keys_and_private_keys_are_never_kept(self):
        for secret in (
            "sk-" + "proj-abcdefghijklmnopqrstuvwx",
            "  gh" + "p_abcdefghijklmnopqrstuvwxyz0123456789\n",
            "cp_live_abcdefghijklmnop1234",
            "AK" + "IAABCDEFGHIJKLMNOP",
            "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXk\n-----END OPENSSH PRIVATE KEY-----",
        ):
            with self.subTest(secret=secret[:12]):
                self.assertEqual(
                    clipwatch.limit_clip(clipwatch.Clip(text=secret)),
                    clipwatch.Clip(concealed=True),
                )
        for ordinary in (
            "sk-",
            "Use your sk-" + "proj-abcdefghijklmnopqrstuvwx key here",  # Prose around it is kept.
            "https://example.com/a/very/long/path/with/segments",
            "0123456789abcdef0123456789abcdef0123456789",
        ):
            with self.subTest(ordinary=ordinary[:12]):
                self.assertEqual(
                    clipwatch.limit_clip(clipwatch.Clip(text=ordinary)),
                    clipwatch.Clip(text=ordinary),
                )

    def test_broader_secret_formats_are_never_kept(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N"
        for secret in (
            jwt,
            "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----",
            "-----BEGIN PGP PRIVATE KEY BLOCK-----\nabc",
            "sk-" + "ant-api03-abcdefghijklmnopqrstuvwx",
            "sk_" + "live_abcdefghijklmnop1234",
            "xox" + "b-123456789012-abcdefghijklmnopqrstu",
            "AI" + "zaSyA-abcdefghijklmnopqrstuvwxyz01234",
            "ya29.a0AfH6SMBabcdefghijklmnop",
            "AS" + "IAABCDEFGHIJKLMNOP",
            "482913",  # A one-time code copied alone.
            "12345678",
            "4111 1111 1111 1111",  # A card number (passes Luhn).
            "5500-0000-0000-0004",
            "Xk9fQ2mZp8Lr3vTn7YwB1aHd5CjE0sGuQ4",  # One long random-looking token.
            f"Authorization: Bearer {jwt}",  # A secret inside longer text.
            "export AWS_KEY=AK" + "IAABCDEFGHIJKLMNOP now",
        ):
            with self.subTest(secret=secret[:14]):
                self.assertTrue(clipwatch.looks_secret(secret))
                self.assertEqual(
                    clipwatch.limit_clip(clipwatch.Clip(text=secret)),
                    clipwatch.Clip(concealed=True),
                )

    def test_ordinary_text_that_resembles_secrets_is_kept(self):
        for ordinary in (
            "12345",  # Too short for a code.
            "123456789",  # Too long for a code, not a card.
            "4111 1111 1111 1112",  # Fails Luhn.
            "call 482913 today",  # A code inside prose.
            "0123456789abcdef0123456789abcdef01234567",  # A git hash (hex only).
            "thisIsAVeryLongCamelCaseIdentifierNameHereOk",  # No digits.
            "/usr/local/share/applications/some-long-directory/name",
            "2024-01-05",
            "The quick brown fox jumps over the lazy dog, again and again.",
            "eyJ",
        ):
            with self.subTest(ordinary=ordinary[:14]):
                self.assertFalse(clipwatch.looks_secret(ordinary))

    def test_copies_from_password_managers_are_skipped_by_app(self):
        for app in (
            "com.1password.1password",
            "com.agilebits.onepassword7",
            "com.bitwarden.desktop",
            "org.keepassxc.KeePassXC",
            "com.lastpass.LastPass",
            "com.dashlane.dashlanephonefinal",
            "com.apple.keychainaccess",
            "KeePassXC.exe",
            "Bitwarden.exe",
        ):
            with self.subTest(app=app):
                self.assertEqual(
                    clipwatch.limit_clip(clipwatch.Clip(text="hello there", source_app=app)),
                    clipwatch.Clip(concealed=True),
                )
        for app in ("", "org.mozilla.firefox", "com.apple.Safari", "notepad.exe"):
            with self.subTest(app=app):
                self.assertEqual(
                    clipwatch.limit_clip(clipwatch.Clip(text="hello there", source_app=app)),
                    clipwatch.Clip(text="hello there"),
                )

    def test_trim_png_cuts_trailing_padding_only(self):
        png = make_png()
        self.assertEqual(clipwatch.trim_png(png + b"\x00\x00"), png)
        self.assertEqual(clipwatch.trim_png(png), png)
        self.assertEqual(clipwatch.trim_png(b"not a png"), b"not a png")

    def test_any_object_with_the_two_methods_is_a_watcher(self):
        class Fake:
            def next_change(self, timeout: float) -> clipwatch.Clip | None:
                return None

            def close(self) -> None:
                pass

        self.assertIsInstance(Fake(), clipwatch.Watcher)
        self.assertNotIsInstance(object(), clipwatch.Watcher)

    def test_a_missing_platform_watcher_is_reported_clearly(self):
        with patch.object(clipwatch, "_platform_watcher", side_effect=clipwatch.Unavailable("no")):
            with self.assertRaises(clipwatch.Unavailable):
                clipwatch.create_watcher("linux")
        with self.assertRaises(clipwatch.Unavailable):
            clipwatch.create_watcher("plan9")


class ChoiceTests(unittest.TestCase):
    def test_text_is_preferred_and_image_is_the_fallback(self):
        both = linux.choose(["TARGETS", "image/png", "UTF8_STRING", "text/html"])
        self.assertEqual((both.text_target, both.image_target), ("UTF8_STRING", "image/png"))
        self.assertEqual(linux.choose(["image/png", "image/jpeg"]).text_target, "")
        self.assertEqual(linux.choose(["image/png"]).image_target, "image/png")
        self.assertFalse(linux.choose(["image/png"]).image_needs_conversion)
        self.assertEqual(linux.choose(["text/html"]), linux.Choice(False, "", ""))

    def test_a_non_png_image_is_offered_as_a_fallback_needing_conversion(self):
        # Some apps (older GTK, LibreOffice) never offer image/png at all.
        jpeg = linux.choose(["text/html", "image/jpeg"])
        self.assertEqual(jpeg.image_target, "image/jpeg")
        self.assertTrue(jpeg.image_needs_conversion)
        # image/png still wins over a fallback format when both are offered.
        both = linux.choose(["image/bmp", "image/png"])
        self.assertEqual((both.image_target, both.image_needs_conversion), ("image/png", False))

    def test_image_conversion_rejects_garbage(self):
        self.assertEqual(linux.image_to_png(b"not an image"), b"")

    @unittest.skipUnless(HAS_PILLOW, "Pillow not installed")
    def test_image_conversion_decodes_a_non_png_format(self):
        from io import BytesIO

        from PIL import Image

        buffer = BytesIO()
        Image.new("RGB", (2, 2), "red").save(buffer, "BMP")
        png = linux.image_to_png(buffer.getvalue())
        self.assertTrue(png.startswith(b"\x89PNG"))

    def test_text_targets_are_tried_in_order_of_fidelity(self):
        self.assertEqual(linux.choose(["STRING", "text/plain"]).text_target, "text/plain")
        self.assertEqual(
            linux.choose(["STRING", "text/plain;charset=utf-8", "text/plain"]).text_target,
            "text/plain;charset=utf-8",
        )
        self.assertEqual(linux.choose(["STRING"]).text_target, "STRING")

    def test_the_password_manager_hint_marks_the_clip_concealed(self):
        marked = linux.choose(["UTF8_STRING", "x-kde-passwordManagerHint"])
        self.assertTrue(marked.concealed)

    def test_text_decoding(self):
        self.assertEqual(linux.decode_text("UTF8_STRING", "héllo".encode()), "héllo")
        self.assertEqual(linux.decode_text("STRING", "héllo".encode("latin-1")), "héllo")
        self.assertEqual(linux.decode_text("text/plain", b"abc\x00\x00"), "abc")
        # Invalid bytes are replaced, never raised.
        self.assertEqual(linux.decode_text("UTF8_STRING", b"a\xffb"), "a�b")


class FakeWatchProcess:
    """Stands in for `wl-paste --watch`: a line on the pipe is one clipboard change.

    A socket pair rather than os.pipe(), because Windows only select()s on sockets.
    """

    def __init__(self) -> None:
        self._writer, self._reader = socket.socketpair()
        self.stdout = self._reader.makefile("r")
        self.terminated = False
        self.exit_code: int | None = None

    def change(self) -> None:
        self._writer.sendall(b"changed\n")

    def close(self) -> None:
        self.stdout.close()
        self._reader.close()
        self._writer.close()

    def poll(self) -> int | None:
        return self.exit_code

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        return 0


class WlPasteTests(unittest.TestCase):
    def source(self, clipboard: dict[str, bytes], types_output: str | None = None):
        process = FakeWatchProcess()
        self.addCleanup(process.close)
        reads: list[list[str]] = []

        def reader(command, max_bytes):
            reads.append(list(command))
            if command[1] == "--list-types":
                return (types_output or "\n".join(clipboard)).encode()
            data = clipboard.get(command[-1])
            return None if data is None or len(data) > max_bytes else data

        # Well past the startup grace period, so changes count.
        source = linux.WlPasteSource(
            process, reader, max_text=1000, max_image=1000, clock=lambda: 1e9
        )
        source._started = 0.0
        return source, process, reads

    def test_wait_reports_a_change_and_times_out_otherwise(self):
        source, process, _ = self.source({})
        self.assertFalse(source.wait(0.05))
        process.change()
        process.change()  # A second line is the same burst, not another change.
        self.assertTrue(source.wait(1))
        self.assertFalse(source.wait(0.05))

    def test_the_startup_notification_for_existing_content_is_ignored(self):
        clock = [100.0]
        process = FakeWatchProcess()
        self.addCleanup(process.close)
        source = linux.WlPasteSource(process, lambda c, m: b"", clock=lambda: clock[0])
        process.change()  # wl-paste reports the current selection right after it starts.
        self.assertFalse(source.wait(0.05))
        clock[0] += 5
        process.change()  # A real copy later on.
        self.assertTrue(source.wait(1))

    def test_reads_text_using_the_best_target(self):
        source, _, reads = self.source(
            {"text/plain;charset=utf-8": "héllo".encode(), "text/plain": b"fallback"}
        )
        self.assertEqual(source.read(), clipwatch.Clip(text="héllo"))
        self.assertEqual(reads[-1][-1], "text/plain;charset=utf-8")

    def test_reads_an_image_when_there_is_no_text(self):
        png = make_png()
        source, _, _ = self.source({"image/png": png})
        self.assertEqual(source.read(), clipwatch.Clip(image_png=png))

    def test_concealed_content_is_never_requested(self):
        source, _, reads = self.source(
            {"text/plain": b"secret", "x-kde-passwordManagerHint": b"secret"}
        )
        self.assertEqual(source.read(), clipwatch.Clip(concealed=True))
        self.assertEqual([command[1] for command in reads], ["--list-types"])

    def test_oversize_and_unreadable_content_is_skipped(self):
        source, _, _ = self.source({"text/plain": b"x" * 2000})
        self.assertEqual(source.read(), clipwatch.Clip())
        source, _, _ = self.source({}, types_output="text/plain")
        self.assertEqual(source.read(), clipwatch.Clip())

    def test_close_stops_the_watcher_process(self):
        source, process, _ = self.source({})
        source.close()
        self.assertTrue(process.terminated)

    def test_a_dead_watcher_is_reported(self):
        source, process, _ = self.source({})
        process.exit_code = 1
        with self.assertRaises(clipwatch.Unavailable):
            source.wait(0.05)


class CreateTests(unittest.TestCase):
    def test_wl_paste_is_used_when_the_compositor_supports_watching(self):
        with (
            patch.object(linux, "wl_paste_can_watch", return_value=True),
            patch.object(linux, "WlPasteSource") as wl,
            patch.object(linux, "X11Source") as x11,
        ):
            linux.create(
                {"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0"}, which=lambda _: "/bin/x"
            )
        wl.assert_called_once()
        x11.assert_not_called()

    def test_x11_is_used_when_wl_paste_cannot_watch(self):
        # GNOME's compositor refuses `wl-paste --watch`; XWayland still works.
        with (
            patch.object(linux, "wl_paste_can_watch", return_value=False),
            patch.object(linux, "WlPasteSource") as wl,
            patch.object(linux, "X11Source") as x11,
        ):
            linux.create(
                {"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0"}, which=lambda _: "/bin/x"
            )
        x11.assert_called_once()
        wl.assert_not_called()

    def test_nothing_available_is_an_error_that_says_why(self):
        with patch.object(linux, "wl_paste_can_watch", return_value=False):
            with self.assertRaisesRegex(clipwatch.Unavailable, "clipboard"):
                linux.create({"WAYLAND_DISPLAY": "wayland-0"}, which=lambda _: None)

    def test_an_x11_connection_failure_is_unavailable_not_a_crash(self):
        with patch.object(linux, "X11Source", side_effect=OSError("cannot connect")):
            with self.assertRaises(clipwatch.Unavailable):
                linux.create({"DISPLAY": ":99"}, which=lambda _: None)

    def test_wl_paste_probe_sees_an_immediate_exit_as_unsupported(self):
        class Exits:
            def poll(self):
                return 1

            def terminate(self):
                pass

            def wait(self, timeout=None):
                return 1

        class Stays(Exits):
            terminated = False

            def poll(self):
                return None

            def terminate(self):
                type(self).terminated = True

        self.assertFalse(linux.wl_paste_can_watch(lambda command: Exits(), settle=0.01))
        self.assertTrue(linux.wl_paste_can_watch(lambda command: Stays(), settle=0.05))
        self.assertTrue(Stays.terminated)
        self.assertFalse(linux.wl_paste_can_watch(self.raise_missing, settle=0.01))

    @staticmethod
    def raise_missing(command):
        raise FileNotFoundError("wl-paste")


@unittest.skipUnless(HAS_XLIB, "python-xlib is not installed")
@unittest.skipUnless(
    os.environ.get("WWD_PRIVATE_DISPLAY"),
    "these tests take over the X clipboard: run them through tests/with-xvfb.sh, "
    "never against a real desktop",
)
class X11Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            xdisplay.Display().close()
        except Exception as exc:  # noqa: BLE001 - no X server: skip the class
            raise unittest.SkipTest(f"No X server: {exc}")

    def setUp(self):
        self.owner = XOwner(chunk=2000)
        self.addCleanup(self.owner.close)

    def watcher(self, **limits):
        watcher = linux.LinuxWatcher(linux.X11Source(**limits))
        self.addCleanup(watcher.close)
        return watcher

    def test_text_copied_by_another_application_is_captured(self):
        watcher = self.watcher()
        self.owner.copy({"UTF8_STRING": "héllo wörld".encode()})
        self.assertEqual(watcher.next_change(3), clipwatch.Clip(text="héllo wörld"))

    def test_no_copy_means_no_change(self):
        self.assertIsNone(self.watcher().next_change(0.2))

    def test_each_copy_is_seen_once(self):
        watcher = self.watcher()
        self.owner.copy({"UTF8_STRING": b"one"})
        self.assertEqual(watcher.next_change(3).text, "one")
        self.assertIsNone(watcher.next_change(0.2))
        self.owner.copy({"UTF8_STRING": b"two"})
        self.assertEqual(watcher.next_change(3).text, "two")

    def test_the_latest_of_several_rapid_copies_wins(self):
        watcher = self.watcher()
        for text in (b"a", b"b", b"c"):
            self.owner.copy({"UTF8_STRING": text})
        self.assertEqual(watcher.next_change(3).text, "c")

    def test_a_large_image_arrives_through_incremental_transfer(self):
        watcher = self.watcher(max_image=500_000)
        png = make_png(60, 50, noise=True)
        self.assertGreater(len(png), 2000)  # More than one INCR chunk.
        self.owner.copy({"image/png": png})
        self.assertEqual(watcher.next_change(5), clipwatch.Clip(image_png=png))

    def test_large_text_arrives_intact(self):
        watcher = self.watcher()
        text = ("line of text 🙂\n" * 800).encode()
        self.owner.copy({"UTF8_STRING": text})
        self.assertEqual(watcher.next_change(5).text, text.decode())

    def test_text_wins_over_an_image_offered_alongside_it(self):
        watcher = self.watcher()
        self.owner.copy({"UTF8_STRING": b"cell value", "image/png": make_png()})
        clip = watcher.next_change(3)
        self.assertEqual(clip.text, "cell value")

    def test_concealed_content_is_never_requested(self):
        watcher = self.watcher()
        self.owner.copy({"UTF8_STRING": b"hunter2", "x-kde-passwordManagerHint": b"secret"})
        self.assertEqual(watcher.next_change(3), clipwatch.Clip(concealed=True))
        self.assertNotIn("UTF8_STRING", self.owner.requests)

    def test_oversize_content_is_dropped_without_reading_it_all(self):
        watcher = self.watcher(max_text=1000)
        self.owner.copy({"UTF8_STRING": b"x" * 50_000})
        self.assertIsNone(watcher.next_change(3))
        # The transfer was abandoned after the limit, not streamed to the end.
        self.assertEqual(self.owner.requests.count("UTF8_STRING"), 1)

    def test_a_formatless_or_unsupported_clip_is_ignored(self):
        watcher = self.watcher()
        self.owner.copy({"text/html": b"<b>bold</b>"})
        self.assertIsNone(watcher.next_change(1))

    def test_losing_the_selection_is_not_a_copy(self):
        watcher = self.watcher()
        self.owner.copy({"UTF8_STRING": b"here"})
        self.assertEqual(watcher.next_change(3).text, "here")
        self.owner.disown()
        self.assertIsNone(watcher.next_change(0.5))

    def test_the_watcher_survives_the_owner_disappearing_mid_request(self):
        watcher = self.watcher()
        self.owner.copy({"UTF8_STRING": b"first"})
        self.owner.close()
        watcher.next_change(0.5)  # Whatever it returns, it must not raise.
        replacement = XOwner()
        self.addCleanup(replacement.close)
        replacement.copy({"UTF8_STRING": b"second"})
        self.assertEqual(watcher.next_change(3).text, "second")

    def test_a_watcher_without_a_display_is_unavailable(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DISPLAY", None)
            with self.assertRaises(clipwatch.Unavailable):
                linux.X11Source()


if __name__ == "__main__":
    unittest.main()
