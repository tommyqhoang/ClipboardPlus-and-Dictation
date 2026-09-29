"""`Cloud.list_ids`: the listing reconciliation trusts only when it is provably complete."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import clipboardplus as cp
from test_clipboardplus_account import KEY, Network, failure, reply


def uuid(number: int) -> str:
    return f"00000000-0000-4000-8000-{number:012d}"


def page(numbers: range, *, more: object) -> dict[str, object]:
    body: dict[str, object] = {"items": [{"id": uuid(n), "type": "text"} for n in numbers]}
    if more is not None:
        body["hasMore"] = more
    return body


class ListingTests(Network):
    def cloud(self) -> cp.Cloud:
        return cp.Cloud(KEY)

    def test_a_single_page_ending_with_no_more_is_complete(self):
        opener = self.opener(reply(200, page(range(3), more=False)))
        listing = self.cloud().list_ids()
        self.assertEqual((listing.ids, listing.complete), (frozenset(map(uuid, range(3))), True))
        url = self.sent(opener).full_url
        self.assertEqual(url, cp.API + "/api/clipboard?limit=200&offset=0")

    def test_an_empty_account_is_a_complete_empty_listing(self):
        self.opener(reply(200, {"items": [], "hasMore": False}))
        listing = self.cloud().list_ids()
        self.assertEqual((listing.ids, listing.complete), (frozenset(), True))

    def test_pages_are_followed_by_offset(self):
        opener = self.opener(
            reply(200, page(range(0, 200), more=True)),
            reply(200, page(range(200, 230), more=False)),
        )
        listing = self.cloud().list_ids()
        self.assertEqual((len(listing.ids), listing.complete), (230, True))
        self.assertTrue(self.sent(opener, 1).full_url.endswith("offset=200"))

    def test_no_explicit_end_is_not_complete(self):
        self.opener(reply(200, page(range(3), more=None)))
        self.assertFalse(self.cloud().list_ids().complete)

    def test_a_row_without_a_valid_id_makes_it_incomplete(self):
        body = page(range(2), more=False)
        body["items"].append({"id": "nope"})  # type: ignore[union-attr]
        self.opener(reply(200, body))
        self.assertFalse(self.cloud().list_ids().complete)

    def test_a_missing_items_list_is_incomplete(self):
        self.opener(reply(200, {"hasMore": False}))
        self.assertFalse(self.cloud().list_ids().complete)

    def test_a_page_that_makes_no_progress_does_not_loop(self):
        self.opener(
            reply(200, page(range(3), more=True)),
            reply(200, page(range(3), more=True)),
        )
        self.assertFalse(self.cloud().list_ids().complete)

    def test_the_item_cap_is_not_complete(self):
        outcomes = [
            reply(200, page(range(start, start + cp.LIST_PAGE), more=True))
            for start in range(0, cp.LIST_MAX_ITEMS, cp.LIST_PAGE)
        ]
        self.opener(*outcomes)
        listing = self.cloud().list_ids()
        self.assertFalse(listing.complete)
        self.assertEqual(len(listing.ids), cp.LIST_MAX_ITEMS)

    def test_errors_are_raised_not_swallowed(self):
        self.opener(failure(500))
        with self.assertRaises(cp.SyncError):
            self.cloud().list_ids()
        self.opener(failure(401))
        with self.assertRaises(cp.AuthError):
            self.cloud().list_ids()


if __name__ == "__main__":
    unittest.main()
