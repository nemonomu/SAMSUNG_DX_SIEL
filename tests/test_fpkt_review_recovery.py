"""Offline regressions for panel mounting, PID isolation and null-only DB recovery."""
from __future__ import annotations

import ast
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import re
import sys
import tempfile
from types import SimpleNamespace, ModuleType
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import siel_log
import fpkt
from fpkt import recover_reviews
from fpkt.review_fields import FIELDS, aggregate_fields, direct_review_url, fill_missing, product_pid, same_review_url
from fpkt.recover_reviews import apply_missing, load_candidates, required_fields, read_resume

URL = 'https://www.flipkart.com/test/p/itm123?pid=TVS123'
REVIEW = 'https://www.flipkart.com/test/product-reviews/itm123?pid=TVS123'
SELECTORS = {'open_reviews_panel': {'xpath': 'panel'},
             'click_show_all_reviews': {'xpath': 'links', 'fallback': 'legacy'},
             'detailed_review_content': {'xpath': 'body'}}


class Element:
    def __init__(self, href='', text=''):
        self.href, self.text = href, text

    def get_attribute(self, name):
        return self.href if name == 'href' else self.text

    def is_displayed(self):
        return True


class Wait:
    def __init__(self, driver, *args, **kwargs):
        self.driver = driver

    def until(self, predicate):
        value = predicate(self.driver)
        if not value:
            raise TimeoutError('synthetic timeout')
        return value


class Driver:
    def __init__(self, panel=True, legacy=False):
        self.current_url = URL
        self.panel = panel
        self.opened = False
        self.legacy = legacy
        self.visited = []

    def find_elements(self, by, xpath):
        if xpath == 'panel' and self.panel:
            return [Element('https://www.flipkart.com/ratings-reviews-details-page?pid=TVS123')]
        if xpath == 'links' and self.opened or xpath == 'legacy' and self.legacy:
            return [Element(REVIEW.replace('TVS123', 'TVS999')),
                    Element(REVIEW + '&an=Picture'), Element(REVIEW)]
        if xpath == 'body' and '/product-reviews/' in self.current_url:
            return [Element(text='First body'), Element(text='Second body')]
        return []

    def find_element(self, by, xpath):
        if xpath == 'summary' and '/product-reviews/' in self.current_url:
            return Element(text='5 ratings and 2 reviews')
        raise LookupError(xpath)

    def execute_script(self, script, *args):
        if 'click()' in script:
            self.opened = True
        return 'complete'

    def get(self, url):
        self.current_url = url
        self.visited.append(url)


def detail_env():
    # No config, driver, database or network imports on this development machine.
    source = ast.parse((ROOT / 'fpkt/detail.py').read_text(encoding='utf-8'))
    source.body = [n for n in source.body if isinstance(n, ast.FunctionDef)]
    env = dict(json=json, re=re, siel_log=siel_log, product_pid=product_pid,
               same_review_url=same_review_url, direct_review_url=direct_review_url, aggregate_fields=aggregate_fields,
               fill_missing=fill_missing, REVIEW_FIELDS=FIELDS, REVIEW_MAX=20,
               REVIEW_SUMMARY_XPATH='summary', _logger=Mock(), _html_path=None,
               _review_violation_saved=False, By=SimpleNamespace(XPATH='xpath', CSS_SELECTOR='css'),
               WebDriverWait=Wait, TimeoutException=TimeoutError,
               WebDriverException=RuntimeError, NoSuchElementException=LookupError,
               _Urllib3RT=ConnectionError, time=SimpleNamespace(sleep=Mock()))
    env.update(parse_qs=parse_qs, parse_qsl=parse_qsl, urlencode=urlencode,
               urlsplit=urlsplit, urlunsplit=urlunsplit)
    exec(compile(source, 'detail.py', 'exec'), env)
    return env


class PanelTests(unittest.TestCase):
    def test_blank_placeholders_are_not_treated_as_loaded_reviews(self):
        env, driver = detail_env(), Driver()
        driver.current_url = REVIEW
        driver.find_elements = lambda by, xp: [Element(text='') for _ in range(20)] if xp == 'body' else []
        self.assertFalse(env['_wait_review_ready'](driver, URL,
            dict(count_of_reviews='100', count_of_star_ratings='200'), 'body'))
        self.assertEqual(env['_scroll_review_bodies'](driver, 'body', 20), 0)

    def test_ready_reviews_skip_product_refresh_and_fixed_sleep(self):
        env, driver = detail_env(), Driver()
        result = env['_collect_reviews'](driver, URL, SELECTORS, {'source_url': URL})
        self.assertIn('Second body', result['detailed_review_content'])
        self.assertEqual(driver.visited, [REVIEW])
        self.assertFalse(driver.opened)
        env['time'].sleep.assert_not_called()

    def test_empty_direct_load_refreshes_product_and_opens_panel_once(self):
        class NeedsRefresh(Driver):
            def find_elements(self, by, xpath):
                if xpath == 'body' and URL not in self.visited:
                    return []
                return super().find_elements(by, xpath)
        env, driver = detail_env(), NeedsRefresh()
        result = env['_collect_reviews'](driver, URL, SELECTORS, {'source_url': URL})
        self.assertEqual(driver.visited, [REVIEW, URL, REVIEW])
        self.assertIn('First body', result['detailed_review_content'])
        self.assertTrue(driver.opened)

    def test_twenty_unique_bodies_and_third_page_for_duplicates(self):
        class Paged(Driver):
            def find_elements(self, by, xpath):
                page = int(parse_qs(urlsplit(self.current_url).query).get('page', ['1'])[0])
                if '/product-reviews/' in self.current_url:
                    if xpath == 'body':
                        start = {1: 0, 2: 9, 3: 19}[page]
                        return [Element(text=f'Body {i}') for i in range(start, start + 10)]
                    if xpath.startswith('//a[contains'):
                        return [Element(REVIEW + f'&page={page + 1}')]
                return super().find_elements(by, xpath)
        env, driver = detail_env(), Paged()
        result = env['_collect_reviews'](driver, URL, SELECTORS,
            dict(source_url=URL, count_of_reviews='100', count_of_star_ratings='200'))
        self.assertEqual(driver.visited, [REVIEW, REVIEW + '&page=2', REVIEW + '&page=3'])
        self.assertEqual(len(result['detailed_review_content'].split(' ||| ')), 20)
        self.assertIn('Body 19', result['detailed_review_content'])
        self.assertNotIn('Body 20', result['detailed_review_content'])
        env['time'].sleep.assert_not_called()

    def test_ten_nonempty_bodies_do_not_need_pagination_link_or_extra_scroll(self):
        env, driver = detail_env(), Driver()
        driver.current_url = REVIEW
        driver.find_elements = lambda by, xp: (
            [Element(text=f'Body {i}') for i in range(10)] if xp == 'body' else [])
        driver.execute_script = Mock()
        self.assertEqual(env['_scroll_review_bodies'](driver, 'body', 10), 10)
        driver.execute_script.assert_not_called()

    def test_lazy_bodies_wait_past_two_and_three_until_ten(self):
        class PollWait(Wait):
            def until(self, predicate):
                for _ in range(3):
                    value = predicate(self.driver)
                    if value:
                        return value
                raise TimeoutError('synthetic timeout')
        env, driver = detail_env(), Driver()
        env['WebDriverWait'] = PollWait
        driver.current_url = REVIEW
        counts = iter([2, 3, 10])
        driver.count = next(counts)
        driver.find_elements = lambda by, xp: [Element(text=f'Body {i}') for i in range(driver.count)]
        driver.execute_script = Mock(side_effect=lambda *args: setattr(driver, 'count', next(counts)))
        self.assertEqual(env['_scroll_review_bodies'](driver, 'body', 10), 10)
        self.assertEqual(driver.execute_script.call_count, 2)

    def test_null_count_uses_direct_url_and_recovers_count_and_body(self):
        env, driver = detail_env(), Driver()
        rec = {'source_url': URL, 'count_of_reviews': None}
        result = env['_collect_reviews'](driver, URL, SELECTORS, rec)
        self.assertFalse(driver.opened)
        self.assertEqual(result['count_of_reviews'], '2')
        self.assertEqual(result['count_of_star_ratings'], '5')
        self.assertIn('First body', result['detailed_review_content'])
        self.assertEqual(result['_review_status'], 'collected')
        self.assertIn(REVIEW, driver.visited)
        self.assertFalse(any('/ratings-reviews-details-page' in u for u in driver.visited))

    def test_legacy_inline_reviews_work_without_panel(self):
        env, driver = detail_env(), Driver(panel=False, legacy=True)
        self.assertEqual(env['_prepare_review_href'](driver, URL, SELECTORS), REVIEW)
        self.assertFalse(driver.opened)
        result = env['_collect_reviews'](driver, URL, SELECTORS, {'source_url': URL})
        self.assertEqual(result['_review_status'], 'collected')
        self.assertEqual(driver.visited, [REVIEW])

    def test_missing_panel_does_not_fabricate_zero_or_take_unrelated_body(self):
        env, driver = detail_env(), Driver(panel=False)
        driver.find_elements = lambda *args: []
        driver.find_element = Mock(side_effect=LookupError('no summary'))
        result = env['_collect_reviews'](driver, URL, SELECTORS, {'source_url': URL})
        self.assertIsNone(result['count_of_reviews'])
        self.assertIsNone(result['detailed_review_content'])
        self.assertEqual(result['_review_status'], 'review_body_missing')
        self.assertEqual(driver.visited, [REVIEW, REVIEW + '&page=2', REVIEW + '&page=3', URL])

    def test_explicit_zero_skips_body(self):
        env, driver = detail_env(), Driver()
        result = env['_collect_reviews'](driver, URL, SELECTORS,
                                        {'source_url': URL, 'count_of_reviews': '0'})
        self.assertIsNone(result['detailed_review_content'])
        self.assertFalse(driver.opened)

    def test_numeric_only_recovery_does_not_collect_body(self):
        env, driver = detail_env(), Driver()
        env['_extract_multi_raw'] = Mock(side_effect=AssertionError('body must not be read'))
        result = env['_collect_reviews'](driver, URL, SELECTORS,
                                        {'source_url': URL}, need_body=False)
        self.assertEqual(result['count_of_reviews'], '2')

    def test_direct_urls_cover_tv_ref_ldy_and_drop_tracking_filters_and_page(self):
        for pid in ('TVS123', 'RFR123', 'WMN123'):
            with self.subTest(pid=pid):
                source = URL.replace('TVS123', pid) + '&lid=listing&an=Quality&page=9#reviews'
                expected = REVIEW.replace('TVS123', pid)
                self.assertEqual(direct_review_url(source), expected)
                self.assertTrue(same_review_url(source, expected))
        for bad in (URL.replace('www.flipkart.com', 'example.com'),
                    URL.replace('https:', 'http:'), URL + '&pid=TVS999',
                    URL.replace('/p/itm123', '/search'), REVIEW, ''):
            with self.subTest(url=bad):
                self.assertIsNone(direct_review_url(bad))

    def test_inline_partial_is_supplemented_by_direct_before_panel(self):
        class InlinePartial(Driver):
            def find_elements(self, by, xpath):
                if xpath == 'links' and self.current_url == URL:
                    return [Element(REVIEW + '&lid=legacy')]
                if xpath == 'body' and '/product-reviews/' in self.current_url:
                    start = 0 if 'lid=legacy' in self.current_url else 10
                    return [Element(text=f'Body {i}') for i in range(start, start + 10)]
                return super().find_elements(by, xpath)
        env, driver = detail_env(), InlinePartial()
        rec = dict(source_url=URL, count_of_reviews='100', count_of_star_ratings='200')
        result = env['_collect_reviews'](driver, URL, SELECTORS, rec)
        self.assertEqual(driver.visited,
                         [REVIEW + '&lid=legacy', REVIEW + '&lid=legacy&page=2',
                          REVIEW + '&lid=legacy&page=3', REVIEW])
        self.assertFalse(driver.opened)
        self.assertEqual(result['_review_status'], 'collected')
        self.assertEqual(len(result['detailed_review_content'].split(' ||| ')), 20)
        self.assertIn('Body 0', result['detailed_review_content'])
        self.assertIn('Body 19', result['detailed_review_content'])

    def test_direct_partial_is_supplemented_by_panel_and_preserves_numbers(self):
        class PanelOnly(Driver):
            def find_elements(self, by, xpath):
                if xpath == 'body' and '/product-reviews/' in self.current_url:
                    indices = range(5, 20) if self.opened else range(5)
                    return [Element(text=f'Body {i}') for i in indices]
                return super().find_elements(by, xpath)
        env, driver = detail_env(), PanelOnly()
        rec = dict(source_url=URL, star_rating='4.2', count_of_reviews='20', count_of_star_ratings='30')
        result = env['_collect_reviews'](driver, URL, SELECTORS, rec)
        self.assertEqual(driver.visited, [REVIEW, REVIEW + '&page=2', URL, REVIEW])
        self.assertTrue(driver.opened)
        self.assertEqual(result['_review_status'], 'collected')
        self.assertEqual(len(result['detailed_review_content'].split(' ||| ')), 20)
        self.assertEqual([result[field] for field in FIELDS[:3]], ['4.2', '30', '20'])

    def test_failed_fallback_preserves_partial_body_and_marks_short(self):
        class Partial(Driver):
            def find_elements(self, by, xpath):
                if xpath == 'body' and '/product-reviews/' in self.current_url:
                    return [Element(text='Only body')]
                return []
        env, driver = detail_env(), Partial(panel=False)
        result = env['_collect_reviews'](driver, URL, SELECTORS,
            dict(source_url=URL, count_of_reviews='20', count_of_star_ratings='30'))
        self.assertEqual(result['_review_status'], 'review_body_partial')
        self.assertEqual(result['detailed_review_content'], 'review1 - Only body')
        self.assertEqual(driver.visited, [REVIEW, REVIEW + '&page=2', URL])

    def test_complete_existing_body_does_not_navigate(self):
        env, driver = detail_env(), Driver()
        body = 'review1 - Existing one ||| review2 - Existing two'
        result = env['_collect_reviews'](driver, URL, SELECTORS,
            dict(source_url=URL, count_of_reviews='2', detailed_review_content=body))
        self.assertEqual(result['detailed_review_content'], body)
        self.assertEqual(driver.visited, [])
        self.assertFalse(driver.opened)

    def test_main_embedded_bodies_are_used_before_direct_url(self):
        for count, expected_visits in [('2', []), ('3', [REVIEW])]:
            with self.subTest(count=count):
                env, driver = detail_env(), Driver()
                driver.find_elements = lambda by, xp: (
                    [Element(text='Embedded one'), Element(text='Embedded two')]
                    if xp == 'body' and driver.current_url == URL else
                    [Element(text='Direct body') for _ in range(3)] if xp == 'body' else [])
                result = env['_collect_reviews'](driver, URL, SELECTORS,
                    dict(source_url=URL, count_of_reviews=count, count_of_star_ratings='30'))
                self.assertEqual(driver.visited, expected_visits)
                self.assertEqual(result['_review_status'], 'collected')
                self.assertIn('Embedded one', result['detailed_review_content'])
                self.assertFalse(driver.opened)

    def test_duplicate_bodies_with_different_whitespace_count_once(self):
        env = detail_env()
        parts = []
        env['_merge_review_parts'](parts, [' Nice\nTV ', 'Nice TV', '', 'Other body'], 20)
        self.assertEqual(parts, ['Nice TV', 'Other body'])

    def test_page_parameter_is_replaced_instead_of_duplicated(self):
        env = detail_env()
        self.assertEqual(env['_review_page_url'](REVIEW + '&page=7&page=8#reviews', 1), REVIEW)
        self.assertEqual(env['_review_page_url'](REVIEW + '&page=7', 2), REVIEW + '&page=2')

    def test_direct_redirect_does_not_accept_other_product_body(self):
        class Redirect(Driver):
            def get(self, url):
                super().get(url)
                if '/product-reviews/' in url:
                    self.current_url = url.replace('TVS123', 'TVS999')
        env, driver = detail_env(), Redirect(panel=False)
        result = env['_collect_reviews'](driver, URL, SELECTORS,
            dict(source_url=URL, count_of_reviews='20', count_of_star_ratings='30'))
        self.assertIsNone(result['detailed_review_content'])
        self.assertEqual(result['_review_status'], 'review_body_missing')
        self.assertEqual(result['count_of_reviews'], '20')

    def test_nine_body_page_keeps_partial_data_and_uses_third_page(self):
        class NineBodies(Driver):
            def find_elements(self, by, xpath):
                if xpath == 'body' and '/product-reviews/' in self.current_url:
                    page = int(parse_qs(urlsplit(self.current_url).query).get('page', ['1'])[0])
                    indices = {1: range(9), 2: range(9, 19), 3: range(19, 29)}[page]
                    return [Element(text=f'Body {i}') for i in indices]
                return super().find_elements(by, xpath)
        env, driver = detail_env(), NineBodies()
        result = env['_collect_reviews'](driver, URL, SELECTORS,
            dict(source_url=URL, count_of_reviews='100', count_of_star_ratings='200'))
        self.assertEqual(result['_review_status'], 'collected')
        self.assertEqual(len(result['detailed_review_content'].split(' ||| ')), 20)
        self.assertEqual(driver.visited, [REVIEW, REVIEW + '&page=2', REVIEW + '&page=3'])
        self.assertFalse(driver.opened)

    def test_review_redirect_does_not_read_other_product_counts(self):
        env, driver = detail_env(), Driver()
        driver.current_url = REVIEW.replace('TVS123', 'TVS999')
        rec = {'source_url': URL}
        env['_fill_review_summary_counts'](driver, rec)
        self.assertNotIn('count_of_reviews', rec)

    def test_only_same_pid_unfiltered_https_review_links_are_accepted(self):
        self.assertTrue(same_review_url(URL, REVIEW))
        for bad in [REVIEW.replace('TVS123', 'TVS999'), REVIEW + '&an=Sound',
                    REVIEW.replace('www.flipkart.com', 'example.com'),
                    REVIEW.replace('?pid=TVS123', ''), REVIEW + '&pid=TVS999',
                    REVIEW.replace('https:', 'http:')]:
            with self.subTest(url=bad):
                self.assertFalse(same_review_url(URL, bad))

    def test_jsonld_only_recovers_exact_product_and_never_defaults_null_to_zero(self):
        data = {'@graph': [{'@type': 'Product', 'sku': 'OTHER',
                            'aggregateRating': {'ratingValue': 5, 'reviewCount': 99}},
                           {'@type': 'Product', 'sku': 'TVS123',
                            'aggregateRating': {'ratingValue': 4.2, 'ratingCount': 18648,
                                                'reviewCount': 1966}}]}
        self.assertEqual(aggregate_fields([json.dumps(data)], 'TVS123'),
                         dict(star_rating='4.2', count_of_star_ratings='18,648', count_of_reviews='1,966'))
        self.assertEqual(aggregate_fields(['{}', 'bad json'], 'TVS123'), {})


class RecoveryTests(unittest.TestCase):
    def test_collect_resume_apply_and_db_failure_preserve_audit_without_recrawling(self):
        runtime = ModuleType('fpkt.detail')
        runtime.load_selectors = Mock(return_value=SELECTORS)
        runtime.make_driver = Mock(return_value=Mock())
        runtime.crawl_review_fields = Mock(return_value=dict(
            star_rating='4.2', count_of_star_ratings='5', count_of_reviews='2',
            detailed_review_content='review1 - body', _review_status='collected'))
        with tempfile.TemporaryDirectory() as temp:
            source, output = Path(temp) / 'run.jsonl', Path(temp) / 'audit.jsonl'
            source.write_text(json.dumps(dict(account_name='flipkart', product='tv',
                batch_id='batch', stage='detail', fsn='TVS123', source_url=URL)), encoding='utf-8')
            argv = ['--input', str(source), '--product', 'tv', '--batch-id', 'batch',
                    '--output', str(output), '--sleep', '0']
            with patch.object(fpkt, 'detail', runtime, create=True), redirect_stdout(io.StringIO()):
                with patch.object(recover_reviews, 'db_connect', side_effect=AssertionError('no DB writes')):
                    self.assertEqual(recover_reviews.main(argv), 0)
                self.assertEqual(runtime.crawl_review_fields.call_count, 1)
                conn = Mock()
                with patch.object(recover_reviews, 'check_db_scope'), \
                     patch.object(recover_reviews, 'db_connect', return_value=conn), \
                     patch.object(recover_reviews, 'apply_missing', return_value=(2, 7)):
                    self.assertEqual(recover_reviews.main(argv + ['--resume', '--apply']), 0)
                conn.commit.assert_called_once()
                conn.rollback.assert_not_called()
                with patch.object(recover_reviews, 'check_db_scope'), \
                     patch.object(recover_reviews, 'db_connect', return_value=conn), \
                     patch.object(recover_reviews, 'apply_missing', side_effect=RuntimeError('synthetic DB failure')):
                    self.assertEqual(recover_reviews.main(argv + ['--resume', '--apply']), 2)
                conn.rollback.assert_called_once()
            self.assertEqual(runtime.crawl_review_fields.call_count, 1)
            self.assertEqual(runtime.make_driver.call_count, 1)
            journal = [json.loads(line) for line in output.read_text(encoding='utf-8').splitlines()]
            self.assertEqual(journal[-1]['db_errors'], 1)
            self.assertTrue(any(row['type'] == 'db_committed' for row in journal))
            self.assertEqual(journal[-2]['type'], 'db_failed')

    def test_selection_preserves_listing_review_count_and_existing_detail_values(self):
        rows = [dict(account_name='flipkart', product='tv', batch_id='batch', stage='main',
                     product_url=URL, source_url='https://www.flipkart.com/search?q=tv', fsn='TVS123',
                     count_of_reviews='1,966', star_rating='4.2', count_of_star_ratings='18,648'),
                dict(account_name='flipkart', product='tv', batch_id='batch', stage='detail',
                     source_url=URL, fsn='TVS123', star_rating='4.3'),
                dict(account_name='flipkart', product='tv', batch_id='other', stage='detail',
                     source_url=URL, fsn='TVS123')]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'run.jsonl'
            path.write_text('\n'.join(map(json.dumps, rows)), encoding='utf-8-sig')
            candidates, count = load_candidates(path, 'tv', 'batch')
        self.assertEqual(count, 1)
        self.assertEqual(candidates[0]['missing'], ['detailed_review_content'])
        self.assertEqual(candidates[0]['values']['star_rating'], '4.3')
        self.assertEqual(candidates[0]['values']['count_of_reviews'], '1,966')

    def test_zero_values_and_nonempty_body_are_preserved(self):
        row = dict(star_rating='0.0', count_of_reviews='0', detailed_review_content='existing')
        changes = fill_missing(row, dict(star_rating='4.2', count_of_reviews='2',
                                        detailed_review_content='new', count_of_star_ratings='123'))
        self.assertEqual(changes, {'count_of_star_ratings': '123'})
        self.assertEqual(row['detailed_review_content'], 'existing')
        self.assertEqual(required_fields(row), [])

    def test_resume_rejects_different_batch_and_recovers_partial_result(self):
        metadata = dict(type='metadata', batch_id='batch')
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'audit.jsonl'
            path.write_text(json.dumps(metadata) + '\n' + json.dumps(
                dict(type='collected', fsn='TVS123', values={'count_of_reviews': '2'})) + '\n',
                encoding='utf-8')
            self.assertEqual(read_resume(path, metadata)['TVS123']['count_of_reviews'], '2')
            with self.assertRaises(ValueError):
                read_resume(path, dict(type='metadata', batch_id='other'))

    def test_db_updates_only_allowed_empty_fields_with_exact_scope_and_audit_before_write(self):
        events = []
        class Cursor:
            rowcount = 1
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def execute(self, sql, params=()):
                events.append(('sql', sql, params))
                self.table = 'retail' if 'retail_com' in sql else 'listing'
            def fetchall(self):
                return [(7, '4.2', None, '1,966', None)] if self.table == 'retail' else [(8, '4.2', None, '1,966')]
        conn = SimpleNamespace(cursor=Cursor)
        values = dict(star_rating='5', count_of_star_ratings='18,648',
                      count_of_reviews='9,999', detailed_review_content='review1 - body')
        matched, changed = apply_missing(conn, 'tv', 'batch', 'TVS123', values,
                                         lambda rec: events.append(('audit', rec)))
        self.assertEqual((matched, changed), (2, 3))
        updates = [e for e in events if e[0] == 'sql' and e[1].startswith('UPDATE')]
        self.assertEqual(len(updates), 2)
        for event in updates:
            self.assertNotIn('star_rating =', event[1])
            self.assertNotIn('count_of_reviews =', event[1])
            self.assertNotIn('price', event[1])
            self.assertEqual(event[2][-3:], ('batch', 'TVS123', 'TV'))
            self.assertEqual(events[events.index(event)-1][0], 'audit')


if __name__ == '__main__':
    unittest.main()
