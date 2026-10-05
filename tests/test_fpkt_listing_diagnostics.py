"""Offline regression tests: synthetic API payloads, no HAR/retailer/DB/email."""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "fpkt_api"))
import listing_diagnostics as diagnostic
import ops_test as ops
import phase_probe as phase
from main_probe import extract_products


def response(count=24, offset=0, total=1155, more=True, kind="PRODUCT_SUMMARY"):
    return {"STATUS_CODE": 200, "RESPONSE": {
        "pageData": {"hasMorePages": more},
        "slots": [
            {"widget": {"type": kind, "data": {"products": [
                {"productInfo": {"value": {"id": f"TEST{i}", "titles": {"title": f"Test {i}"}},
                                 "action": {"url": f"/test/p/item?pid=TEST{i}"}}}
                for i in range(offset, offset + count)]}}},
            {"widget": {"type": "FILTER_SORT_OPTIONS", "data": {
                "productStartIndex": offset + 1, "productEndIndex": offset + count,
                "totalProducts": total, "query": "tv"}}},
            {"widget": {"type": "PAGINATION_BAR", "data": {"currentPage": 1, "totalPages": 116}}},
        ]}}


class EvidenceTests(unittest.TestCase):
    def test_server_ten_parser_loss_and_last_page_are_distinct(self):
        payload = response(10)
        evidence = diagnostic.response_snapshot(payload, extract_products(payload))
        self.assertEqual(evidence["raw_product_count"], 10)
        self.assertEqual(evidence["server_total"], 1155)
        self.assertEqual(diagnostic.classify(evidence), "server_returned_ten_products")
        payload = response(24)
        evidence = diagnostic.response_snapshot(payload, extract_products(payload)[:10])
        self.assertEqual(diagnostic.classify(evidence), "parser_count_mismatch")
        payload = response(10, total=10, more=False)
        self.assertEqual(diagnostic.classify(diagnostic.response_snapshot(payload, extract_products(payload))),
                         "last_page_ten_products")

    def test_unknown_widget_is_counted_independently_of_parser(self):
        payload = response(10, kind="NEW_PRODUCT_WIDGET")
        evidence = diagnostic.response_snapshot(payload, extract_products(payload))
        self.assertEqual((evidence["raw_product_count"], evidence["parsed_product_count"]), (10, 0))

    def test_request_projection_excludes_sensitive_and_opaque_fields(self):
        evidence = diagnostic.request_snapshot(
            "https://fake-user:fake-pass@example.test/api?token=fake-value&cacheFirst=false",
            {"Cookie": "fake-value", "Authorization": "fake-value", "User-Agent": "test-agent"},
            {"pageUri": "/search?q=tv&as-show=on&token=fake-value", "session": "fake-value",
             "pageContext": {"pageNumber": 2, "paginatedFetch": True,
                             "paginationContextMap": {"opaque": "fake-value", "offset": 24,
                                                      "session": {"size": 99}}}})
        text = json.dumps(evidence)
        for excluded in ("fake-value", "fake-user", "fake-pass", "opaque", "99"):
            self.assertNotIn(excluded, text)
        self.assertEqual(evidence["page_controls"]["pageContext.pageNumber"], 2)
        self.assertEqual(evidence["pagination_numeric_controls"], {"offset": 24})

    def test_probes_change_only_as_show_and_never_mutate_original(self):
        body = {"pageUri": "/search?q=tv&as-show=on&as=off&page=2",
                "pageContext": {"pageNumber": 2, "paginationContextMap": {"cursor": "fake"}}}
        before = copy.deepcopy(body)
        sent = []
        def fetch(url, headers, probe_body, **kwargs):
            sent.append(copy.deepcopy(probe_body))
            self.assertEqual(kwargs["timeout_seconds"], 20)
            return response(24 if "as-show=off" in probe_body["pageUri"] else 10)
        with patch.object(phase, "fetch_json", side_effect=fetch):
            result = phase.compare_listing_requests("https://example.test/api", {}, body, 2)
        self.assertEqual(body, before)
        self.assertEqual(sent[0], sent[2])
        self.assertEqual(sent[0]["pageContext"], sent[1]["pageContext"])
        self.assertEqual([r["response"]["raw_product_count"] for r in result], [10, 24, 10])

    def test_report_survives_missing_browser_and_keeps_original(self):
        payload = response(10)
        evidence = diagnostic.response_snapshot(payload, extract_products(payload))
        with tempfile.TemporaryDirectory() as temp, patch.object(diagnostic, "capture_browser", side_effect=ImportError("fake")):
            folder = Path(diagnostic.diagnose(temp, "main", 1, "tv", evidence, None))
            report = json.loads((folder / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["api"], evidence)
            self.assertEqual(report["errors"], ["browser:ImportError"])
            self.assertFalse(report["retryable"])
            self.assertEqual(report["as_show_effect"], "not_established")

    def test_http_telemetry_and_legacy_fetch_signature(self):
        wire = json.dumps(response()).encode()
        stream = Mock(status=200, url="https://example.test/api?token=fake")
        stream.read.return_value = wire
        stream.__enter__ = Mock(return_value=stream)
        stream.__exit__ = Mock(return_value=False)
        with patch.object(phase.urllib.request, "urlopen", return_value=stream):
            telemetry = {}
            payload = phase.fetch_json("https://example.test/api", {}, {}, telemetry=telemetry)
            self.assertEqual(payload["STATUS_CODE"], 200)
            self.assertEqual(telemetry["bytes"], len(wire))
            self.assertEqual(telemetry["final_url"], "https://example.test/api")
            self.assertEqual(phase.fetch_json("https://example.test/api", {}, {}), payload)

    def test_browser_uses_search_then_stage_page_and_one_screenshot(self):
        fake = MagicMock()
        browser = fake.__enter__.return_value.chromium.launch.return_value
        page = browser.new_context.return_value.new_page.return_value
        page.url = "https://www.flipkart.com/search?q=washing+machine&as-show=off&as=off"
        snapshots = [{"dom_product_count": 10, "visible_text": "<first>"},
                     {"dom_product_count": 24, "visible_text": "after scroll"}]
        with tempfile.TemporaryDirectory() as temp, \
                patch.dict(sys.modules, {"playwright.sync_api": Mock(sync_playwright=Mock(return_value=fake))}), \
                patch.object(diagnostic, "browser_snapshot", side_effect=snapshots):
            result = diagnostic.capture_browser(temp, "washing+machine", "bsr", 2)
            self.assertIn("&lt;first&gt;", (Path(temp) / "browser_initial.html").read_text())
        page.locator.return_value.first.fill.assert_called_once_with("washing machine")
        page.locator.return_value.first.press.assert_called_once_with("Enter")
        self.assertEqual(page.goto.call_count, 2)
        self.assertIn("sort=popularity", page.goto.call_args.args[0])
        self.assertIn("page=2", page.goto.call_args.args[0])
        page.screenshot.assert_called_once()
        browser.close.assert_called_once()
        self.assertEqual(result["after_scroll"]["dom_product_count"], 24)
        self.assertNotIn("visible_text", result["initial"])


class ListingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        for name, value in (("html_headers", {}), ("listing_html_metadata_for_page", {})):
            mock = patch.object(ops, name, return_value=value)
            setattr(self, name, mock.start())
            self.addCleanup(mock.stop)

    def collect(self, payloads, target=300, max_pages=30, **kwargs):
        with patch.object(ops, "listing_page", side_effect=payloads) as fetch:
            result = ops.listing_until(self.folder, "main", "tv", target, max_pages,
                                       retry_delay=0, diagnostic_dir=self.folder, **kwargs)
        return result, fetch

    def test_first_ten_stops_before_enrichment_and_next_page(self):
        with patch.object(diagnostic, "capture_browser", side_effect=ImportError):
            result, fetch = self.collect([response(10)])
        raw, final, pages, errors, records = result
        self.assertEqual((len(raw), len(final), pages, fetch.call_count), (0, 0, 1, 1))
        self.assertEqual(errors[0]["exit_code"], 86)
        self.assertEqual(records[0]["rows"], 10)
        self.listing_html_metadata_for_page.assert_not_called()
        self.assertTrue((self.folder / "diagnostics/main_page1_ten_products/report.json").exists())

    def test_baseline_24_and_prior_rows_survive_later_ten(self):
        with patch.object(diagnostic, "capture_browser", return_value={}):
            result, fetch = self.collect([response(24), response(10, 24)])
        self.assertEqual((len(result[0]), len(result[1]), fetch.call_count), (24, 24, 2))
        report = json.loads((self.folder / "diagnostics/main_page2_ten_products/report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["baseline_24"]["requested_page"], 1)
        self.assertEqual(report["api"]["requested_page"], 2)

    def test_normal_24_dedupe_and_target_stop_unchanged(self):
        result, fetch = self.collect([response(24), response(24, 14)], target=30)
        self.assertEqual((len(result[0]), len(result[1]), fetch.call_count), (40, 30, 2))
        self.assertFalse(result[3])
        observations = [json.loads(line) for line in (self.folder / "diagnostics/listing_observations.jsonl").read_text().splitlines()]
        self.assertEqual(observations[1]["cross_page_duplicate_count"], 10)
        self.assertEqual(observations[1]["parsed_product_count"], 24)
        self.assertEqual(result[4][-1]["status"], "target_reached")

    def test_page_cap_shortfall_is_failure_not_success(self):
        result, _ = self.collect([response(24)], max_pages=1)
        self.assertEqual(result[3][0]["error"], "target_shortfall_at_page_limit")
        self.assertEqual(result[3][0]["exit_code"], 86)

    def test_network_failure_retains_existing_retries(self):
        result, fetch = self.collect([TimeoutError(), response(24)], target=24, retries=1)
        self.assertEqual(fetch.call_count, 2)
        self.assertFalse(result[3])

    def test_diagnostic_write_failure_fails_closed(self):
        with patch.object(ops, "record_page", side_effect=OSError):
            result, _ = self.collect([response(10)])
        self.assertEqual(result[3][0]["exit_code"], 86)
        self.listing_html_metadata_for_page.assert_not_called()

    def test_run_skips_bsr_detail_db_after_main_diagnostic(self):
        args = argparse.Namespace(insecure=False, query=None, product="tv", out_dir=self.folder,
            real_batch_id=False, db_insert=True, db_dry_run=False, api_dir=self.folder,
            main_target=300, max_pages_main=30, bsr_target=100, max_pages_bsr=15,
            listing_retries=0, listing_retry_delay=0, email_report=False)
        stop = ([], [], 1, [{"exit_code": 86, "error": "ten_product_page"}], [])
        with patch.object(ops, "listing_until", return_value=stop) as listing, \
                patch.object(ops, "detail_from_api_with_retries") as detail, \
                patch.object(ops, "send_email_report") as email, \
                patch.object(ops.subprocess, "run") as external:
            _, lines, code = ops.run(args)
        self.assertEqual(code, 86)
        self.assertEqual(listing.call_count, 1)
        detail.assert_not_called()
        email.assert_not_called()
        external.assert_not_called()
        self.assertIn("[db_insert] skipped: listing diagnostic stop", lines)
        self.assertTrue((self.folder / "summary.txt").exists())

    def test_run_bsr_stop_remains_terminal_even_with_email_failure(self):
        args = argparse.Namespace(insecure=False, query=None, product="tv", out_dir=self.folder,
            real_batch_id=False, db_insert=True, db_dry_run=False, api_dir=self.folder,
            main_target=300, max_pages_main=30, bsr_target=100, max_pages_bsr=15,
            listing_retries=0, listing_retry_delay=0, email_report=True, email_report_success_only=True)
        stop = ([], [], 1, [{"exit_code": 86, "error": "ten_product_page"}], [])
        with patch.object(ops, "listing_until", side_effect=[([], [], 1, [], []), stop]) as listing, \
                patch.object(ops, "send_email_report", side_effect=RuntimeError) as email, \
                patch.object(ops.subprocess, "run") as external:
            _, _, code = ops.run(args)
        self.assertEqual((code, listing.call_count, email.call_count), (86, 2, 1))
        external.assert_not_called()


@unittest.skipUnless(os.name == "nt", "Windows batch control flow")
class SchedulerTests(unittest.TestCase):
    def test_mixed_failures_keep_diagnostic_stop_out_of_retry_and_final_success(self):
        # Execute only the bookkeeping fragments with synthetic exit codes.
        # Never invoke the scheduler, collection command, git, or installer.
        source = (ROOT / "schedule/fpkt_api_run_all.bat").read_text(encoding="utf-8")
        first = source.index('  if not "!PRODUCT_CODE!"=="0" (')
        last = source.index('    if /I not "%FPKT_CONTINUE_ON_PRODUCT_FAIL%"', first)
        record_failure = source[first:last] + "  )\n"
        first = source.index('  set "FIRST_PASS_FAILED_PRODUCTS=')
        last = source.index('  for %%P in (!RETRY_PRODUCTS!)', first)
        reset_retry = source[first:last]
        with tempfile.TemporaryDirectory() as temp:
            harness = Path(temp) / "retry_harness.bat"
            harness.write_text(
                '@echo off\nsetlocal EnableDelayedExpansion\nset "RUN_LOG=NUL"\n'
                'set "FAILED_PRODUCTS="\nset "FAILED_PRODUCT_NAMES="\nset "NO_RETRY_PRODUCTS="\n'
                'set "EXIT_CODE="\nset "PRODUCT_CODE=86"\nfor %%P in (tv) do (\n' + record_failure + ')\n'
                'set "PRODUCT_CODE=1"\nfor %%P in (ref) do (\n' + record_failure + ')\n' + reset_retry +
                '\necho RETRY=!RETRY_PRODUCTS!\necho FAILED=!FAILED_PRODUCTS!\necho CODE=!EXIT_CODE!\n',
                encoding="ascii", newline="\r\n")
            result = subprocess.run(["cmd.exe", "/d", "/c", str(harness)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("RETRY= ref", result.stdout)
        self.assertIn("FAILED= tv(86)", result.stdout)
        self.assertIn("CODE=86", result.stdout)


if __name__ == "__main__":
    unittest.main()
