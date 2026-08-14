from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "fpkt_api"))

from ops_test import (  # noqa: E402
    build_schema_outputs,
    build_email_report,
    detail_from_api_response,
    detail_schema_record,
    similar_product_names,
)


def recommendation_widget(
    view_type: str,
    header: str,
    card_key: str,
    names: list[str | None],
) -> dict:
    cards = []
    for name in names:
        card_value = {}
        if name is not None:
            card_value[card_key] = {
                "value": {"label_2": {"value": {"text": name}}}
            }
        cards.append({"value": card_value})
    return {
        "widget": {
            "viewType": view_type,
            "data": {
                "dlsData": {
                    "hp_reco_header_0": {
                        "value": {"label_0": {"value": {"text": header}}}
                    },
                    "MRCSV_0": {"value": cards},
                }
            },
        }
    }


def response_with_slots(*slots: dict) -> dict:
    return {"RESPONSE": {"slots": list(slots)}}


class SimilarProductNamesTests(unittest.TestCase):
    def test_legacy_widget_is_preserved(self) -> None:
        response = response_with_slots(
            recommendation_widget(
                "pp_reco_pmu_horizontal_scrollable_ads",
                "Similar Products",
                "hp_reco_product-card_0",
                ["Product A", "Product B"],
            )
        )

        self.assertEqual(similar_product_names(response), "Product A ||| Product B")

    def test_with_pill_widget_and_discount_header_are_supported(self) -> None:
        response = response_with_slots(
            recommendation_widget(
                "pp_reco_pmu_horizontal_scrollable_ads_with_pill",
                "Washing Machines rated 4 stars and above",
                "reco_product_card_with_Pill_0",
                ["Wrong A", "Wrong B"],
            ),
            recommendation_widget(
                "pp_reco_pmu_horizontal_scrollable_ads_with_pill",
                "Discounts on Similar Products",
                "reco_product_card_with_Pill_0",
                ["Product A", "Product B"],
            ),
        )

        self.assertEqual(similar_product_names(response), "Product A ||| Product B")

    def test_order_deduplication_and_malformed_cards(self) -> None:
        response = response_with_slots(
            recommendation_widget(
                "pp_reco_pmu_horizontal_scrollable_ads_with_pill",
                "  Discounts   on Similar Products  ",
                "reco_product_card_with_Pill_0",
                [" Product A ", None, "Product B", "Product A"],
            )
        )
        cards = response["RESPONSE"]["slots"][0]["widget"]["data"]["dlsData"]["MRCSV_0"]["value"]
        cards.extend([None, "malformed", {"value": "malformed"}])

        self.assertEqual(similar_product_names(response), "Product A ||| Product B")

    def test_unrelated_header_or_view_type_is_ignored(self) -> None:
        response = response_with_slots(
            recommendation_widget(
                "pp_reco_pmu_horizontal_scrollable_ads_with_pill",
                "Similar Fully Automatic Top Load Washing Machines",
                "reco_product_card_with_Pill_0",
                ["Wrong A"],
            ),
            recommendation_widget(
                "unrelated_view_type",
                "Similar Products",
                "reco_product_card_with_Pill_0",
                ["Wrong B"],
            ),
        )

        self.assertIsNone(similar_product_names(response))

    def test_hybrid_card_falls_back_to_component_with_a_name(self) -> None:
        response = response_with_slots(
            recommendation_widget(
                "pp_reco_pmu_horizontal_scrollable_ads_with_pill",
                "Discounts on Similar Products",
                "reco_product_card_with_Pill_0",
                ["Product A"],
            )
        )
        card_data = response["RESPONSE"]["slots"][0]["widget"]["data"]["dlsData"]["MRCSV_0"]["value"][0]["value"]
        card_data["hp_reco_product-card_0"] = {"value": {"label_2": {}}}

        self.assertEqual(similar_product_names(response), "Product A")

    def test_empty_or_missing_slots_return_none(self) -> None:
        self.assertIsNone(similar_product_names({}))
        self.assertIsNone(similar_product_names(response_with_slots()))

    def test_detail_and_schema_records_keep_similar_names(self) -> None:
        response = response_with_slots(
            recommendation_widget(
                "pp_reco_pmu_horizontal_scrollable_ads_with_pill",
                "Discounts on Similar Products",
                "reco_product_card_with_Pill_0",
                ["Product A", "Product B"],
            )
        )
        detail = detail_from_api_response(
            response,
            "https://www.flipkart.com/sample/p/itm?pid=TEST123",
            "ldy",
        )
        record = detail_schema_record(
            detail,
            "ldy",
            "2026-08-14 12:00:00",
            "20260814120000",
            None,
        )

        expected = "Product A ||| Product B"
        self.assertEqual(detail["retailer_sku_name_similar"], expected)
        self.assertEqual(record["retailer_sku_name_similar"], expected)

        retail_rows, _product_list_rows, _jsonl_rows = build_schema_outputs(
            "ldy",
            [
                {
                    "product_url": detail["source_url"],
                    "product_id": "TEST123",
                    "product_name": "Sample",
                    "crawl_datetime": "2026-08-14 12:00:00",
                }
            ],
            [],
            [detail],
            [],
            "2026-08-14 12:00:00",
            "20260814120000",
        )
        self.assertEqual(retail_rows[0]["retailer_sku_name_similar"], expected)

    def test_email_warns_when_every_similar_value_is_null(self) -> None:
        retail_rows = [
            {
                "retailer_sku_name": "Sample",
                "final_sku_price": "1,000",
                "product_url": "https://www.flipkart.com/sample/p/itm?pid=TEST123",
                "retailer_sku_name_similar": None,
            }
        ]

        body, has_warning, is_sos = build_email_report(
            product="ldy",
            main_final=[],
            bsr_final=[],
            main_target=0,
            bsr_target=0,
            retail_rows=retail_rows,
            retail_cols=[],
            product_list_rows=[],
            errors=[],
            retail_db_ok=True,
            product_list_db_ok=True,
        )

        self.assertTrue(has_warning)
        self.assertFalse(is_sos)
        self.assertIn("retailer_sku_name_similar all NULL: 1/1 rows", body)


if __name__ == "__main__":
    unittest.main()
