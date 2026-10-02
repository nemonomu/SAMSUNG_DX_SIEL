"""Offline regressions: no browser, network, config or database is loaded.

Load function definitions only because fpkt.run performs SQL setup on import.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
import tempfile
import traceback
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]


def definitions(relative, **env):
    path = ROOT / relative
    tree = ast.parse(path.read_text(encoding='utf-8'))
    tree.body = [node for node in tree.body
                 if isinstance(node, (ast.FunctionDef, ast.ClassDef))
                 or isinstance(node, ast.ImportFrom) and node.module == '__future__']
    exec(compile(tree, str(path), 'exec'), env)
    return env


def record(pid):
    return {'fsn': pid, 'product_url': f'https://example.test/p?pid={pid}'}


class FakeDriver:
    def __init__(self, pages):
        self.pages = pages
        self.page = 0
        self.current_url = ''
        self.title = 'TV listing'
        self.page_source = '<html>saved DOM</html>'

    def get(self, url):
        self.page += 1
        self.current_url = url

    def find_elements(self, *args):
        return self.pages.get(self.page, [])

    def execute_script(self, script):
        if 'readyState' in script:
            return 'complete'
        if 'innerText' in script:
            return 'Access denied: verify you are human'
        return ['ID1']

    def save_screenshot(self, path):
        Path(path).write_bytes(b'synthetic screenshot')
        return True


def listing_env():
    output = []
    env = definitions(
        'fpkt/listing.py', os=os, json=json, sys=sys, _HERE=str(ROOT / 'fpkt'),
        _logger=Mock(), time=SimpleNamespace(sleep=Mock()),
        By=SimpleNamespace(XPATH='xpath'), WebDriverWait=Mock(),
        TimeoutException=TimeoutError, WebDriverException=RuntimeError,
        ACCOUNT_NAME='flipkart', COMPANY='sea', DIVISION='dx',
    )
    env.update(extract_card=lambda card, selectors: dict(card), emit=output.append,
               now_server_ts=lambda: '2026-10-02 00:00:00',
               scroll_to_bottom=Mock(), maybe_save_html=Mock())
    return env, output


class ListingCollectionTests(unittest.TestCase):
    def crawl(self, env, pages, target, stage='main'):
        driver = FakeDriver(pages)
        count = env['crawl_paged'](
            driver, 'tv', stage, 'https://example.test/search',
            {'base_container': {'xpath': '//card'}}, 'batch', target, 30, stage + '_rank')
        return driver, count

    def test_target_300_continues_past_30_pages_and_counts_unique_products(self):
        env, output = listing_env()
        pages = {p: [record(f'ID{i:04}') for i in range((p-1)*9, p*9)] * 2
                 for p in range(1, 35)}
        driver, count = self.crawl(env, pages, 300)
        self.assertEqual((driver.page, count), (34, 300))
        self.assertEqual(len({r['fsn'] for r in output}), 300)
        self.assertEqual([r['main_rank'] for r in output], list(range(1, 301)))

    def test_bsr_100_ignores_duplicates_and_preserves_source_rank(self):
        env, output = listing_env()
        pages = {p: [record(f'ID{i:04}') for i in range((p-1)*12, p*12)] * 2
                 for p in range(1, 10)}
        driver, count = self.crawl(env, pages, 100, 'bsr')
        self.assertEqual((driver.page, count, len(output)), (9, 100, 100))
        self.assertEqual(output[12]['bsr_rank'], 13)
        self.assertEqual(output[12]['source_rank'], 25)

    def test_all_duplicate_page_does_not_end_collection(self):
        env, output = listing_env()
        _, count = self.crawl(env, {1: [record('A')]*24, 2: [record('A')]*24,
                                    3: [record('B')]*24}, 2)
        self.assertEqual(count, 2)
        self.assertEqual([r['fsn'] for r in output], ['A', 'B'])

    def test_ten_raw_cards_abort_even_when_target_would_be_met(self):
        env, output = listing_env()
        diagnostic = Mock(return_value='saved')
        env['save_listing_diagnostic'] = diagnostic
        with self.assertRaises(env['ShortListingPage']):
            self.crawl(env, {1: [record('A')]*10}, 1)
        self.assertEqual(output, [])
        diagnostic.assert_called_once()

    def test_ten_unique_among_24_raw_cards_is_allowed(self):
        env, output = listing_env()
        _, count = self.crawl(env, {1: [record(f'ID{i%10}') for i in range(24)]}, 10)
        self.assertEqual((count, len(output)), (10, 10))

    def test_diagnostic_failure_still_aborts_product(self):
        env, output = listing_env()
        env['save_listing_diagnostic'] = Mock(side_effect=OSError('disk unavailable'))
        with self.assertRaisesRegex(env['ShortListingPage'], 'disk unavailable'):
            self.crawl(env, {1: [record('A')]*10}, 100)
        self.assertEqual(output, [])

    def test_empty_page_records_shortfall(self):
        env, output = listing_env()
        driver, count = self.crawl(env, {1: [record('A')]*24}, 300)
        self.assertEqual((driver.page, count), (2, 1))
        self.assertIn('shortfall=299', env['_logger'].info.call_args.args[0])
        self.assertIn('reason=empty_page', env['_logger'].info.call_args.args[0])

    def test_diagnostics_save_current_dom_without_another_navigation(self):
        env, _ = listing_env()
        with tempfile.TemporaryDirectory(dir=ROOT / '.codex_tmp') as folder:
            env['_HERE'] = folder
            driver = FakeDriver({})
            driver.save_screenshot = Mock(side_effect=OSError('screenshot failed'))
            prefix = env['save_listing_diagnostic'](
                driver, 'tv', 'main', 10, 'requested-url', 'batch',
                [record('A')]*10, {'base_container': {'xpath': '//card'}})
            report = json.loads(Path(prefix + '.json').read_text(encoding='utf-8'))
            self.assertEqual(Path(prefix + '.html').read_text(), driver.page_source)
            self.assertEqual(driver.page, 0)
            self.assertEqual(report['card_count'], 10)
            self.assertEqual(len(report['products']), 10)
            self.assertIn('screenshot', report['errors'])
            self.assertTrue(any('Possible access challenge' in signal for signal in report['signals']))


def run_env(abort_stage=None):
    listing, _ = listing_env()
    batches = []
    def batch_id(stage, product):
        value = f'batch_{product}_{len(batches)}'
        batches.append(value)
        return value
    l = SimpleNamespace(
        ShortListingPage=listing['ShortListingPage'], make_batch_id=Mock(side_effect=batch_id),
        emit=Mock(), init_logging=Mock(), load_selectors=Mock(return_value={'card': {}}),
        SITE_ACCOUNT='Flipkart', MAIN_URL_TEMPLATES={'tv': 'main', 'ref': 'main'},
        BSR_URL_TEMPLATES={'tv': 'bsr', 'ref': 'bsr'},
    )
    def crawl(driver, product, stage, url, selectors, batch, *rest):
        if product == 'tv' and stage == abort_stage:
            raise l.ShortListingPage('ten_product_page')
        l.emit({**record(product.upper()), 'product': product, 'stage': stage,
                stage + '_rank': 1, 'batch_id': batch})
    l.crawl_paged = Mock(side_effect=crawl)
    d = SimpleNamespace(
        emit=Mock(), init_logging=Mock(), make_batch_id=Mock(),
        load_selectors=Mock(return_value={'card': {}}), SITE_ACCOUNT='Flipkart', STAGE='detail',
        crawl_detail=Mock(side_effect=lambda driver, product, url, selectors, batch:
                          {**record(product.upper()), 'stage': 'detail',
                           'product': product, 'batch_id': batch}),
    )
    env = definitions(
        'fpkt/run.py', L=l, D=d, sys=sys, re=re, argparse=argparse, traceback=traceback,
        time=SimpleNamespace(sleep=Mock()), _FPKT_PID_RE=re.compile(r'[?&]pid=([A-Z0-9]+)'),
        _main_cache={}, _bsr_cache={}, _streaming_enabled=True, _results_path='test.jsonl',
    )
    for name in ('_setup_results', '_close_results', '_write_results', '_stream_insert',
                 '_setup_db', '_close_db', '_make_driver_tracked', '_hard_kill_driver'):
        env[name] = Mock()
    return env, l, d


class RunFlowTests(unittest.TestCase):
    def execute(self, env):
        # Even an out-of-order stages option must not start INSERT before BSR validation.
        argv = ['run.py', '--product', 'tv', 'ref', '--stages', 'main', 'detail', 'bsr']
        with patch.object(sys, 'argv', argv):
            return env['main']()

    def test_batch_is_shared_across_stages_and_changes_for_next_product(self):
        env, l, d = run_env()
        self.assertEqual(self.execute(env), 0)
        rows = [call.args[0] for call in env['_write_results'].call_args_list]
        for product in ('tv', 'ref'):
            product_rows = [r for r in rows if r.get('product') == product]
            self.assertEqual([r['stage'] for r in product_rows], ['main', 'bsr', 'detail'])
            self.assertEqual(len({r['batch_id'] for r in product_rows}), 1)
        self.assertNotEqual(rows[0]['batch_id'], rows[-1]['batch_id'])
        self.assertEqual(l.make_batch_id.call_count, 2)
        d.make_batch_id.assert_not_called()
        self.assertEqual(env['_stream_insert'].call_count, 2)

    def test_ten_cards_skip_tv_detail_and_db_but_continue_ref(self):
        for stage in ('main', 'bsr'):
            with self.subTest(stage=stage):
                env, l, d = run_env(stage)
                self.assertEqual(self.execute(env), 1)
                self.assertEqual([c.args[1] for c in d.crawl_detail.call_args_list], ['ref'])
                self.assertEqual(env['_stream_insert'].call_count, 1)
                self.assertEqual(env['_stream_insert'].call_args.args[0]['product'], 'ref')
                statuses = [c.args[0] for c in env['_write_results'].call_args_list
                            if c.args[0].get('status')]
                self.assertEqual(statuses[0]['status'], 'aborted_before_detail_and_db')
                self.assertEqual(set(env['_main_cache']), {'REF'})


if __name__ == '__main__':
    unittest.main()
