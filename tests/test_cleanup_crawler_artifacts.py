from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


from cleanup_crawler_artifacts import (
    SECONDS_PER_DAY,
    cleanup_amazon_artifacts,
    cleanup_amazon_log_root,
    cleanup_flipkart_artifacts,
)


NOW = 1_800_000_000.0


def write_aged_file(path: Path, age_seconds: float, text: str = "data") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    timestamp = NOW - age_seconds
    os.utime(path, (timestamp, timestamp))


def make_aged_dir(path: Path, age_seconds: float) -> None:
    path.mkdir(parents=True, exist_ok=True)
    write_aged_file(path / "output.csv", age_seconds)
    timestamp = NOW - age_seconds
    os.utime(path, (timestamp, timestamp))


class CrawlerArtifactCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = Path(tempfile.mkdtemp(prefix="siel_cleanup_test_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_flipkart_removes_only_old_allowlisted_run_directories(self) -> None:
        root = self.temp_dir / "fpkt_api" / "test_output"
        old_run = root / "ops_tv_20270101_010101"
        boundary_run = root / "ops_all_20270101_010101_123"
        recent_run = root / "ops_ref_20270101_010101"
        excluded_product = root / "ops_hhp_20270101_010101"
        unrelated = root / "listing_tv_20270101_010101"
        make_aged_dir(old_run, 4 * SECONDS_PER_DAY)
        make_aged_dir(boundary_run, 3 * SECONDS_PER_DAY)
        make_aged_dir(recent_run, 2 * SECONDS_PER_DAY)
        make_aged_dir(excluded_product, 10 * SECONDS_PER_DAY)
        make_aged_dir(unrelated, 10 * SECONDS_PER_DAY)

        stats = cleanup_flipkart_artifacts(
            root,
            products=["tv", "ref", "ldy"],
            days=3,
            apply=True,
            now=NOW,
        )

        self.assertFalse(old_run.exists())
        self.assertFalse(boundary_run.exists())
        self.assertTrue(recent_run.exists())
        self.assertTrue(excluded_product.exists())
        self.assertTrue(unrelated.exists())
        self.assertEqual(stats.removed, 2)
        self.assertEqual(stats.failures, [])

    def test_flipkart_dry_run_never_deletes(self) -> None:
        root = self.temp_dir / "fpkt_api" / "test_output"
        old_run = root / "ops_ldy_20270101_010101"
        make_aged_dir(old_run, 4 * SECONDS_PER_DAY)

        stats = cleanup_flipkart_artifacts(
            root,
            products=["ldy"],
            days=3,
            apply=False,
            now=NOW,
        )

        self.assertTrue(old_run.exists())
        self.assertEqual(stats.eligible, 1)
        self.assertEqual(stats.removed, 0)

    def test_amazon_removes_only_selected_product_html_and_jsonl(self) -> None:
        root = self.temp_dir / "amzn" / "logs"
        old_tv_html = root / "siel_amazon_tv_detail_2701010101.html"
        old_tv_jsonl = root / "siel_amazon_tv_run_20270101010101.jsonl"
        old_tv_log = root / "siel_amazon_tv_run_20270101010101.log"
        old_ref_html = root / "siel_amazon_ref_detail_2701010101.html"
        recent_tv_html = root / "siel_amazon_tv_main_2701010101.html"
        unrelated_html = root / "manual_capture.html"
        for path in (old_tv_html, old_tv_jsonl, old_tv_log, old_ref_html, unrelated_html):
            write_aged_file(path, 4 * SECONDS_PER_DAY)
        write_aged_file(recent_tv_html, 2 * SECONDS_PER_DAY)

        stats = cleanup_amazon_log_root(root, ["tv"], days=3, apply=True, now=NOW)

        self.assertFalse(old_tv_html.exists())
        self.assertFalse(old_tv_jsonl.exists())
        self.assertTrue(old_tv_log.exists())
        self.assertTrue(old_ref_html.exists())
        self.assertTrue(recent_tv_html.exists())
        self.assertTrue(unrelated_html.exists())
        self.assertEqual(stats.removed, 2)

    def test_amazon_cleans_source_and_synced_copy(self) -> None:
        repo = self.temp_dir / "repo"
        source = repo / "amzn" / "logs"
        sync = self.temp_dir / "synced_logs"
        source_file = source / "siel_amazon_ldy_detail_2701010101.html"
        sync_file = sync / "siel_amazon_ldy_detail_2701010101.html"
        write_aged_file(source_file, 4 * SECONDS_PER_DAY)
        write_aged_file(sync_file, 4 * SECONDS_PER_DAY)

        results = cleanup_amazon_artifacts(
            repo,
            ["ldy"],
            days=3,
            apply=True,
            now=NOW,
            sync_dir=sync,
        )

        self.assertFalse(source_file.exists())
        self.assertFalse(sync_file.exists())
        self.assertEqual([result.removed for result in results], [1, 1])

    def test_cleanup_failure_is_reported_without_raising(self) -> None:
        root = self.temp_dir / "amzn" / "logs"
        old_file = root / "siel_amazon_ref_detail_2701010101.html"
        write_aged_file(old_file, 4 * SECONDS_PER_DAY)

        with patch.object(Path, "unlink", side_effect=PermissionError("locked")):
            stats = cleanup_amazon_log_root(root, ["ref"], days=3, apply=True, now=NOW)

        self.assertTrue(old_file.exists())
        self.assertEqual(stats.removed, 0)
        self.assertEqual(len(stats.failures), 1)

    def test_missing_roots_are_safe(self) -> None:
        stats = cleanup_flipkart_artifacts(
            self.temp_dir / "missing",
            products=["tv"],
            days=3,
            apply=True,
            now=NOW,
        )
        self.assertTrue(stats.missing_root)
        self.assertEqual(stats.removed, 0)


if __name__ == "__main__":
    unittest.main()
