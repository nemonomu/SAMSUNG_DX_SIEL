from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "fpkt_api"))

import siel_log  # noqa: E402
from ops_test import build_schema_outputs, detail_from_api_response  # noqa: E402


def load_insert_module():
    """DB/config 실제 모듈을 열지 않고 merge 로직만 불러온다."""
    psycopg2 = types.ModuleType("psycopg2")
    psycopg2_extras = types.ModuleType("psycopg2.extras")
    psycopg2.extras = psycopg2_extras
    stubs = {
        "psycopg2": psycopg2,
        "psycopg2.extras": psycopg2_extras,
        "config": types.ModuleType("config"),
        "siel_item_mst": types.ModuleType("siel_item_mst"),
    }
    previous = {name: sys.modules.get(name) for name in stubs}
    sys.modules.update(stubs)
    try:
        import insert_test_retail_com
        return insert_test_retail_com
    finally:
        for name, module in previous.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


insert_module = load_insert_module()


class FlipkartRatingCountTests(unittest.TestCase):
    def test_abbreviated_counts_are_rejected(self):
        for raw in ("31K Ratings", "10K", "1.1L ratings", "2 Cr ratings"):
            with self.subTest(raw=raw):
                self.assertTrue(siel_log.has_abbreviated_count(raw))
                self.assertIsNone(siel_log.parse_exact_count_int(raw))

    def test_exact_counts_are_accepted_and_westernized(self):
        self.assertEqual(siel_log.parse_exact_count_int("31,912 Ratings"), 31912)
        self.assertEqual(siel_log.parse_exact_count_int("1,10,239"), 110239)
        self.assertEqual(siel_log.best_exact_count_text("31", "31,912"), "31,912")

    def test_known_truncation_cases_choose_the_listing_count(self):
        cases = (
            ("31", "31,894", "31,894"),
            ("10", "10,941", "10,941"),
            ("1", "1,334", "1,334"),
            ("1", "110,239", "110,239"),
            ("55", "55,981", "55,981"),
        )
        for detail_count, listing_count, expected in cases:
            with self.subTest(detail=detail_count, listing=listing_count):
                self.assertEqual(
                    siel_log.best_exact_count_text(detail_count, listing_count),
                    expected,
                )

    def test_detail_parser_does_not_turn_31k_into_31(self):
        response = {"value": {"rating": 4.2, "reviewText": "31K Ratings & 3,359 Reviews"}}
        detail = detail_from_api_response(
            response,
            "https://www.flipkart.com/sample/p/itm?pid=TEST123",
            "tv",
        )

        self.assertIsNone(detail["count_of_star_ratings"])
        self.assertEqual(detail["count_of_reviews"], 3359)
        self.assertTrue(detail["_rating_count_abbreviated"])

    def test_abbreviated_target_does_not_take_similar_product_count(self):
        response = {
            "target": {"rating": 4.2, "reviewText": "31K Ratings & 3,359 Reviews"},
            "similar_product": {"value": "1,234 Ratings"},
        }
        detail = detail_from_api_response(
            response,
            "https://www.flipkart.com/sample/p/itm?pid=TEST123",
            "tv",
        )

        self.assertIsNone(detail["count_of_star_ratings"])
        self.assertEqual(detail["count_of_reviews"], 3359)
        self.assertTrue(detail["_rating_count_abbreviated"])

    def test_detail_parser_keeps_exact_count(self):
        response = {"value": {"rating": 4.2, "reviewText": "31,912 Ratings & 3,359 Reviews"}}
        detail = detail_from_api_response(
            response,
            "https://www.flipkart.com/sample/p/itm?pid=TEST123",
            "tv",
        )

        self.assertEqual(detail["count_of_star_ratings"], 31912)
        self.assertEqual(detail["count_of_reviews"], 3359)
        self.assertFalse(detail["_rating_count_abbreviated"])

    def test_output_and_db_merge_use_the_same_safe_value(self):
        url = "https://www.flipkart.com/sample/p/itm?pid=TEST123"
        main = {
            "product_url": url,
            "product_id": "TEST123",
            "product_name": "Sample",
            "star_rating": "4.2",
            "count_of_star_ratings": "31,912",
            "count_of_reviews": "3,359",
            "crawl_datetime": "2026-08-27 12:00:00",
        }
        detail = {
            "source_url": url,
            "fsn": "TEST123",
            "star_rating": "4.2",
            "count_of_star_ratings": "31",
            "crawl_datetime": "2026-08-27 12:01:00",
        }

        retail_rows, product_rows, _jsonl_rows = build_schema_outputs(
            "tv", [main], [], [detail], [], "2026-08-27 12:00:00", "f_20260827_120000"
        )
        listing = {
            "TEST123": {
                "main": {
                    **main,
                    "account_name": "flipkart",
                    "product": "tv",
                    "batch_id": "f_20260827_120000",
                },
                "bsr": None,
            }
        }
        db_rows = insert_module.merge(listing, {"TEST123": detail}, max_n=0)

        self.assertEqual(retail_rows[0]["count_of_star_ratings"], "31,912")
        self.assertEqual(product_rows[0]["count_of_star_ratings"], "31,912")
        self.assertEqual(db_rows[0]["count_of_star_ratings"], "31,912")

    def test_product_list_does_not_truncate_abbreviated_listing_count(self):
        main = {
            "product_url": "https://www.flipkart.com/sample/p/itm?pid=TEST123",
            "product_id": "TEST123",
            "product_name": "Sample",
            "count_of_star_ratings": "31K",
            "crawl_datetime": "2026-08-27 12:00:00",
        }

        _retail_rows, product_rows, _jsonl_rows = build_schema_outputs(
            "tv", [main], [], [], [], "2026-08-27 12:00:00", "f_20260827_120000"
        )

        self.assertIsNone(product_rows[0]["count_of_star_ratings"])

    def test_newer_exact_detail_count_is_preserved(self):
        self.assertEqual(siel_log.best_exact_count_text("31,920", "31,912"), "31,920")


if __name__ == "__main__":
    unittest.main()
