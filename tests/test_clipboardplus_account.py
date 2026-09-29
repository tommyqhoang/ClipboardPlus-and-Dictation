"""Clipboard+ account flow and cloud client: request contract and failure handling."""

from __future__ import annotations

import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import clipboardplus as cp
import clipstore

KEY = "cp_live_" + "a1b2c3d4" * 6
PASSWORD = "correct horse battery staple"
TOKEN = "eyJ.session.token"
ITEM_ID = "3f2b8c1e-6a4d-4c7e-9b1a-0d5e7f8a9b2c"
OTHER_ID = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "sync_keys.json").read_text("utf-8"))


def reply(status: int, body: Any = None) -> Mock:
    result = Mock(status=status)
    result.__enter__ = lambda self: self
    result.__exit__ = lambda *args: None
    payload = body if isinstance(body, bytes) else json.dumps({} if body is None else body).encode()
    result.read.return_value = payload
    return result


def failure(status: int, body: Any = None) -> urllib.error.HTTPError:
    payload = body if isinstance(body, bytes) else json.dumps({} if body is None else body).encode()
    return urllib.error.HTTPError(cp.API, status, "err", {}, io.BytesIO(payload))  # type: ignore[arg-type]


def stored(**changes: Any) -> clipstore.Item:
    fields: dict[str, Any] = {
        "id": 1,
        "kind": "text",
        "text": "hello",
        "image_file": "",
        "thumb_file": "",
        "width": 0,
        "height": 0,
        "bytes": 5,
        "created_at": 1790424000.123,
        "updated_at": 1790424000.123,
        "favorite": False,
        "label": "",
        "source": "desktop",
        "cloud_id": "",
        "cloud_key": "",
        "cloud_favorite": False,
        "dirty": True,
        "sync_skip": False,
    }
    fields.update(changes)
    return clipstore.Item(**fields)


class Network(unittest.TestCase):
    def opener(self, *outcomes: Any) -> Mock:
        opener = Mock()
        opener.open.side_effect = list(outcomes)
        patcher = patch.object(cp.urllib.request, "build_opener", return_value=opener)
        self.build = patcher.start()
        self.addCleanup(patcher.stop)
        return opener

    @staticmethod
    def sent(opener: Mock, index: int = -1) -> Any:
        return opener.open.call_args_list[index].args[0]

    @staticmethod
    def body(request: Any) -> Any:
        return json.loads(request.data)


class SyncKeyTests(unittest.TestCase):
    def test_matches_the_servers_own_keys(self):
        for case in FIXTURES:
            with self.subTest(case=case["key"][:40]):
                self.assertEqual(
                    cp.sync_key(case["kind"], case["created_ms"], case["primary"]), case["key"]
                )

    def test_a_key_never_ends_inside_a_surrogate_pair(self):
        key = cp.sync_key("text", 1790424000000, "x" * 199 + "😀")
        key.encode("utf-8")  # Would raise on a lone surrogate.
        self.assertTrue(key.endswith("x" * 199))


class AccountTests(Network):
    def test_register_and_login_return_the_session_token(self):
        for function, route in ((cp.register, "/api/auth/register"), (cp.login, "/api/auth/login")):
            with self.subTest(route=route):
                opener = self.opener(reply(201, {"token": TOKEN, "user": {"email": "a@b.co"}}))
                self.assertEqual(function("a@b.co", PASSWORD), TOKEN)
                request = self.sent(opener)
                self.assertEqual(request.full_url, cp.API + route)
                self.assertEqual(request.get_method(), "POST")
                self.assertEqual(self.body(request), {"email": "a@b.co", "password": PASSWORD})
                self.assertIsNone(request.get_header("Authorization"))

    def test_account_errors_have_plain_messages(self):
        cases = (
            (cp.register, failure(409, {"code": "ACCOUNT_EXISTS"}), "already has an account"),
            (cp.register, failure(409, {"code": "GOOGLE_ACCOUNT_EXISTS"}), "Continue in browser"),
            (
                cp.register,
                failure(400, {"error": "Password must be at least 8 characters"}),
                "at least 8",
            ),
            (
                cp.login,
                failure(401, {"error": "Invalid email or password"}),
                "Wrong email or password",
            ),
            (cp.login, failure(401, {"code": "GOOGLE_ACCOUNT_ONLY"}), "Continue in browser"),
            (cp.login, failure(429), "Too many attempts"),
            (cp.login, failure(500), "trouble"),
            (cp.login, urllib.error.URLError("offline"), "Couldn’t reach"),
            (cp.login, reply(200, b"<html>"), "trouble"),
            (cp.login, reply(200, {"nope": 1}), "trouble"),
        )
        for function, outcome, message in cases:
            with self.subTest(message=message, outcome=repr(outcome)[:40]):
                self.opener(outcome)
                with self.assertRaises(cp.AuthError) as caught:
                    function("a@b.co", PASSWORD)
                self.assertIn(message, str(caught.exception))

    def test_the_password_and_token_never_appear_in_errors(self):
        secret_bodies = (
            {"error": f"bad {PASSWORD}"},
            {"error": TOKEN},
        )
        for body in secret_bodies:
            for status in (400, 401, 500):
                with self.subTest(status=status, body=body):
                    self.opener(failure(status, body))
                    with self.assertRaises(cp.AuthError) as caught:
                        cp.login("a@b.co", PASSWORD)
                    text = repr(caught.exception) + str(caught.exception)
                    self.assertNotIn(PASSWORD, text)
                    self.assertNotIn(TOKEN, text)
                    self.assertIsNone(caught.exception.__cause__)
                    self.assertIsNone(caught.exception.__context__)

    def test_create_key_asks_for_read_and_write_and_returns_the_key(self):
        opener = self.opener(reply(201, {"apiKey": {"scopes": []}, "token": KEY}))
        self.assertEqual(cp.create_key(TOKEN, "Whisper on this computer"), KEY)
        request = self.sent(opener)
        self.assertEqual(request.full_url, cp.API + "/api/keys")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + TOKEN)
        self.assertEqual(
            self.body(request),
            {"name": "Whisper on this computer", "scopes": ["clipboard:read", "clipboard:write"]},
        )

    def test_create_key_rejects_a_malformed_key_and_explains_the_key_limit(self):
        self.opener(reply(201, {"token": "not-a-key"}))
        with self.assertRaises(cp.AuthError):
            cp.create_key(TOKEN, "x")
        self.opener(failure(409, {"error": "Maximum of 10 active API keys allowed"}))
        with self.assertRaises(cp.AuthError) as caught:
            cp.create_key(TOKEN, "x")
        self.assertIn("10 keys", str(caught.exception))

    def test_only_https_endpoints_receive_credentials(self):
        opener = self.opener(reply(201, {"token": TOKEN}))
        with self.assertRaises(cp.AuthError):
            cp.login("a@b.co", PASSWORD, api="http://evil.example")
        with self.assertRaises(cp.AuthError):
            cp.create_key(TOKEN, "x", api="http://evil.example")
        opener.open.assert_not_called()

    def test_redirects_are_never_followed(self):
        self.opener(reply(200, {"token": TOKEN}))
        cp.login("a@b.co", PASSWORD)
        handlers = self.build.call_args.args
        self.assertTrue(any(isinstance(handler, cp.NoRedirect) for handler in handlers))


class VerifyTests(Network):
    def test_a_key_must_be_able_to_read_and_write(self):
        cases = (
            # (write probe, read probe, expected)
            (failure(400), reply(200, {"items": []}), "ok"),
            (failure(403), reply(200, {"items": []}), "read-only"),
            (failure(400), failure(403), "write-only"),
            (failure(403), failure(403), "no-access"),
            (failure(401), reply(200), "invalid"),
            (failure(400), failure(401), "invalid"),
            (failure(500), reply(200), "error"),
            (failure(400), failure(500), "error"),
            (reply(201), reply(200), "error"),
            (urllib.error.URLError("offline"), reply(200), "offline"),
            (failure(400), urllib.error.URLError("offline"), "offline"),
        )
        for write, read, expected in cases:
            with self.subTest(expected=expected, write=repr(write)[:30], read=repr(read)[:30]):
                self.opener(write, read)
                self.assertEqual(cp.verify(KEY), expected)

    def test_the_probes_save_nothing(self):
        opener = self.opener(failure(400), reply(200, {"items": []}))
        cp.verify(KEY)
        write, read = self.sent(opener, 0), self.sent(opener, 1)
        self.assertEqual((write.get_method(), self.body(write)), ("POST", {"type": "verify"}))
        self.assertEqual(
            (read.get_method(), read.full_url), ("GET", cp.API + "/api/clipboard?limit=1")
        )


class CloudTests(Network):
    def cloud(self) -> cp.Cloud:
        return cp.Cloud(KEY)

    def test_requires_a_key_and_https(self):
        with self.assertRaises(ValueError):
            cp.Cloud("nope")
        opener = self.opener(reply(200, {}))
        with self.assertRaises(cp.SyncError):
            cp.Cloud(KEY, api="http://evil.example").pull(None)
        opener.open.assert_not_called()

    def test_pull_reads_items_and_deletions(self):
        page = {
            "items": [
                {
                    "id": ITEM_ID,
                    "type": "text",
                    "content": "hello",
                    "url": None,
                    "label": "greeting",
                    "source": "Chrome",
                    "isFavorite": True,
                    "large": False,
                    "ts": 1790424000123,
                    "updatedAt": "2026-09-26T12:00:05.000Z",
                },
                {
                    "id": OTHER_ID,
                    "type": "url",
                    "content": None,
                    "url": "https://example.com/",
                    "label": None,
                    "source": None,
                    "isFavorite": False,
                    "ts": 1790424000999,
                    "updatedAt": "2026-09-26T12:00:06.000Z",
                },
                {"id": "not-a-uuid", "type": "text", "content": "x", "ts": 1},
                {"id": OTHER_ID, "type": "image", "ts": 1},
                "junk",
            ],
            "deletedItems": [
                {"type": "text", "ts": 1790424001000, "contentPrefix": "gone"},
                {"type": "video", "ts": 5, "contentPrefix": "?"},
            ],
        }
        opener = self.opener(reply(200, page))
        result = self.cloud().pull(1790424000.0)
        request = self.sent(opener)
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(
            request.full_url, cp.API + "/api/clipboard/pull?since=2026-09-26T12%3A00%3A00.000Z"
        )
        self.assertEqual(request.get_header("Authorization"), "Bearer " + KEY)
        self.assertEqual(
            result.items,
            [
                cp.CloudItem(
                    ITEM_ID,
                    "text",
                    "hello",
                    "greeting",
                    True,
                    "Chrome",
                    1790424000123,
                    1790424005.0,
                ),
                cp.CloudItem(
                    OTHER_ID,
                    "url",
                    "https://example.com/",
                    "",
                    False,
                    "",
                    1790424000999,
                    1790424006.0,
                ),
            ],
        )
        self.assertEqual(result.deleted, [cp.Removed("text", 1790424001000, "gone")])

    def test_a_full_pull_sends_no_cursor(self):
        opener = self.opener(reply(200, {"items": []}))
        self.assertEqual(self.cloud().pull(None), cp.Pull([], []))
        self.assertEqual(self.sent(opener).full_url, cp.API + "/api/clipboard/pull")

    def test_push_sends_text_and_links_in_batches_of_100(self):
        items = [stored(id=n, text=f"item {n}", created_at=1790424000.0 + n) for n in range(1, 251)]
        items.append(
            stored(id=999, kind="url", text="https://example.com/", favorite=True, label="Ex")
        )
        items.append(stored(id=1000, kind="image", text="", image_file="a.png"))
        opener = self.opener(reply(200, {"synced": 100}), reply(200), reply(200))
        self.cloud().push(items)
        self.assertEqual(opener.open.call_count, 3)
        requests = [self.sent(opener, n) for n in range(3)]
        self.assertEqual([len(self.body(r)["items"]) for r in requests], [100, 100, 51])
        for request in requests:
            self.assertEqual(request.full_url, cp.API + "/api/clipboard/sync")
            self.assertEqual(request.get_method(), "POST")
        first = self.body(requests[0])["items"][0]
        self.assertEqual(
            first,
            {
                "type": "text",
                "content": "item 1",
                "ts": 1790424001000,
                "isFavorite": False,
                "source": cp.SOURCE,
            },
        )
        last = self.body(requests[2])["items"][-1]
        self.assertEqual(last["type"], "url")
        self.assertEqual(last["url"], "https://example.com/")
        self.assertEqual((last["isFavorite"], last["label"]), (True, "Ex"))
        sent_types = {i["type"] for r in requests for i in self.body(r)["items"]}
        self.assertEqual(sent_types, {"text", "url"})

    def test_push_with_nothing_to_send_makes_no_request(self):
        opener = self.opener()
        self.cloud().push([stored(kind="image")])
        self.cloud().push([])
        opener.open.assert_not_called()

    def test_toggle_favorite_and_delete_use_the_item_routes(self):
        opener = self.opener(
            reply(200, {"item": {"isFavorite": True}}),
            reply(200, {"deleted": True}),
            failure(404),
            failure(404),
        )
        cloud = self.cloud()
        self.assertTrue(cloud.toggle_favorite(ITEM_ID))
        cloud.delete(ITEM_ID)
        cloud.delete(ITEM_ID)  # 404 is success: it is already gone.
        self.assertIsNone(cloud.toggle_favorite(ITEM_ID))
        toggle, delete = self.sent(opener, 0), self.sent(opener, 1)
        self.assertEqual(
            (toggle.get_method(), toggle.full_url),
            ("PATCH", f"{cp.API}/api/clipboard/{ITEM_ID}/favorite"),
        )
        self.assertEqual(
            (delete.get_method(), delete.full_url), ("DELETE", f"{cp.API}/api/clipboard/{ITEM_ID}")
        )

    def test_set_label_patches_the_item_and_lets_go_of_refusals(self):
        opener = self.opener(reply(200, {"item": {}}), failure(400), failure(404))
        cloud = self.cloud()
        cloud.set_label(ITEM_ID, "Wifi")
        cloud.set_label(ITEM_ID, "")  # No longer a favorite there: nothing to retry.
        cloud.set_label(ITEM_ID, "")  # Gone there.
        sent = self.sent(opener, 0)
        self.assertEqual(
            (sent.get_method(), sent.full_url), ("PATCH", f"{cp.API}/api/clipboard/{ITEM_ID}")
        )
        self.assertEqual(json.loads(sent.data), {"label": "Wifi"})

    def test_an_id_is_validated_before_it_reaches_a_url(self):
        opener = self.opener()
        for bad in ("../account", "", "a b", ITEM_ID + "/../x", "http://x"):
            with self.subTest(bad=bad):
                with self.assertRaises(cp.SyncError):
                    self.cloud().delete(bad)
        opener.open.assert_not_called()

    def test_clear_keeps_favorites_unless_asked(self):
        opener = self.opener(reply(200, {"deleted": 3}))
        self.cloud().clear(favorites=False)
        request = self.sent(opener)
        self.assertEqual(
            (request.get_method(), request.full_url), ("DELETE", cp.API + "/api/clipboard")
        )
        self.assertEqual(opener.open.call_count, 1)

    def test_clear_with_favorites_also_deletes_the_starred_items(self):
        listing = {
            "items": [
                {"id": ITEM_ID, "type": "text", "isFavorite": True},
                {"id": OTHER_ID, "type": "text", "isFavorite": False},
            ],
            "hasMore": False,
        }
        opener = self.opener(reply(200), reply(200, listing), reply(200, {"deleted": 1}))
        self.cloud().clear(favorites=True)
        self.assertEqual(opener.open.call_count, 3)
        bulk = self.sent(opener, 2)
        self.assertEqual(
            (bulk.get_method(), bulk.full_url), ("DELETE", cp.API + "/api/clipboard/bulk")
        )
        self.assertEqual(self.body(bulk), {"ids": [ITEM_ID]})

    def test_authentication_failures_are_distinct_from_other_failures(self):
        cloud = self.cloud()
        for status in (401, 403):
            with self.subTest(status=status):
                self.opener(failure(status))
                with self.assertRaises(cp.AuthError):
                    cloud.pull(None)
        for outcome in (
            failure(500),
            failure(400),
            failure(413),
            urllib.error.URLError("offline"),
            TimeoutError(),
            OSError("reset"),
            cp.http.client.IncompleteRead(b""),
        ):
            with self.subTest(outcome=repr(outcome)[:40]):
                self.opener(outcome)
                with self.assertRaises(cp.SyncError) as caught:
                    cloud.pull(None)
                self.assertNotIsInstance(caught.exception, cp.AuthError)

    def test_the_status_is_available_to_the_caller(self):
        self.opener(failure(413), urllib.error.URLError("offline"))
        with self.assertRaises(cp.SyncError) as caught:
            self.cloud().push([stored()])
        self.assertEqual(caught.exception.status, 413)
        with self.assertRaises(cp.SyncError) as caught:
            self.cloud().push([stored()])
        self.assertIsNone(caught.exception.status)

    def test_malformed_replies_are_a_sync_error(self):
        for body in (b"<html>", b"[]", b'{"items": "no"}', b"\xff\xfe", b""):
            with self.subTest(body=body):
                self.opener(reply(200, body))
                with self.assertRaises(cp.SyncError):
                    self.cloud().pull(None)

    def test_the_key_never_appears_in_errors(self):
        self.opener(failure(500, {"error": KEY}), failure(401, {"error": KEY}))
        for _ in range(2):
            with self.assertRaises((cp.SyncError, cp.AuthError)) as caught:
                self.cloud().pull(None)
            self.assertNotIn(KEY, repr(caught.exception) + str(caught.exception))


if __name__ == "__main__":
    unittest.main()
