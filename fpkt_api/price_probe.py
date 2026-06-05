from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path
from typing import Any

from ops_test import (
    detail_from_html,
    detail_price_values,
    original_price_text,
    price_text,
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
    parser.add_argument("--file", required=True, help="CSV, JSONL, or saved HTML file.")
    parser.add_argument("--pid", help="PID/item to filter.")
    parser.add_argument("--url", help="Source product URL for saved HTML.")
    args = parser.parse_args()

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
