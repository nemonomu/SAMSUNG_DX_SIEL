from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path
from typing import Any

from ops_test import (
    detail_api_response,
    detail_from_api_response,
    detail_from_html,
    detail_price_values,
    iter_dicts,
    original_price_text,
    percent_text_from_text,
    price_text,
    rupee_amounts_from_text,
    scalar_texts,
    text_or_none,
    to_int,
)


def calc_savings(final_price: Any, original_price: Any) -> float | None:
    final_int = to_int(final_price)
    original_int = to_int(original_price)
    if final_int is None or original_int in (None, 0):
        return None
    return (original_int - final_int) * 100.0 / original_int


def is_mismatch(final_price: Any, original_price: Any, savings: Any) -> bool:
    calculated = calc_savings(final_price, original_price)
    stored = to_int(savings)
    if calculated is None or stored is None:
        return False
    return abs(calculated - stored) > 1.0


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def find_rows(path: Path, pid: str | None) -> list[dict[str, str]]:
    rows = read_csv_rows(path)
    if not pid:
        return rows
    matches: list[dict[str, str]] = []
    for row in rows:
        text = " ".join(str(value or "") for value in row.values())
        if pid in text:
            matches.append(row)
    return matches


def row_summary(row: dict[str, Any]) -> dict[str, Any]:
    final_price = row.get("final_sku_price") or row.get("final_price")
    original_price = row.get("original_sku_price") or row.get("original_price")
    savings = row.get("savings")
    calculated = calc_savings(final_price, original_price)
    return {
        "item": row.get("item") or row.get("fsn") or row.get("product_id"),
        "stage": row.get("stage") or row.get("page_type"),
        "name": row.get("retailer_sku_name") or row.get("product_name"),
        "url": row.get("product_url") or row.get("source_url"),
        "final": final_price,
        "original": original_price,
        "savings": savings,
        "calculated_savings": None if calculated is None else round(calculated, 2),
        "mismatch": is_mismatch(final_price, original_price, savings),
    }


def shorten(value: Any, limit: int = 260) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, default=str)
    else:
        text = str(value)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        return text[:limit] + "..."
    return text


def amount_candidates(value: Any) -> list[int]:
    amounts: list[int] = []
    if isinstance(value, (int, float)):
        amount = int(value)
        if amount >= 1000:
            return [amount]
    for amount in rupee_amounts_from_text(value, allow_plain=True):
        if amount not in amounts:
            amounts.append(amount)
    if isinstance(value, dict):
        for text in scalar_texts(value):
            for amount in rupee_amounts_from_text(text, allow_plain=True):
                if amount not in amounts:
                    amounts.append(amount)
    return amounts[:8]


def percent_candidates(value: Any) -> list[str]:
    percents: list[str] = []
    direct = percent_text_from_text(value)
    if direct:
        percents.append(direct)
    if isinstance(value, dict):
        for text in scalar_texts(value):
            percent = percent_text_from_text(text)
            if percent and percent not in percents:
                percents.append(percent)
    return percents[:8]


def interesting_price_key(key: Any) -> bool:
    compact = str(key).replace("-", "_").lower().replace("_", "")
    if compact in {
        "fsp",
        "finalprice",
        "mrp",
        "nepprice",
        "buyatprice",
        "xtrasaverprice",
        "specialprice",
        "sellingprice",
        "discountedprice",
        "originalprice",
        "listprice",
        "totaldiscount",
    }:
        return True
    return any(token in compact for token in ("price", "mrp", "discount", "saving"))


def price_field_candidates(slot: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in iter_dicts(slot):
        for key, value in item.items():
            if not interesting_price_key(key):
                continue
            amounts = amount_candidates(value)
            percents = percent_candidates(value)
            preview = shorten(value)
            if not amounts and not percents and not preview:
                continue
            rows.append(
                {
                    "key": str(key),
                    "amounts": amounts,
                    "percents": percents,
                    "value": preview,
                }
            )
            if len(rows) >= 35:
                return rows
    return rows


def price_text_candidates(slot: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for text in scalar_texts(slot):
        lowered = text.lower()
        if not (
            "\u20b9" in text
            or "%" in text
            or "price" in lowered
            or "mrp" in lowered
            or "off" in lowered
            or "buy at" in lowered
            or "lowest price" in lowered
        ):
            continue
        normalized = re.sub(r"\s+", " ", text).strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        rows.append(
            {
                "text": shorten(normalized, 220),
                "amounts": amount_candidates(normalized),
                "percents": percent_candidates(normalized),
            }
        )
        if len(rows) >= 25:
            break
    return rows


def price_slot_summaries(response: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    slots = (response.get("RESPONSE") or {}).get("slots") or []
    for index, slot in enumerate(slots):
        widget = slot.get("widget") or {}
        widget_type = widget.get("type")
        view_type = widget.get("viewType")
        if widget_type != "ATLAS_NEP_V2" and view_type != "pp_pricing_price_summary":
            continue
        rows.append(
            {
                "slot_index": index,
                "widget_type": widget_type,
                "view_type": view_type,
                "field_candidates": price_field_candidates(slot),
                "text_candidates": price_text_candidates(slot),
            }
        )
    return rows


def probe_csv(path: Path, pid: str | None) -> list[dict[str, Any]]:
    return [row_summary(row) for row in find_rows(path, pid)]


def probe_jsonl(path: Path, pid: str | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            if pid and pid not in line:
                continue
            try:
                rows.append(row_summary(json.loads(line)))
            except json.JSONDecodeError:
                continue
    return rows


def probe_api(api_dir: Path, url: str, product: str) -> dict[str, Any]:
    response = detail_api_response(api_dir, url)
    production_detail = detail_from_api_response(response, url, product)
    price_values = detail_price_values(response)
    return {
        "url": url,
        "product": product,
        "production_detail_summary": row_summary(production_detail),
        "production_price_values": {
            "final": price_text(price_values.get("final_sku_price")),
            "original": original_price_text(
                price_values.get("original_sku_price"),
                price_values.get("final_sku_price"),
            ),
            "savings": text_or_none(price_values.get("savings")),
            "calculated_savings": calc_savings(
                price_values.get("final_sku_price"),
                price_values.get("original_sku_price"),
            ),
            "mismatch": is_mismatch(
                price_values.get("final_sku_price"),
                price_values.get("original_sku_price"),
                price_values.get("savings"),
            ),
        },
        "price_slots": price_slot_summaries(response),
    }


def html_price_snippets(text: str) -> list[dict[str, Any]]:
    keys = ("finalPrice", '"fsp"', '"mrp"', "nepPrice", "Lowest price", "line-through")
    snippets: list[dict[str, Any]] = []
    for key in keys:
        for match in re.finditer(re.escape(key), text):
            start = max(0, match.start() - 220)
            end = min(len(text), match.end() + 320)
            snippet = re.sub(r"\s+", " ", text[start:end])
            snippets.append({"key": key, "snippet": snippet[:700]})
            if len([row for row in snippets if row["key"] == key]) >= 5:
                break
    return snippets


def probe_html(path: Path, url: str | None) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="replace")
    source_url = url or "https://www.flipkart.com/x/p/itm"
    parsed = detail_from_html(text, source_url)
    parsed_summary = {
        "final": price_text(parsed.get("final_sku_price")),
        "original": original_price_text(parsed.get("original_sku_price"), parsed.get("final_sku_price")),
        "savings": text_or_none(parsed.get("savings")),
        "calculated_savings": calc_savings(parsed.get("final_sku_price"), parsed.get("original_sku_price")),
    }
    return {
        "detail_from_html": parsed_summary,
        "snippets": html_price_snippets(text),
    }


def print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect FPKT price source candidates.")
    parser.add_argument("--file", help="CSV, JSONL, or saved HTML file.")
    parser.add_argument("--pid", help="PID/item to filter.")
    parser.add_argument("--url", help="Source product URL for saved HTML or live detail API probe.")
    parser.add_argument("--api-dir", help="FPKT API capture dir for live detail API probe.")
    parser.add_argument("--product", default="hhp", choices=["tv", "hhp", "ref", "ldy"])
    args = parser.parse_args()

    if args.api_dir and args.url:
        print_json(probe_api(Path(args.api_dir), args.url, args.product))
        return 0

    if not args.file:
        raise SystemExit("--file is required unless --api-dir and --url are provided")

    path = Path(args.file)
    if not path.exists():
        raise SystemExit(f"file not found: {path}")
    suffix = path.suffix.lower()
    if suffix == ".html":
        print_json(probe_html(path, args.url))
    elif suffix == ".jsonl":
        print_json(probe_jsonl(path, args.pid))
    elif suffix == ".csv":
        print_json(probe_csv(path, args.pid))
    else:
        raise SystemExit(f"unsupported file type: {path.suffix}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
