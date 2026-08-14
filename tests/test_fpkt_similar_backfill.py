from __future__ import annotations

import json
import sys
import shutil
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "fpkt_api"))

from similar_backfill import (  # noqa: E402
    AuditWriteAfterCommitError,
    Candidate,
    CollectionResult,
    DetailCapture,
    apply_updates,
    collect_candidates,
    fetch_similar_with_retries,
    load_detail_capture,
    parse_args,
    prepare_candidates,
    run,
    select_batch_rows,
    table_for_product,
)


URL = "https://www.flipkart.com/sample/p/itm123?pid=FSN123"


class FakeCursor:
    def __init__(self, rows=None, rowcounts=None, execute_error=None):
        self.rows = list(rows or [])
        self.rowcounts = list(rowcounts or [])
        self.execute_error = execute_error
        self.executions = []
        self.rowcount = -1
        self.closed = False

    def execute(self, sql, params):
        self.executions.append((sql, params))
        if self.execute_error is not None and sql.lstrip().upper().startswith("UPDATE"):
            raise self.execute_error
        if sql.lstrip().upper().startswith("UPDATE"):
            self.rowcount = self.rowcounts.pop(0) if self.rowcounts else 1

    def fetchall(self):
        return list(self.rows)

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, cursor, commit_error=None):
        self._cursor = cursor
        self.commit_error = commit_error
        self.commits = 0
        self.rollbacks = 0
        self.closes = 0

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1
        if self.commit_error is not None:
            raise self.commit_error

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closes += 1


def candidate(row_id=1, *, item="FSN123", url=URL, current=None, product="ldy"):
    return Candidate(product, row_id, item, url, current)


def make_output_dir() -> Path:
    path = ROOT / "fpkt_api" / "test_output" / f"unit_similar_backfill_{uuid.uuid4().hex}"
    path.mkdir(parents=True)
    return path


class SimilarBackfillTests(unittest.TestCase):
    def test_table_name_comes_only_from_allowlist(self):
        self.assertEqual(table_for_product("tv"), "dx_siel_tv_retail_com")
        with self.assertRaises(ValueError):
            table_for_product("tv; DROP TABLE anything")

    def test_select_is_exact_and_parameterized(self):
        batch_id = "f_20260616_123456'; DROP TABLE anything; --"
        cursor = FakeCursor(rows=[(7, "FSN123", URL, None)])
        conn = FakeConnection(cursor)

        rows = select_batch_rows(conn, ["ldy"], batch_id)

        self.assertEqual(rows, [candidate(7)])
        sql, params = cursor.executions[0]
        self.assertNotIn(batch_id, sql)
        self.assertIn("dx_siel_ldy_retail_com", sql)
        self.assertEqual(params, (batch_id, "Flipkart", "SIEL", "LDY"))
        self.assertTrue(cursor.closed)

    def test_prepare_skips_existing_bad_urls_mismatch_and_duplicate_ids(self):
        rows = [
            candidate(1),
            candidate(1),
            candidate(2, current="already filled"),
            candidate(3, url=None),
            candidate(4, url="https://example.com/p/itm?pid=FSN123"),
            candidate(5, item="OTHER"),
        ]

        eligible, stats = prepare_candidates(rows)

        self.assertEqual([row.row_id for row in eligible], [1])
        self.assertEqual(stats.duplicate_rows, 1)
        self.assertEqual(stats.skipped_existing, 1)
        self.assertEqual(stats.skipped_no_url, 1)
        self.assertEqual(stats.skipped_invalid_url, 1)
        self.assertEqual(stats.skipped_item_mismatch, 1)

    def test_prepare_limit_is_stable(self):
        rows = [
            candidate(1),
            candidate(2),
            candidate(3, item="FSN999", url="https://www.flipkart.com/other/p/itm?pid=FSN999"),
        ]
        eligible, stats = prepare_candidates(rows, max_items=1)
        self.assertEqual([row.row_id for row in eligible], [1, 2])
        self.assertEqual(stats.eligible_rows, 3)
        self.assertEqual(stats.limited_rows, 1)

    def test_duplicate_product_fetches_once_but_preserves_each_row(self):
        calls = []

        def fetch(row):
            calls.append(row.row_id)
            return "Product A ||| Product B"

        results, unique_fetches = collect_candidates(
            [candidate(10), candidate(11)], fetch, request_delay=0
        )

        self.assertEqual(calls, [10])
        self.assertEqual(unique_fetches, 1)
        self.assertEqual([result.status for result in results], ["ready", "ready"])
        self.assertFalse(results[0].cached)
        self.assertTrue(results[1].cached)

    def test_fetch_error_is_cached_and_other_products_continue(self):
        calls = []

        def fetch(row):
            calls.append((row.product, row.row_id))
            if row.product == "ldy":
                raise TimeoutError("timed out")
            return "TV Product"

        results, _ = collect_candidates(
            [candidate(1), candidate(2), candidate(3, product="tv")],
            fetch,
            request_delay=0,
            max_consecutive_errors=5,
        )

        self.assertEqual(calls, [("ldy", 1), ("tv", 3)])
        self.assertEqual([result.status for result in results], ["error", "error", "ready"])
        self.assertTrue(results[1].cached)

    def test_circuit_breaker_marks_remaining_rows_not_attempted(self):
        def fetch(_row):
            raise TimeoutError("timed out")

        results, unique_fetches = collect_candidates(
            [
                candidate(1, item="ONE", url="https://www.flipkart.com/a/p/x?pid=ONE"),
                candidate(2, item="TWO", url="https://www.flipkart.com/b/p/x?pid=TWO"),
            ],
            fetch,
            max_consecutive_errors=1,
        )

        self.assertEqual(unique_fetches, 1)
        self.assertEqual([result.status for result in results], ["error", "not_attempted"])

    def test_whitespace_result_is_not_planned_for_update(self):
        results, _ = collect_candidates([candidate(1)], lambda _row: "   ")
        self.assertEqual(results[0].status, "no_similar")
        self.assertIsNone(results[0].value)

    def test_apply_updates_only_ready_values_and_commits(self):
        malicious_value = "O'Reilly'); DROP TABLE x; --"
        results = [
            CollectionResult(candidate(1), malicious_value, "ready"),
            CollectionResult(candidate(2), None, "no_similar"),
        ]
        cursor = FakeCursor(rowcounts=[1])
        conn = FakeConnection(cursor)

        updated, skipped = apply_updates(conn, results, "f_20260616_123456")

        self.assertEqual((updated, skipped), (1, 0))
        self.assertEqual(conn.commits, 1)
        self.assertEqual(conn.rollbacks, 0)
        self.assertEqual(len(cursor.executions), 1)
        sql, params = cursor.executions[0]
        self.assertNotIn(malicious_value, sql)
        self.assertEqual(params[0], malicious_value)
        self.assertIn("NULLIF(BTRIM(retailer_sku_name_similar), '') IS NULL", sql)
        self.assertIn("BTRIM(COALESCE(product_url, '')) = %s", sql)
        self.assertIn("UPPER(BTRIM(COALESCE(item, ''))) = %s", sql)
        self.assertEqual(params[-2:], (URL, "FSN123"))
        self.assertEqual(results[0].apply_status, "updated")

    def test_apply_preserves_value_filled_concurrently(self):
        result = CollectionResult(candidate(1), "Product A", "ready")
        cursor = FakeCursor(rowcounts=[0])
        conn = FakeConnection(cursor)

        updated, skipped = apply_updates(conn, [result], "f_20260616_123456")

        self.assertEqual((updated, skipped), (0, 1))
        self.assertEqual(result.apply_status, "skipped_not_empty_or_changed")

    def test_apply_error_rolls_back(self):
        result = CollectionResult(candidate(1), "Product A", "ready")
        cursor = FakeCursor(execute_error=RuntimeError("db failed"))
        conn = FakeConnection(cursor)

        with self.assertRaisesRegex(RuntimeError, "db failed"):
            apply_updates(conn, [result], "f_20260616_123456")

        self.assertEqual(conn.commits, 0)
        self.assertEqual(conn.rollbacks, 1)
        self.assertTrue(cursor.closed)

    def test_commit_error_rolls_back(self):
        result = CollectionResult(candidate(1), "Product A", "ready")
        conn = FakeConnection(
            FakeCursor(rowcounts=[1]), commit_error=RuntimeError("commit failed")
        )

        with self.assertRaisesRegex(RuntimeError, "commit failed"):
            apply_updates(conn, [result], "f_20260616_123456")

        self.assertEqual(conn.commits, 1)
        self.assertEqual(conn.rollbacks, 1)

    def test_fetch_adapter_uses_existing_response_parser(self):
        row = candidate(1)
        response = {
            "RESPONSE": {
                "pageData": {"pageContext": {"productId": "FSN123"}},
                "slots": [],
            }
        }
        capture = DetailCapture(
            "https://2.rome.api.flipkart.com/api/4/page/fetch",
            '{"pageContext":{}}',
            (("Accept", "application/json"),),
        )
        with patch("similar_backfill.detail_response_from_capture", return_value=response) as api:
            with patch("similar_backfill.similar_product_names", return_value="A ||| B") as parser:
                value = fetch_similar_with_retries(
                    row,
                    capture=capture,
                    retries=0,
                    retry_delay=0,
                )

        self.assertEqual(value, "A ||| B")
        api.assert_called_once_with(capture, URL)
        parser.assert_called_once_with(response)

    def test_fetch_adapter_rejects_wrong_product_response(self):
        capture = DetailCapture(
            "https://2.rome.api.flipkart.com/api/4/page/fetch",
            '{"pageContext":{}}',
            (),
        )
        response = {
            "RESPONSE": {
                "pageData": {"pageContext": {"productId": "OTHER"}},
                "slots": [],
            }
        }
        with patch("similar_backfill.detail_response_from_capture", return_value=response):
            with self.assertRaisesRegex(ValueError, "does not match"):
                fetch_similar_with_retries(
                    candidate(1),
                    capture=capture,
                    retries=0,
                    retry_delay=0,
                )

    def test_fetch_adapter_rejects_mixed_product_ids(self):
        capture = DetailCapture(
            "https://2.rome.api.flipkart.com/api/4/page/fetch",
            '{"pageContext":{}}',
            (),
        )
        response = {
            "RESPONSE": {
                "pageData": {"pageContext": {"productId": "FSN123"}},
                "slots": [
                    {
                        "widget": {
                            "data": {"parentProduct": {"value": {"id": "OTHER"}}}
                        }
                    }
                ],
            }
        }
        with patch("similar_backfill.detail_response_from_capture", return_value=response):
            with self.assertRaisesRegex(ValueError, "mixed or mismatched"):
                fetch_similar_with_retries(
                    candidate(1),
                    capture=capture,
                    retries=0,
                    retry_delay=0,
                )

    def test_capture_loader_rejects_malformed_json(self):
        capture_dir = make_output_dir()
        self.addCleanup(shutil.rmtree, capture_dir, True)
        (capture_dir / "detail_curl.txt").write_text(
            "curl 'https://2.rome.api.flipkart.com/api/4/page/fetch' "
            "-H 'Accept: application/json' --data-raw '{bad json}'",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "not valid JSON"):
            load_detail_capture(capture_dir)

    def test_capture_loader_rejects_non_api_endpoint(self):
        capture_dir = make_output_dir()
        self.addCleanup(shutil.rmtree, capture_dir, True)
        (capture_dir / "detail_curl.txt").write_text(
            "curl 'https://www.flipkart.com/api/4/page/fetch' "
            "-H 'Accept: application/json' --data-raw '{\"pageContext\":{}}'",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "api.flipkart.com"):
            load_detail_capture(capture_dir)

    def test_capture_loader_normalizes_null_page_context(self):
        capture_dir = make_output_dir()
        self.addCleanup(shutil.rmtree, capture_dir, True)
        (capture_dir / "detail_curl.txt").write_text(
            "curl 'https://2.rome.api.flipkart.com/api/4/page/fetch' "
            "-H 'Accept: application/json' --data-raw '{\"pageContext\":null}'",
            encoding="utf-8",
        )
        capture = load_detail_capture(capture_dir)
        self.assertEqual(json.loads(capture.body_json)["pageContext"], {})

    def test_run_defaults_to_dry_run_and_never_opens_write_connection(self):
        read_cursor = FakeCursor(rows=[(1, "FSN123", URL, None)])
        read_conn = FakeConnection(read_cursor)
        connect_calls = []

        def connect():
            connect_calls.append(True)
            return read_conn

        output_path = make_output_dir()
        self.addCleanup(shutil.rmtree, output_path, True)
        try:
            args = parse_args(
                [
                    "--product",
                    "ldy",
                    "--batch-id",
                    "f_20260616_123456",
                    "--out-dir",
                    str(output_path),
                    "--request-delay",
                    "0",
                ]
            )
            summary, results, output_dir, exit_code = run(
                args,
                connect_fn=connect,
                fetch_fn=lambda _row: "A ||| B",
            )

            self.assertEqual(exit_code, 0)
            self.assertEqual(summary.planned_updates, 1)
            self.assertEqual(summary.updated_rows, 0)
            self.assertEqual(results[0].apply_status, "planned_dry_run")
            self.assertTrue((output_dir / "similar_backfill.csv").exists())
            self.assertTrue((output_dir / "summary.txt").exists())
        finally:
            shutil.rmtree(output_path, ignore_errors=True)

        self.assertEqual(len(connect_calls), 1)
        self.assertEqual(read_conn.commits, 0)
        self.assertEqual(read_conn.rollbacks, 1)
        self.assertEqual(read_conn.closes, 1)

    def test_run_apply_uses_fresh_write_connection(self):
        read_conn = FakeConnection(FakeCursor(rows=[(1, "FSN123", URL, None)]))
        write_conn = FakeConnection(FakeCursor(rowcounts=[1]))
        connections = iter([read_conn, write_conn])

        output_path = make_output_dir()
        self.addCleanup(shutil.rmtree, output_path, True)
        try:
            args = parse_args(
                [
                    "--product",
                    "ldy",
                    "--batch-id",
                    "f_20260616_123456",
                    "--out-dir",
                    str(output_path),
                    "--request-delay",
                    "0",
                    "--apply",
                ]
            )
            summary, results, _output_dir, exit_code = run(
                args,
                connect_fn=lambda: next(connections),
                fetch_fn=lambda _row: "A ||| B",
            )
        finally:
            shutil.rmtree(output_path, ignore_errors=True)

        self.assertEqual(exit_code, 0)
        self.assertEqual(summary.updated_rows, 1)
        self.assertEqual(results[0].apply_status, "updated")
        self.assertEqual(read_conn.closes, 1)
        self.assertEqual(write_conn.commits, 1)
        self.assertEqual(write_conn.closes, 1)

    def test_run_returns_partial_when_batch_has_no_rows(self):
        read_conn = FakeConnection(FakeCursor(rows=[]))
        output_path = make_output_dir()
        self.addCleanup(shutil.rmtree, output_path, True)
        args = parse_args(
            [
                "--product",
                "ldy",
                "--batch-id",
                "missing_batch",
                "--out-dir",
                str(output_path),
            ]
        )

        summary, results, _output_dir, exit_code = run(
            args,
            connect_fn=lambda: read_conn,
            fetch_fn=lambda _row: self.fail("fetch must not run"),
        )

        self.assertEqual(exit_code, 2)
        self.assertEqual(summary.selection.matched_rows, 0)
        self.assertEqual(results, [])

    def test_run_apply_commits_successes_and_preserves_fetch_errors(self):
        second_url = "https://www.flipkart.com/other/p/itm?pid=FSN999"
        read_conn = FakeConnection(
            FakeCursor(rows=[(1, "FSN123", URL, None), (2, "FSN999", second_url, None)])
        )
        write_conn = FakeConnection(FakeCursor(rowcounts=[1]))
        connections = iter([read_conn, write_conn])
        output_path = make_output_dir()
        self.addCleanup(shutil.rmtree, output_path, True)
        args = parse_args(
            [
                "--product",
                "ldy",
                "--batch-id",
                "f_20260616_123456",
                "--out-dir",
                str(output_path),
                "--request-delay",
                "0",
                "--apply",
            ]
        )

        def fetch(row):
            if row.row_id == 1:
                raise TimeoutError("timed out")
            return "Product B"

        summary, results, _output_dir, exit_code = run(
            args,
            connect_fn=lambda: next(connections),
            fetch_fn=fetch,
        )

        self.assertEqual(exit_code, 2)
        self.assertEqual(summary.updated_rows, 1)
        self.assertEqual([result.status for result in results], ["error", "ready"])
        self.assertEqual(len(write_conn._cursor.executions), 1)
        self.assertEqual(write_conn._cursor.executions[0][1][1], 2)

    def test_apply_refuses_insecure_tls_without_explicit_override(self):
        output_path = make_output_dir()
        self.addCleanup(shutil.rmtree, output_path, True)
        args = parse_args(
            [
                "--product",
                "ldy",
                "--batch-id",
                "f_20260616_123456",
                "--out-dir",
                str(output_path),
                "--apply",
                "--insecure",
            ]
        )
        with self.assertRaisesRegex(ValueError, "TLS verification is disabled"):
            run(args, connect_fn=lambda: self.fail("DB must not be opened"), fetch_fn=lambda _row: None)

    def test_audit_failure_after_commit_is_explicit(self):
        read_conn = FakeConnection(FakeCursor(rows=[(1, "FSN123", URL, None)]))
        write_conn = FakeConnection(FakeCursor(rowcounts=[1]))
        connections = iter([read_conn, write_conn])
        output_path = make_output_dir()
        self.addCleanup(shutil.rmtree, output_path, True)
        args = parse_args(
            [
                "--product",
                "ldy",
                "--batch-id",
                "f_20260616_123456",
                "--out-dir",
                str(output_path),
                "--request-delay",
                "0",
                "--apply",
            ]
        )

        with patch("similar_backfill.write_audit_outputs", side_effect=[None, OSError("disk full")]):
            with self.assertRaises(AuditWriteAfterCommitError):
                run(
                    args,
                    connect_fn=lambda: next(connections),
                    fetch_fn=lambda _row: "A ||| B",
                )

        self.assertEqual(write_conn.commits, 1)
        self.assertEqual(write_conn.rollbacks, 0)


if __name__ == "__main__":
    unittest.main()
