"""Offline coverage of listing preservation through the actual DB row builder."""
from __future__ import annotations

import argparse
import csv
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from test_fpkt_rating_counts import insert_module as insert
import ops_test as ops


class PartialFailureTests(unittest.TestCase):
    def test_detail_and_review_failures_keep_listing_and_original_ranks(self):
        main = [
            {"product_id": f"TEST{i}", "product_url": f"https://example.test/p/item?pid=TEST{i}",
             "product_name": f"TV {i}", "main_rank": rank, "final_price": "1000"}
            for i, rank in enumerate((1, 2, 4))
        ]
        bsr = [{**main[2], "bsr_rank": 7}]
        inserted = {}
        conn = MagicMock()

        def fake_detail(_api_dir, url, _product, _retries, _label):
            if "TEST1" in url:
                raise TimeoutError("detail unavailable")
            return {"source_url": url, "sku": "known-model", "screen_size": "108 cm (43 inch)",
                    "model_year": "2026", "count_of_star_ratings": "100",
                    "count_of_reviews": "1" if "TEST2" in url else "0",
                    "review_url": url if "TEST2" in url else None}, []

        def fake_insert(path, _max_n, _dry_run):
            batch = Mock()
            with patch.object(insert.config, "DB_CONFIG", {}, create=True), \
                    patch.object(insert.psycopg2, "connect", return_value=conn, create=True), \
                    patch.object(insert.psycopg2.extras, "execute_batch", batch, create=True), \
                    patch.object(insert.siel_item_mst, "fill_from_mst", return_value=0, create=True), \
                    patch.object(insert.siel_item_mst, "upsert_item_mst_batch", return_value=0, create=True), \
                    patch.object(sys, "argv", ["insert_test_retail_com.py", str(path), "0"]), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(insert.main(), 0)
            for call in batch.call_args_list:
                sql, rows = call.args[1:3]
                inserted[sql.split()[2]] = {row["item"]: row for row in rows}
            return 0, ["[insert] OK: tv retail=3 list=3"]

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "fpkt_api").mkdir()
            args = argparse.Namespace(insecure=False, query=None, product="tv", out_dir=root / "out",
                real_batch_id=False, db_insert=True, db_dry_run=False, api_dir=root,
                main_target=3, max_pages_main=3, bsr_target=1, max_pages_bsr=1,
                listing_retries=0, listing_retry_delay=0, email_report=False,
                max_detail=0, detail_retries=0, review_pages=1, review_retries=0,
                review_short_max_pages=1, max_reviews_per_product=20, allow_qa_insert=False, insert_max_n=0)
            listing = [([], main, 1, [], []), ([], bsr, 1, [], [])]
            with patch.object(ops, "ROOT", root), patch.object(ops, "listing_until", side_effect=listing), \
                    patch.object(ops, "detail_from_api_with_retries", side_effect=fake_detail), \
                    patch.object(ops, "review_for_product_with_retries", side_effect=TimeoutError("review unavailable")), \
                    patch.object(ops, "insert_into_db", side_effect=fake_insert), \
                    patch.object(ops, "safe_print"):
                _, lines, code = ops.run(args)

            self.assertEqual(code, 0)
            self.assertTrue(any("retaining listings" in line for line in lines))
            with (args.out_dir / "errors.csv").open(encoding="utf-8-sig", newline="") as f:
                self.assertEqual({(row["stage"], row["product_id"]) for row in csv.DictReader(f)},
                                 {("detail", "TEST1"), ("review", "TEST2")})
            for table in ("dx_siel_tv_retail_com", "dx_siel_tv_product_list"):
                rows = inserted[table]
                self.assertEqual(set(rows), {"TEST0", "TEST1", "TEST2"})
                self.assertEqual([rows[f"TEST{i}"]["main_rank"] for i in range(3)], [1, 2, 4])
                self.assertEqual(rows["TEST2"]["bsr_rank"], 7)
                self.assertEqual(rows["TEST1"]["final_sku_price"], "₹1,000")
            self.assertIsNone(inserted["dx_siel_tv_retail_com"]["TEST1"]["sku"])
            self.assertEqual(inserted["dx_siel_tv_retail_com"]["TEST2"]["sku"], "known-model")
            self.assertIsNone(inserted["dx_siel_tv_retail_com"]["TEST2"]["detailed_review_content"])
            conn.commit.assert_called_once()

    def test_legacy_and_amazon_reranking_are_unchanged(self):
        for account in ("Amazon", "Flipkart"):
            records = {str(rank): {"main": {"account_name": account, "product": "tv",
                                            "main_rank": rank}, "bsr": None}
                       for rank in (1, 4)}
            rows = insert.merge(records, {}, renumber_ranks=True)
            self.assertEqual([row["main_rank"] for row in rows], [1, 2])


if __name__ == "__main__":
    unittest.main()
