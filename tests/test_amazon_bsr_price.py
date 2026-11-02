"""BSR price regressions. Browser cases use synthetic HTML and block all requests."""
from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from test_amazon_savings import ITR, listing
from test_amazon_listing_quantity import LISTING, BrowserCard, BrowserDriver


class BsrPriceTests(unittest.TestCase):
    def test_valid_formats_and_zero_fraction(self):
        for raw, expected in (
            ('₹35,490', '₹35,490'), ('₹35,490.00', '₹35,490'),
            ('₹1,49,999', '₹1,49,999'), ('₹149,999', '₹149,999'),
            ('₹12,34,567.50', '₹12,34,567.50'), ('₹9,999.50', '₹9,999.50'),
            ('₹999', '₹999'), ('₹0', '₹0'),
            (' ₹\u00a035,490.00 ', '₹35,490'),
        ):
            with self.subTest(raw=raw):
                # Unneeded DOM parts must not override a valid price.
                self.assertEqual(ITR.siel_log.parse_amzn_bsr_price(raw, '123', '45'),
                                 (expected, False))

    def test_malformed_prices_require_valid_dom_parts(self):
        for raw, whole, fraction, expected in (
            ('₹35,49000', '35,490', '00', '₹35,490'),
            ('₹17,99000', '17,990', '00', '₹17,990'),
            ('₹1,49,99900', '1,49,999', '00', '₹1,49,999'),
            ('₹9,99950', '9,999', '50', '₹9,999.50'),
            ('₹35,49000', None, None, None),
            ('₹35,49000', '35,490', None, None),
            ('₹35,49000', '35,490', '0', None),
            ('₹35,49000', '35,490', '000', None),
            ('₹35,49000', '35,490', 'x0', None),
            ('₹35,49000', '35,49000', '00', None),
        ):
            with self.subTest(raw=raw, whole=whole, fraction=fraction):
                self.assertEqual(ITR.siel_log.parse_amzn_bsr_price(raw, whole, fraction),
                                 (expected, expected is None))

    def test_status_and_missing_price_are_not_format_failures(self):
        for raw in (None, '', 'No featured offers available', 'Currently unavailable.',
                    'Currently unavailable (2 offers)', 'Price higher than typical'):
            with self.subTest(raw=raw):
                self.assertEqual(ITR.siel_log.parse_amzn_bsr_price(raw), (raw or None, False))

    def test_rejected_price_stays_null_through_json_and_both_tables(self):
        for product in ('ref', 'ldy', 'tv', 'hhp'):
            for redirect in (False, True):
                with self.subTest(product=product, redirect=redirect):
                    bsr = LISTING._normalize_bsr_record(listing(product, final_sku_price='₹35,49000'))
                    bsr = json.loads(json.dumps(bsr))
                    self.assertTrue(bsr['_bsr_price_invalid'])
                    detail = {'final_sku_price': '₹35,490', 'original_sku_price': '₹44,999',
                              'savings': '21%', 'redirect': redirect,
                              '_redirect_use_landing': redirect, 'landing_asin': 'B000000002'}
                    for factory in (ITR.make_row, ITR.make_row_listing):
                        row = factory(None, bsr, detail)
                        self.assertIsNone(row['final_sku_price'])
                        self.assertIsNone(row['savings'])
                        self.assertNotIn('_bsr_price_invalid', row)

    def test_missing_price_still_uses_detail_and_valid_main_still_wins(self):
        detail = {'final_sku_price': '₹35,490', 'original_sku_price': '₹44,999', 'savings': '21%'}
        bsr = LISTING._normalize_bsr_record(listing('ref', final_sku_price=None))
        self.assertNotIn('_bsr_price_invalid', bsr)
        self.assertEqual(ITR.make_row(None, bsr, detail)['final_sku_price'], '₹35,490')
        self.assertIsNone(ITR.make_row_listing(None, bsr, detail)['final_sku_price'])
        invalid_bsr = LISTING._normalize_bsr_record(listing('ref', final_sku_price='₹35,49000'))
        self.assertEqual(ITR.make_row(listing('ref'), invalid_bsr, detail)['final_sku_price'], '₹37,500')

    def test_old_raw_records_cannot_be_reformatted_into_larger_amounts(self):
        for raw in ('₹35,49000', '₹1,49,99900', '₹1,,000', '35,49000'):
            for product in ('ref', 'ldy', 'tv', 'hhp'):
                with self.subTest(raw=raw, product=product):
                    for factory in (ITR.make_row, ITR.make_row_listing):
                        row = factory(None, listing(product, final_sku_price=raw),
                                      {'final_sku_price': '₹35,490', 'savings': '21%'})
                        self.assertIsNone(row['final_sku_price'])
                        self.assertIsNone(row['savings'])


class PriceBrowserDriver(BrowserDriver):
    def execute_script(self, source, *args):
        return self.page.evaluate('([source, args]) => new Function(source)(...args)',
                                  [source, [a.element if isinstance(a, BrowserCard) else a for a in args]])


@unittest.skipUnless(os.environ.get('SIEL_BSR_PRICE_DOM_TEST') == '1',
                     'Set SIEL_BSR_PRICE_DOM_TEST=1 for offline browser regressions')
class BsrPriceBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(
            channel='chrome', headless=True,
            args=['--disable-background-networking', '--disable-component-update',
                  '--disable-sync', '--no-first-run', '--no-default-browser-check'])
        cls.context = cls.browser.new_context()
        cls.context.route('**/*', lambda route: route.abort())
        cls.page = cls.context.new_page()
        cls.driver = PriceBrowserDriver(cls.page)

    @classmethod
    def tearDownClass(cls):
        cls.context.close()
        cls.browser.close()
        cls.playwright.stop()

    def set_card(self, markup, extra=''):
        self.page.set_content(f'''<div id="zg"><div id="gridItemRoot">
            <a href="https://www.amazon.in/dp/B000000001">Example refrigerator</a>
            {markup}</div>{extra}</div>''')
        return BrowserCard(self.page.query_selector('#gridItemRoot'), self.driver)

    def paths(self, card):
        js = LISTING._normalize_bsr_record(LISTING._js_bsr_records(self.driver)[0])
        selectors = {
            'final_sku_price': {
                'xpath': './/span[contains(@class,"p13n-sc-price")] | '
                         './/span[contains(@class,"a-price")]//span[@class="a-offscreen"]',
                'fallback': './/span[contains(@class,"a-color-price")]',
            },
            'product_url': {'xpath': './/a[contains(@href,"/dp/")]'},
        }
        with patch.object(LISTING, '_js_bsr_records', return_value=[]), \
                patch.object(LISTING, '_page_height', side_effect=LISTING.WebDriverException()), \
                patch.object(LISTING, '_load_bsr_cards', return_value=[card]):
            xpath = LISTING._load_bsr_records(self.driver, '//card', selectors)[0]
        return js, xpath

    def test_recovery_normal_formats_and_storage_on_both_paths(self):
        for raw, whole, fraction, decimal, captured, stored in (
            ('₹35,49000', '35,490', '00', '', '₹35,490', '₹35,490'),
            ('₹17,99000', '17,990', '00', '.', '₹17,990', '₹17,990'),
            ('₹1,49,99900', '1,49,999', '00', '', '₹1,49,999', '₹149,999'),
            ('₹9,99950', '9,999', '50', '.', '₹9,999.50', '₹9,999.50'),
            ('₹35,490', '999', '99', '', '₹35,490', '₹35,490'),
            ('₹35,490.00', '999', '99', '.', '₹35,490', '₹35,490'),
            ('₹1,49,999', '999', '99', '', '₹1,49,999', '₹149,999'),
        ):
            with self.subTest(raw=raw):
                card = self.set_card(f'''<span class="a-price" data-a-color="base">
                    <span class="a-offscreen">{raw}</span><span aria-hidden="true">
                    <span class="a-price-symbol">₹</span><span class="a-price-whole">{whole}<span
                    class="a-price-decimal">{decimal}</span></span>
                    <span class="a-price-fraction">{fraction}</span></span></span>''')
                before = self.page.content()
                for row in self.paths(card):
                    self.assertEqual(row['final_sku_price'], captured)
                    self.assertNotIn('_bsr_price_invalid', row)
                    self.assertNotIn('_bsr_price_parts', row)
                    row.update(account_name='amazon', product='ref')
                    self.assertEqual(ITR.make_row(None, row, {})['final_sku_price'], stored)
                self.assertEqual(self.page.content(), before)

    def test_unrecoverable_prices_do_not_borrow_other_price_parts(self):
        parts = '<span class="a-price-whole">35,490</span><span class="a-price-fraction">00</span>'
        bad = '<span class="a-offscreen">₹35,49000</span>'
        cases = (
            f'<span class="a-price">{bad}</span><span class="a-price">{parts}</span>',
            f'<span class="a-price">{bad}<span class="a-price">{parts}</span></span>',
            f'<span class="a-price">{bad}<span data-asin="B000000002">{parts}</span></span>',
            f'<span class="a-price a-text-price" data-a-strike="true">{bad}{parts}</span>',
            f'<span class="p13n-sc-price">₹35,49000</span><span class="a-price">{parts}</span>',
            f'<span class="a-price">{bad}<span class="a-price-whole">35,490</span></span>',
            f'<span class="a-price">{bad}{parts}{parts}</span>',
            '<span class="a-color-price">₹35,49000</span>',
        )
        for markup in cases:
            with self.subTest(markup=markup):
                card = self.set_card(markup, f'''<div id="gridItemRoot">
                    <a href="/dp/B000000002">Other refrigerator</a>
                    <span class="a-price">{parts}</span></div>''')
                for row in self.paths(card):
                    self.assertIsNone(row['final_sku_price'])
                    self.assertTrue(row['_bsr_price_invalid'])
                    row.update(account_name='amazon', product='ref')
                    self.assertIsNone(ITR.make_row(None, row, {'final_sku_price': '₹35,490'})['final_sku_price'])

    def test_absent_and_unavailable_prices_keep_existing_behavior(self):
        for markup, expected in (
            ('', None),
            ('<span class="p13n-sc-price">No featured offers available</span>', 'No featured offers available'),
            ('<span class="p13n-sc-price">₹35,490</span>', '₹35,490'),
            ('<span class="a-color-price">₹35,490</span>', '₹35,490'),
        ):
            with self.subTest(markup=markup):
                for row in self.paths(self.set_card(markup)):
                    self.assertEqual(row['final_sku_price'], expected)
                    self.assertNotIn('_bsr_price_invalid', row)


if __name__ == '__main__':
    unittest.main()
