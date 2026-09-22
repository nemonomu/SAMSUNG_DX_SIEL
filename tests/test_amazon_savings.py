"""Offline price/INSERT regression tests; never import real DB configuration."""
from __future__ import annotations

import importlib.util
import os
import sys
import types
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]


def load_source(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_insert_module():
    pg = types.ModuleType('psycopg2')
    pg.extras = types.ModuleType('psycopg2.extras')
    stubs = {
        'psycopg2': pg,
        'psycopg2.extras': pg.extras,
        'config': types.ModuleType('config'),
        'siel_item_mst': types.ModuleType('siel_item_mst'),
        'siel_log': load_source('_savings_log', ROOT / 'siel_log.py'),
    }
    with patch.dict(sys.modules, stubs):
        return load_source('_savings_insert', ROOT / 'insert_test_retail_com.py')


ITR = load_insert_module()


def listing(product='tv', **changes):
    row = {
        'account_name': 'amazon', 'product': product, 'asin': 'B000000001',
        'product_url': 'https://www.amazon.in/dp/B000000001',
        'final_sku_price': '₹37,500', 'original_sku_price': '₹54,490',
        'savings': 'stale value', 'main_rank': 1,
    }
    row.update(changes)
    return row


class AmazonSavingsTests(unittest.TestCase):
    def test_user_examples(self):
        for final, original, expected in (
            ('₹37,500', '₹54,490', '₹16,990'),
            ('₹36,490', '₹55,900', '₹19,410'),
            ('₹160,990', '₹275,000', '₹114,010'),
            ('₹4,129', '₹7,999', '₹3,870'),
            ('₹40,990', '₹59,490', '₹18,500'),
        ):
            with self.subTest(final=final):
                self.assertEqual(ITR.amazon_price_fields(final, original, None),
                                 (final, original, expected))

    def test_formats_and_exact_decimal_subtraction(self):
        for final, original, expected in (
            ('₹1,60,990', '₹2,75,000', '₹114,010'),
            (' ₹\u00a037,500.00 ', ' 54,490.00 ', '₹16,990'),
            (37500, 54490, '₹16,990'),
            (Decimal('99.99'), Decimal('100'), '₹0.01'),
            ('₹2,297.99', '₹3,499.99', '₹1,202'),
            ('₹999.90', '₹1,000', '₹0.10'),
            ('₹999.50', '₹1,001', '₹1.50'),
            ('₹1,000', '₹1,000', '₹0'),
            ('₹1', '₹100', '₹99'),
        ):
            with self.subTest(final=final, original=original):
                self.assertEqual(ITR.amazon_price_fields(final, original, 'old')[2], expected)

    def test_non_price_or_missing_values_never_become_savings(self):
        values = (
            None, '', ' ', 'No featured offers available', '품절',
            'Currently unavailable', 'Currently unavailable (2 offers)',
            'Only 2 left', '₹1,000 per month', 'From ₹1,000',
            '₹1,000 - ₹2,000', '₹1,000 (10% off)', '₹1,2,3',
            '₹1,,000', '₹,100', '₹100,', '$1,000', '₹-100', '-100',
            '₹1.234', 'NaN', 'Infinity', float('nan'), float('inf'), True,
        )
        for value in values:
            with self.subTest(value=value, field='final'):
                self.assertIsNone(ITR.amazon_price_fields(value, '₹54,490', 'old')[2])
            with self.subTest(value=value, field='original'):
                self.assertIsNone(ITR.amazon_price_fields('₹37,500', value, 'old')[2])

    def test_zero_and_negative_difference_are_not_discounts(self):
        for final, original in (('₹0', '₹100'), ('₹0', '₹0'),
                                ('₹100', '₹0'), ('₹101', '₹100')):
            with self.subTest(final=final, original=original):
                self.assertIsNone(ITR.amazon_price_fields(final, original, 'old')[2])

    def test_unavailable_price_keeps_existing_price_field_behavior(self):
        for status in ('No featured offers available', '품절'):
            self.assertEqual(ITR.amazon_price_fields(status, '₹54,490', 'old'),
                             (status, None, None))

    def test_detail_savings_requires_page_value_for_tv_ref_ldy(self):
        for product in ITR.PRODUCT_LOWERS:
            for account in ('amazon', 'Amazon', 'AMAZON'):
                with self.subTest(product=product, account=account):
                    main = listing(product, account_name=account)
                    before = dict(main)
                    retail_row = ITR.make_row(main, None, {})
                    list_row = ITR.make_row_listing(main, None)
                    self.assertEqual(retail_row['savings'], '₹16,990' if product == 'hhp' else None)
                    self.assertEqual(list_row['savings'], '₹16,990')
                    for row in (retail_row, list_row):
                        self.assertEqual(row['original_sku_price'], '₹54,490')
                        self.assertEqual(row['final_sku_price'], '₹37,500')
                    self.assertEqual(main, before)
                    self.assertIn('savings', ITR.COLUMNS_BY_PRODUCT[product])
                    self.assertIn('savings', ITR.COLUMNS_LIST_AMZN)

    def test_visible_detail_percentages_are_normalized_without_calculation(self):
        for product, visible, expected in (('tv', '-50%', '50%'),
                                           ('ref', '-18%', '18%'),
                                           ('ldy', '-61%', '61%')):
            with self.subTest(product=product):
                main = listing(product)
                detail = {'savings': visible}
                self.assertEqual(ITR.make_row(main, None, detail)['savings'], expected)
                self.assertEqual(ITR.make_row_listing(main, None, detail)['savings'], '₹16,990')

    def test_detail_parser_removes_only_the_leading_minus(self):
        for value, expected in (('-36%', '36%'), ('  -50%  ', '50%'),
                                ('18%', '18%'), ('-61.5%', '61.5%')):
            with self.subTest(value=value):
                self.assertEqual(ITR.siel_log.parse_amzn_savings_percentage(value), expected)

    def test_invalid_detail_percentages_do_not_fall_back_to_price_difference(self):
        for value in (None, '', 'stale value', '₹16,990', '-50% off'):
            with self.subTest(value=value):
                self.assertIsNone(ITR.make_row(listing(), None, {'savings': value})['savings'])

    def test_detail_fallback_does_not_leak_into_product_list(self):
        main = listing(final_sku_price=None, original_sku_price=None)
        detail = {'final_sku_price': '₹37,500', 'original_sku_price': '₹54,490',
                  'savings': '-31%'}
        self.assertEqual(ITR.make_row(main, None, detail)['savings'], '31%')
        self.assertIsNone(ITR.make_row_listing(main, None, detail)['savings'])

    def test_listing_prices_and_status_take_priority_over_detail(self):
        detail = {'final_sku_price': '₹1,000', 'original_sku_price': '₹2,000',
                  'savings': '-50%'}
        row = ITR.make_row(listing(), None, detail)
        self.assertEqual(row['savings'], '50%')
        row = ITR.make_row(listing(final_sku_price='No featured offers available'), None, detail)
        self.assertEqual(row['final_sku_price'], 'No featured offers available')
        self.assertIsNone(row['savings'])

    def test_malformed_amount_is_rejected_before_comma_normalization(self):
        for field in ('final_sku_price', 'original_sku_price'):
            row = ITR.make_row(listing('hhp', **{field: '₹1,,000'}), None, {})
            self.assertIsNone(row['savings'])

    def test_bsr_only_and_redirect_price_selection(self):
        bsr = listing(bsr_rank=1)
        self.assertIsNone(ITR.make_row(None, bsr, {})['savings'])
        detail = {
            'redirect': True, '_redirect_use_landing': True,
            'landing_asin': 'B000000002', 'final_sku_price': '₹100',
            'original_sku_price': '₹250', 'savings': '-60%',
        }
        row = ITR.make_row(listing(), None, detail)
        self.assertEqual(row['item'], 'B000000002')
        self.assertEqual(row['savings'], '60%')
        self.assertEqual(ITR.make_row_listing(listing(), None, detail)['savings'], '₹16,990')

    def test_flipkart_retains_percentage_and_invalid_price_policy(self):
        for product in ITR.PRODUCT_LOWERS:
            with self.subTest(product=product):
                main = listing(product, account_name='flipkart',
                               final_sku_price='₹750', original_sku_price='₹1,000')
                self.assertEqual(ITR.make_row(main, None, {})['savings'], '25%')
                self.assertEqual(ITR.make_row_listing(main, None)['savings'], '25%')
        self.assertEqual(ITR.normalize_fpkt_price_values('₹100', '₹90'), ('₹100', None, None))

    def test_other_account_keeps_existing_savings(self):
        row = ITR.make_row(listing(account_name='other', savings='unchanged'), None, {})
        self.assertEqual(row['savings'], 'unchanged')

    def test_stream_insert_receives_savings_for_all_eight_tables(self):
        amzn = types.ModuleType('amzn')
        amzn.listing = types.ModuleType('amzn.listing')
        amzn.detail = types.ModuleType('amzn.detail')
        with patch.dict(sys.modules, {'amzn': amzn}), \
                patch.dict(os.environ, {'AMZN_AUTO_APPLY_SQL': '0'}):
            runner = load_source('_savings_run', ROOT / 'amzn' / 'run.py')
        cursor, connection = Mock(), Mock()
        mst = types.ModuleType('siel_item_mst')
        mst.upsert_item_mst_batch = Mock()
        pg = types.ModuleType('psycopg2')
        pg.connect = Mock(return_value=connection)
        connection.cursor.return_value = cursor
        cfg = types.ModuleType('config')
        cfg.DB_CONFIG = {}
        with patch.dict(sys.modules, {'insert_test_retail_com': ITR, 'siel_item_mst': mst,
                                      'psycopg2': pg, 'config': cfg}):
            runner._setup_db()
            for product in ITR.PRODUCT_LOWERS:
                for final, expected in (('₹37,500', '₹16,990'),
                                        ('No featured offers available', None),
                                        ('Currently unavailable (2 offers)', None)):
                    with self.subTest(product=product, final=final):
                        cursor.reset_mock()
                        connection.reset_mock()
                        runner._main_cache = {'B000000001': listing(product, final_sku_price=final)}
                        detail = {'asin': 'B000000001'}
                        if product != 'hhp' and expected is not None:
                            detail['savings'] = '-50%'
                        runner._stream_insert(detail)
                        inserts = [call.args for call in cursor.execute.call_args_list
                                   if call.args[0].startswith('INSERT INTO')]
                        self.assertEqual(len(inserts), 2)
                        for (sql, row), suffix in zip(inserts, ('retail_com', 'product_list')):
                            self.assertTrue(sql.startswith(f'INSERT INTO dx_siel_{product}_{suffix} '))
                            self.assertIn('%(savings)s', sql)
                            expected_savings = ('50%' if suffix == 'retail_com' and product != 'hhp'
                                                and expected is not None else expected)
                            self.assertEqual(row['savings'], expected_savings)
                            self.assertEqual(row['final_sku_price'], final)
                        connection.commit.assert_called_once()
                        connection.rollback.assert_not_called()


if __name__ == '__main__':
    unittest.main()
