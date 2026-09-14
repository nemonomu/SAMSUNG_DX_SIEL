"""Card-scope regressions; optional real Chrome tests use only synthetic HTML."""
from __future__ import annotations

import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from test_amazon_savings import ROOT, ITR, load_source


def load_listing():
    uc = types.ModuleType('undetected_chromedriver')
    uc.Chrome = type('ChromeStub', (), {})
    batch = types.ModuleType('siel_batch')
    batch.next_batch_id = Mock()
    with patch.dict(sys.modules, {
        'psycopg2': ITR.psycopg2, 'psycopg2.extras': ITR.psycopg2.extras,
        'config': types.ModuleType('config'), 'siel_log': ITR.siel_log,
        'siel_batch': batch, 'undetected_chromedriver': uc,
    }):
        return load_source('_quantity_listing', ROOT / 'amzn/listing.py')


LISTING = load_listing()


class ListingQuantityRoutingTests(unittest.TestCase):
    def test_main_routes_only_target_products_to_strict_capture(self):
        for product in ('tv', 'ref', 'ldy', 'hhp'):
            driver = Mock()
            driver.find_elements.return_value = [Mock()]
            captured = []
            with patch.object(LISTING, 'extract_card', return_value={'asin': 'B000000001'}) as extract, \
                    patch.object(LISTING, 'load_page', return_value=True), \
                    patch.object(LISTING, 'scroll_to_bottom'), \
                    patch.object(LISTING, 'maybe_save_html'), \
                    patch.object(LISTING.time, 'sleep'), \
                    patch.object(LISTING, 'emit', side_effect=captured.append):
                count = LISTING.crawl_main(driver, product, {'base_container': {'xpath': '//card'}},
                                           'batch', 1, 1)
            self.assertEqual(count, 1)
            self.assertEqual(extract.call_args.kwargs['collect_quantity'], product != 'hhp')
            self.assertEqual(captured[0]['product'], product)

    def test_bsr_initial_empty_retry_and_partial_retry_keep_product_scope(self):
        for product in ('tv', 'ref', 'ldy', 'hhp'):
            for initial in ([], [{'asin': 'B000000001'}],
                            [{'asin': f'B{i:09d}'} for i in range(1, 51)]):
                calls = [initial, [{'asin': 'B000000001'}]]
                with self.subTest(product=product, initial=len(initial)), \
                        patch.object(LISTING, '_load_bsr_records', side_effect=calls) as load, \
                        patch.object(LISTING, 'load_page', return_value=True), \
                        patch.object(LISTING, 'maybe_save_html'), \
                        patch.object(LISTING.time, 'sleep'), patch.object(LISTING, 'emit'):
                    self.assertEqual(LISTING.crawl_bsr(
                        Mock(), product, {'base_container': {'xpath': '//card'}}, 'batch', 1), 1)
                    for call in load.call_args_list:
                        self.assertEqual(call.kwargs['collect_quantity'], product != 'hhp')

    def test_bsr_dom_fallback_keeps_quantity_capture_flag(self):
        for enabled in (True, False):
            with patch.object(LISTING, '_js_bsr_records', return_value=[]), \
                    patch.object(LISTING, '_page_height', side_effect=LISTING.WebDriverException()), \
                    patch.object(LISTING, '_load_bsr_cards', return_value=[Mock()]), \
                    patch.object(LISTING, 'extract_card', return_value={'asin': 'B000000001'}) as extract:
                LISTING._load_bsr_records(Mock(), '//card', {}, collect_quantity=enabled)
                self.assertEqual(extract.call_args.kwargs['collect_quantity'], enabled)

    def test_card_failure_is_null_and_bad_db_selector_is_not_used(self):
        card = Mock()
        card.get_attribute.return_value = 'B000000001'
        card.parent.execute_script.side_effect = LISTING.WebDriverException()
        row = LISTING.extract_card(card, {
            'available_quantity_for_purchase': {'xpath': '//another-product'},
        }, collect_quantity=True)
        self.assertIsNone(row['available_quantity_for_purchase'])
        card.find_elements.assert_not_called()


class BrowserDriver:
    """Small WebDriver adapter: execute the actual crawler JS on a local HTML page."""
    def __init__(self, page):
        self.page = page

    def execute_script(self, source, *args):
        if args and isinstance(args[0], BrowserCard):
            return args[0].element.evaluate('(card, source) => new Function(source)(card)', source)
        return self.page.evaluate('([source, args]) => new Function(source)(...args)',
                                  [source, list(args)])


class BrowserCard:
    def __init__(self, element, driver):
        self.element, self.parent = element, driver

    def get_attribute(self, name):
        return self.element.get_attribute(name)

    def find_elements(self, by, xpath):
        return [BrowserCard(el, self.parent) for el in self.element.query_selector_all('xpath=' + xpath)]

    def find_element(self, by, xpath):
        elements = self.find_elements(by, xpath)
        if not elements:
            raise LISTING.NoSuchElementException()
        return elements[0]

    @property
    def text(self):
        return self.element.inner_text()


@unittest.skipUnless(os.environ.get('SIEL_QUANTITY_DOM_TEST') == '1',
                     'Set SIEL_QUANTITY_DOM_TEST=1 for offline real-browser regressions')
class ListingQuantityBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.playwright = sync_playwright().start()
        # Explicit disposable directory, separate from all real browser profiles.
        profile = Path(os.environ['SIEL_QUANTITY_BROWSER_DIR']).resolve()
        cls.context = cls.playwright.chromium.launch_persistent_context(
            str(profile), channel='chrome', headless=True,
            args=['--disable-background-networking', '--disable-component-update',
                  '--disable-sync', '--no-first-run', '--no-default-browser-check'])
        cls.context.route('**/*', lambda route: route.abort())
        cls.page = cls.context.new_page()
        cls.driver = BrowserDriver(cls.page)

    @classmethod
    def tearDownClass(cls):
        cls.context.close()
        cls.playwright.stop()

    def set_cards(self, notice, stage='main', extra=''):
        wrapper = ('data-component-type="s-search-result" data-asin="B000000001"'
                   if stage == 'main' else 'id="gridItemRoot"')
        self.page.set_content(f'''<div id="zg"><div {wrapper}>
            <a href="https://www.amazon.in/dp/B000000001"><h2>Example TV</h2></a>
            <span class="p13n-sc-price">₹37,500</span>{notice}</div>{extra}</div>''')
        selector = '[data-component-type="s-search-result"]' if stage == 'main' else '#gridItemRoot'
        return BrowserCard(self.page.query_selector(selector), self.driver)

    def test_only_complete_visible_quantity_notices_are_accepted(self):
        cases = [
            ('<div><span>Only 2 left in stock.</span></div>', 'Only 2 left in stock.'),
            ('<div><span>only&nbsp;<b>17</b> left  in stock</span></div>', 'only 17 left in stock'),
            ('<div>Only <span>3</span> left in stock.</div>', 'Only 3 left in stock.'),
            ('<div><span>Only<br>4 left in stock.</span></div>', 'Only 4 left in stock.'),
            ('<div>In stock</div>', None),
            ('<div>Available to ship in 1-2 days</div>', None),
            ('<div>No featured offers available</div><div>품절</div>', None),
            ('<span>2</span><div>100+ bought in past month</div>', None),
            ('<div><span>Only 0 left in stock.</span></div>', None),
            ('<div><span>Only -2 left in stock.</span></div>', None),
            ('<div><span>Only 1.5 left in stock.</span></div>', None),
            ('<div><span>Only 1-2 left in stock.</span></div>', None),
            ('<div><span>Only 2 days left</span></div>', None),
            ('<div>Not <span>Only 2 left in stock.</span></div>', None),
            ('<div><span>Only 2 left in stock.</span> Unavailable</div>', None),
            ('<div style="display:none"><span>Only 2 left in stock.</span></div>', None),
            ('<div aria-hidden="true"><span>Only 2 left in stock.</span></div>', None),
            ('<div style="opacity:0"><span>Only 2 left in stock.</span></div>', None),
            ('<h2><span>Only 2 left in stock.</span></h2>', None),
            ('<div><a href="/dp/B000000001"><span>Only 2 left in stock.</span></a></div>', None),
            ('<div data-asin="B000000002"><span>Only 2 left in stock.</span></div>', None),
            ('<div class="zg-grid-general-faceout"><a href="/dp/B000000002">Variant</a>'
             '<div><span>Only 2 left in stock.</span></div></div>', None),
            ('<div class="a-carousel-container"><div><span>Only 2 left in stock.</span></div></div>', None),
            ('<div><span>Only 2 left in stock.</span></div><div><span>Only 3 left in stock.</span></div>', None),
            ('<div><span>Only 2 left in stock.</span></div><div><span>Only 2 left in stock.</span></div>',
             'Only 2 left in stock.'),
        ]
        for stage in ('main', 'bsr'):
            for html, expected in cases:
                with self.subTest(stage=stage, html=html):
                    card = self.set_cards(html, stage)
                    if stage == 'main':
                        row = LISTING.extract_card(card, {}, collect_quantity=True)
                    else:
                        row = LISTING._js_bsr_records(self.driver, collect_quantity=True)[0]
                        unchanged = dict(row)
                        unchanged.pop('available_quantity_for_purchase')
                        self.assertEqual(unchanged, LISTING._js_bsr_records(self.driver)[0])
                    self.assertEqual(row['available_quantity_for_purchase'], expected)

    def test_neighboring_cards_do_not_supply_missing_quantity(self):
        for stage in ('main', 'bsr'):
            wrapper = ('data-component-type="s-search-result" data-asin="B000000002"'
                       if stage == 'main' else 'id="gridItemRoot"')
            card = self.set_cards('', stage, f'''<div {wrapper}>
                <a href="/dp/B000000002">Other TV</a><div>Only 9 left in stock.</div></div>''')
            if stage == 'main':
                self.assertIsNone(LISTING.extract_card(card, {}, True)['available_quantity_for_purchase'])
            else:
                rows = LISTING._js_bsr_records(self.driver, True)
                self.assertIsNone(rows[0]['available_quantity_for_purchase'])
                self.assertEqual(rows[1]['available_quantity_for_purchase'], 'Only 9 left in stock.')

    def assert_notice(self, stage, markup, expected):
        card = self.set_cards(markup, stage)
        before = self.page.content()
        row = (LISTING.extract_card(card, {}, collect_quantity=True) if stage == 'main'
               else LISTING._js_bsr_records(self.driver, collect_quantity=True)[0])
        self.assertEqual(row['available_quantity_for_purchase'], expected)
        self.assertEqual(self.page.content(), before, 'Quantity capture must not mutate page DOM')

    def test_hidden_descendants_cannot_leak_through_visible_parents(self):
        attributes = [
            'style="opacity:0"', 'aria-hidden="true"', 'hidden',
            'style="display:none"', 'style="visibility:hidden"',
            'style="content-visibility:hidden"', 'style="font-size:0"',
            'style="position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(1px,1px,1px,1px)"',
            'style="position:absolute;clip-path:inset(50%)"',
            'style="position:absolute;clip-path:inset(0 100% 0 0)"',
            'class="a-offscreen" style="position:absolute;left:-9999px"',
        ]
        for stage in ('main', 'bsr'):
            for attr in attributes:
                for inner in ('Only 2 left in stock.', '<span>Only 2 left in stock.</span>'):
                    with self.subTest(stage=stage, attr=attr, inner=inner):
                        self.assert_notice(stage, f'<div><span {attr}>{inner}</span></div>', None)

    def test_visible_quantity_survives_hidden_counts_in_same_or_separate_wrapper(self):
        for stage in ('main', 'bsr'):
            for attr in ('style="opacity:0"', 'aria-hidden="true"', 'class="a-offscreen"',
                         'style="position:absolute;clip-path:inset(50%)"'):
                for markup in (
                    f'<div><span>Only 3 left in stock.</span></div>'
                    f'<div><span {attr}>Only 2 left in stock.</span></div>',
                    f'<div><span>Only 3 left in stock.</span>'
                    f'<span {attr}>Only 2 left in stock.</span></div>',
                    f'<div>Only <span {attr}>9</span><b>3</b> left in stock.</div>',
                ):
                    with self.subTest(stage=stage, markup=markup):
                        self.assert_notice(stage, markup, 'Only 3 left in stock.')

    def test_rendered_boundaries_and_inline_markup_still_control_matching(self):
        cases = [
            ('<div>Not\n<span>Only 2 left in stock.</span></div>', None),
            ('<div><span>Only 2 left in stock.</span>\nUnavailable</div>', None),
            ('<div>Only <span style="display:contents"><b>2</b></span> left in stock.</div>',
             'Only 2 left in stock.'),
            ('<div><span style="white-space:pre-line">Only\n2 left in stock.</span></div>',
             'Only 2 left in stock.'),
            ('<div><span style="clip-path:inset(0px)">Only 2 left in stock.</span></div>',
             'Only 2 left in stock.'),
            ('<div style="visibility:hidden"><span style="visibility:visible">Only 2 left in stock.</span></div>',
             'Only 2 left in stock.'),
            ('<div style="font-size:0"><span style="font-size:16px">Only 2 left in stock.</span></div>',
             'Only 2 left in stock.'),
        ]
        for stage in ('main', 'bsr'):
            for markup, expected in cases:
                with self.subTest(stage=stage, markup=markup):
                    self.assert_notice(stage, markup, expected)

    def test_normal_cards_outside_current_viewport_are_not_treated_as_hidden(self):
        for stage in ('main', 'bsr'):
            card = self.set_cards('<div>Only 2 left in stock.</div>', stage)
            card.element.evaluate('el => { el.style.marginTop = "3000px"; }')
            row = (LISTING.extract_card(card, {}, collect_quantity=True) if stage == 'main'
                   else LISTING._js_bsr_records(self.driver, collect_quantity=True)[0])
            self.assertEqual(row['available_quantity_for_purchase'], 'Only 2 left in stock.')

    def test_bsr_fallback_and_db_boundary_use_the_same_captured_value(self):
        card = self.set_cards('<div><span>Only 2 left in stock.</span></div>', 'bsr')
        selectors = {'product_url': {'xpath': './/a[contains(@href,"/dp/")]'}}
        with patch.object(LISTING, '_js_bsr_records', return_value=[]), \
                patch.object(LISTING, '_page_height', side_effect=LISTING.WebDriverException()), \
                patch.object(LISTING, '_load_bsr_cards', return_value=[card]):
            rec = LISTING._load_bsr_records(self.driver, '//card', selectors, collect_quantity=True)[0]
        self.assertEqual(rec['available_quantity_for_purchase'], 'Only 2 left in stock.')
        for product in ('tv', 'ref', 'ldy'):
            rec.update(account_name='amazon', product=product)
            for factory in (ITR.make_row, ITR.make_row_listing):
                row = factory(None, rec, {'inventory_status': 'Only 9 left in stock.'})
                self.assertEqual(row['available_quantity_for_purchase'], 'Only 2 left in stock.')

    def test_disabled_capture_preserves_hhp_fields(self):
        card = self.set_cards('<div><span>Only 2 left in stock.</span></div>')
        self.assertNotIn('available_quantity_for_purchase', LISTING.extract_card(card, {}))
        self.set_cards('<div><span>Only 2 left in stock.</span></div>', 'bsr')
        self.assertNotIn('available_quantity_for_purchase', LISTING._js_bsr_records(self.driver)[0])


if __name__ == '__main__':
    unittest.main()
