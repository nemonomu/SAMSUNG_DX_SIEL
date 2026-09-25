"""Offline regressions for listing quantity in TV/REF/LDY and unchanged HHP."""
from __future__ import annotations

import io
import json
import os
import sys
import types
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import MagicMock, Mock, mock_open, patch

from test_amazon_savings import ITR, ROOT, listing, load_source


class AmazonAvailableQuantityTests(unittest.TestCase):
    def test_explicit_positive_quantity_is_copied_without_rewriting(self):
        for status in ('Only 1 left in stock.', 'Only 2 left in stock.',
                       'Only 17 left in stock', 'Only 999 left in stock.',
                       'only 3 left in stock.', 'ONLY 4 LEFT IN STOCK.',
                       ' Only\u00a05\nleft  in stock. '):
            with self.subTest(status=status):
                self.assertEqual(ITR.amazon_available_quantity(status), status)

    def test_availability_dates_and_non_quantity_text_are_null(self):
        for status in (None, '', ' ', 'In stock', 'In Stock.', '품절',
                       'Available to ship in 1-2 days', 'No featured offers available',
                       'Only 2 days left', 'Only 2 left in stock in 1-2 days',
                       '2 offers from ₹1,000', 'Only 0 left in stock.',
                       'Only -1 left in stock.', 'Only 1.5 left in stock.',
                       'Only 1-2 left in stock.', 'Only a few left in stock.',
                       'Not Only 2 left in stock.', 'Only 2 left in stock. Unavailable',
                       2, True, ['Only 2 left in stock.']):
            with self.subTest(status=status):
                self.assertIsNone(ITR.amazon_available_quantity(status))

    def test_target_products_use_listing_and_hhp_keeps_detail_inventory(self):
        for product in ITR.PRODUCT_LOWERS:
            for account in ('amazon', 'Amazon', 'AMAZON'):
                with self.subTest(product=product, account=account):
                    main = listing(product, account_name=account,
                                   available_quantity_for_purchase='Only 9 left in stock.')
                    detail = {'inventory_status': 'Only 2 left in stock.'}
                    before_main, before_detail = dict(main), dict(detail)
                    row = ITR.make_row(main, None, detail)
                    self.assertEqual(row['available_quantity_for_purchase'],
                                     detail['inventory_status'] if product == 'hhp'
                                     else 'Only 9 left in stock.')
                    self.assertEqual(row['inventory_status'], detail['inventory_status'])
                    self.assertEqual(row['savings'], '₹16,990' if product == 'hhp' else None)
                    self.assertEqual(row['final_sku_price'], '₹37,500')
                    self.assertEqual(main, before_main)
                    self.assertEqual(detail, before_detail)
                    self.assertIn('available_quantity_for_purchase', ITR.COLUMNS_BY_PRODUCT[product])

    def test_listing_quantity_survives_unknown_detail_inventory(self):
        main = listing(available_quantity_for_purchase='Only 9 left in stock.')
        for detail in ({'inventory_status': 'In stock'},
                       {'inventory_status': 'Available to ship in 1-2 days'},
                       {'inventory_status': None}, {'asin': 'B000000001'}):
            with self.subTest(detail=detail):
                row = ITR.make_row(main, None, detail)
                self.assertEqual(row['available_quantity_for_purchase'], 'Only 9 left in stock.')
                self.assertEqual(row['inventory_status'], detail.get('inventory_status'))

    def test_product_list_never_uses_detail_inventory(self):
        detail = {'inventory_status': 'Only 2 left in stock.'}
        row = ITR.make_row_listing(listing(), None, detail)
        self.assertIsNone(row['available_quantity_for_purchase'])
        self.assertIsNone(row['inventory_status'])
        main = listing(available_quantity_for_purchase='listing-only quantity')
        self.assertIsNone(ITR.make_row_listing(main, None, detail)['available_quantity_for_purchase'])

    def test_listing_quantity_does_not_require_detail(self):
        for detail in (None, {}):
            with self.subTest(detail=detail):
                main = listing(available_quantity_for_purchase='Only 9 left in stock.')
                row = ITR.make_row(main, None, detail)
                self.assertEqual(row['available_quantity_for_purchase'], 'Only 9 left in stock.')
                self.assertIsNone(row['inventory_status'])

    def test_merge_passes_same_listing_quantity_to_both_tables(self):
        main = listing(available_quantity_for_purchase='Only 9 left in stock.')
        records = {'sample': {'main': main, 'bsr': None}}
        self.assertEqual(ITR.merge(records, {})[0]['available_quantity_for_purchase'],
                         'Only 9 left in stock.')
        self.assertEqual(ITR.merge(records, {}, listing_only=True)[0]['available_quantity_for_purchase'],
                         'Only 9 left in stock.')

    def test_bsr_only_never_uses_detail_inventory(self):
        row = ITR.make_row(None, listing(bsr_rank=1), {'inventory_status': 'Only 1 left in stock.'})
        self.assertIsNone(row['available_quantity_for_purchase'])
        bsr = listing(bsr_rank=1, available_quantity_for_purchase='Only 4 left in stock.')
        row = ITR.make_row(None, bsr, {'inventory_status': 'Only 1 left in stock.'})
        self.assertEqual(row['available_quantity_for_purchase'], 'Only 4 left in stock.')

    def test_skipped_detail_cannot_supply_quantity(self):
        for reason in ('asin_mismatch', 'continue_shopping_page'):
            with self.subTest(reason=reason):
                row = ITR.make_row(listing(), None, {
                    '_detail_skip': reason, 'redirect': True,
                    'inventory_status': 'Only 2 left in stock.',
                })
                self.assertIsNone(row['available_quantity_for_purchase'])

    def test_accepted_redirect_never_copies_stock_between_different_asins(self):
        row = ITR.make_row(listing(available_quantity_for_purchase='Only 9 left in stock.'), None, {
            'redirect': True, '_redirect_use_landing': True,
            'landing_asin': 'B000000002', 'inventory_status': 'Only 3 left in stock.',
        })
        self.assertEqual(row['item'], 'B000000002')
        self.assertIsNone(row['available_quantity_for_purchase'])

    def test_missing_or_invalid_main_quantity_does_not_use_bsr_or_detail(self):
        for product in ('tv', 'ref', 'ldy'):
            for value in (None, '', 'In stock', 'Available to ship in 1-2 days',
                          'Only 2 left in stock. Unavailable', '2', 2, True):
                with self.subTest(product=product, value=value):
                    main = listing(product, available_quantity_for_purchase=value)
                    bsr = listing(product, available_quantity_for_purchase='Only 7 left in stock.')
                    detail = {'inventory_status': 'Only 2 left in stock.'}
                    for factory in (ITR.make_row, ITR.make_row_listing):
                        row = factory(main, bsr, detail)
                        self.assertIsNone(row['available_quantity_for_purchase'])
                        self.assertEqual(row['page_type'], 'main')

    def test_bsr_and_skipped_detail_keep_stock_for_same_listing_asin(self):
        for product in ('tv', 'ref', 'ldy'):
            for stage in ('main', 'bsr'):
                for detail in (None, {}, {'_detail_skip': 'asin_mismatch', 'redirect': True,
                                         'asin': 'B000000001', 'inventory_status': 'Only 2 left in stock.'}):
                    with self.subTest(product=product, stage=stage, detail=detail):
                        rec = listing(product, available_quantity_for_purchase='Only 7 left in stock.')
                        for factory in (ITR.make_row, ITR.make_row_listing):
                            row = factory(rec if stage == 'main' else None,
                                          rec if stage == 'bsr' else None, detail)
                            self.assertEqual(row['available_quantity_for_purchase'], 'Only 7 left in stock.')

    def test_other_accounts_keep_listing_quantity(self):
        for account in ('flipkart', 'other'):
            for product in ITR.PRODUCT_LOWERS:
                with self.subTest(account=account, product=product):
                    main = listing(product, account_name=account,
                                   available_quantity_for_purchase='Only 7 left')
                    detail = {'inventory_status': 'Only 2 left in stock.'}
                    row = ITR.make_row(main, None, detail)
                    self.assertEqual(row['available_quantity_for_purchase'], 'Only 7 left')
                    self.assertEqual(row['inventory_status'], detail['inventory_status'])

    def test_stream_insert_uses_listing_quantity_except_hhp_retail(self):
        amzn = types.ModuleType('amzn')
        amzn.listing = types.ModuleType('amzn.listing')
        amzn.detail = types.ModuleType('amzn.detail')
        with patch.dict(sys.modules, {'amzn': amzn}), \
                patch.dict(os.environ, {'AMZN_AUTO_APPLY_SQL': '0'}):
            runner = load_source('_quantity_run', ROOT / 'amzn/run.py')
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
                for status, expected in (('Only 2 left in stock.', 'Only 2 left in stock.'),
                                         ('In stock', None),
                                         ('Available to ship in 1-2 days', None), (None, None)):
                    with self.subTest(product=product, status=status):
                        cursor.reset_mock()
                        connection.reset_mock()
                        runner._main_cache = {'B000000001': listing(
                            product, available_quantity_for_purchase='Only 9 left in stock.')}
                        runner._stream_insert({'asin': 'B000000001', 'inventory_status': status})
                        inserts = [call.args for call in cursor.execute.call_args_list
                                   if call.args[0].startswith('INSERT INTO')]
                        self.assertEqual(len(inserts), 2)
                        for (sql, row), suffix in zip(inserts, ('retail_com', 'product_list')):
                            self.assertTrue(sql.startswith(f'INSERT INTO dx_siel_{product}_{suffix} '))
                            self.assertIn('%(available_quantity_for_purchase)s', sql)
                            self.assertEqual(row['available_quantity_for_purchase'],
                                             expected if product == 'hhp' and suffix == 'retail_com'
                                             else 'Only 9 left in stock.')
                            self.assertEqual(row['savings'], '₹16,990' if product == 'hhp' or suffix == 'product_list' else None)
                        connection.commit.assert_called_once()
                        connection.rollback.assert_not_called()

    def test_batch_insert_uses_listing_quantity_except_hhp_retail(self):
        records, expected = [], {}
        for product in ITR.PRODUCT_LOWERS:
            for status, quantity in (('Only 1 left in stock.', 'Only 1 left in stock.'),
                                      ('In stock', None),
                                      ('Available to ship in 1-2 days', None), (None, None),
                                      ('missing-detail', None)):
                asin = f'B{len(expected) + 1:09d}'
                main = listing(product, stage='main', asin=asin,
                               product_url=f'https://www.amazon.in/dp/{asin}',
                               available_quantity_for_purchase='Only 9 left in stock.')
                detail = {'stage': 'detail', 'asin': asin, 'source_url': main['product_url'],
                          'account_name': 'amazon', 'inventory_status': status}
                records.append(main)
                if status != 'missing-detail':
                    records.append(detail)
                expected[asin] = quantity if product == 'hhp' else 'Only 9 left in stock.'
        source = '\n'.join(json.dumps(row, ensure_ascii=False) for row in records)
        conn, batch = MagicMock(), Mock()
        with patch.object(ITR.config, 'DB_CONFIG', {}, create=True), \
                patch.object(ITR.psycopg2, 'connect', Mock(return_value=conn), create=True), \
                patch.object(ITR.psycopg2.extras, 'execute_batch', batch, create=True), \
                patch.object(ITR.siel_item_mst, 'fill_from_mst', Mock(return_value=0), create=True), \
                patch.object(ITR.siel_item_mst, 'upsert_item_mst_batch', Mock(return_value=0), create=True), \
                patch.object(ITR.os.path, 'exists', return_value=True), \
                patch.dict(ITR.os.environ, {'SIEL_INSERT_DRY_RUN': '0'}), \
                patch('builtins.open', mock_open(read_data=source)), \
                patch.object(sys, 'argv', ['insert_test_retail_com.py', 'synthetic.jsonl', '0']), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(ITR.main(), 0)
        self.assertEqual(batch.call_count, 8)
        tables, count = set(), 0
        for call in batch.call_args_list:
            sql, rows = call.args[1:3]
            table = sql.split()[2]
            tables.add(table)
            self.assertIn('%(available_quantity_for_purchase)s', sql)
            for row in rows:
                self.assertEqual(row['available_quantity_for_purchase'],
                                 expected[row['item']] if table.endswith('_retail_com')
                                 else 'Only 9 left in stock.')
                self.assertEqual(row['savings'], '₹16,990' if table.endswith('_product_list')
                                 or '_hhp_' in table else None)
                count += 1
        self.assertEqual(count, 40)
        self.assertEqual(tables, {f'dx_siel_{p}_{s}' for p in ITR.PRODUCT_LOWERS
                                 for s in ('retail_com', 'product_list')})
        self.assertEqual(conn.commit.call_count, 4)
        conn.rollback.assert_not_called()


if __name__ == '__main__':
    unittest.main()
