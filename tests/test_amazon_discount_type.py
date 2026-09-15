"""Amazon discount_type whitelist and merge regression tests."""
from __future__ import annotations

import unittest

from test_amazon_savings import ITR, listing


class AmazonDiscountTypeParserTests(unittest.TestCase):
    def test_supported_fixed_labels_are_canonicalized(self):
        for raw, expected in (
            ('Limited Time Offer', 'Limited Time Offer'),
            (' limited   time offer ', 'Limited Time Offer'),
            ('HOT DEAL', 'Hot deal'),
            ('Limited time deal', 'Limited time deal'),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(ITR.siel_log.parse_amzn_discount_type(raw), expected)

    def test_dynamic_ends_in_labels_are_preserved(self):
        for raw, expected in (
            ('Ends in', 'Ends in'),
            ('Ends in 03:21:45', 'Ends in 03:21:45'),
            (' ends   in   7h 20m ', 'Ends in 7h 20m'),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(ITR.siel_log.parse_amzn_discount_type(raw), expected)

    def test_coupon_and_other_promotions_are_rejected(self):
        for raw in (
            None,
            '',
            'You pay ₹16,950 ₹250 off coupon applied',
            'You pay ₹12,730 2% off coupon applied',
            'Coupon applied',
            'Flat INR 500 Off on Select Bank Cards',
            'Ends tomorrow',
        ):
            with self.subTest(raw=raw):
                self.assertIsNone(ITR.siel_log.parse_amzn_discount_type(raw))


class AmazonDiscountTypeMergeTests(unittest.TestCase):
    def test_invalid_listing_value_falls_back_to_valid_detail(self):
        main = listing(discount_type='You pay ₹16,950 ₹250 off coupon applied')
        detail = {'discount_type': 'Ends in 03:21:45'}
        self.assertEqual(
            ITR.make_row(main, None, detail)['discount_type'],
            'Ends in 03:21:45',
        )
        self.assertIsNone(
            ITR.make_row_listing(main, None, detail)['discount_type'])

    def test_valid_listing_value_has_priority(self):
        main = listing(discount_type='Hot deal')
        detail = {'discount_type': 'Limited time deal'}
        self.assertEqual(ITR.make_row(main, None, detail)['discount_type'], 'Hot deal')

    def test_all_amazon_products_apply_the_whitelist(self):
        for product in ITR.PRODUCT_LOWERS:
            with self.subTest(product=product):
                main = listing(
                    product=product,
                    discount_type='You pay ₹23,740 ₹1,250 off coupon applied',
                )
                self.assertIsNone(ITR.make_row(main, None, {})['discount_type'])

    def test_flipkart_discount_type_is_unchanged(self):
        main = listing(
            account_name='flipkart',
            discount_type='Saver Deal',
        )
        self.assertEqual(ITR.make_row(main, None, {})['discount_type'], 'Saver Deal')


if __name__ == '__main__':
    unittest.main()
