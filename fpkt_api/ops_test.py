"""Operation-like Flipkart API test without DB inserts.

This runner is intentionally isolated from the production crawler. It exercises
the direct API/structured-data path and saves files for inspection:

- main listing: collect first N unique products, first-rank-wins
- bsr listing: collect first N unique products, first-rank-wins
- detail: fetch product pages for a limited number of products and parse JSON-LD
- review: fetch review API pages for the same limited detail products
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import smtplib
import ssl
import subprocess
import sys
import urllib.request
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from lxml import html

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from main_probe import extract_products, request_headers
from phase_probe import (
    curl_headers,
    curl_url,
    default_api_dir,
    fetch_json,
    first_har_request_for_page,
    first_text,
    listing_page,
    normalize_text,
    page_fetch_curl_commands,
    product_ld,
    read_text,
    review_rows_from_response,
    split_curl_commands,
)
from siel_batch import next_batch_id
import siel_log


PRODUCT_QUERY = {
    "tv": "tv",
    "hhp": "smartphone",
    "ref": "refrigerator",
    "ldy": "washing+machine",
}

RETAIL_BASE_COLS = [
    "country", "product", "item", "sku", "account_name", "page_type",
    "retailer_sku_name", "product_url", "calendar_week", "crawl_datetime", "batch_id",
    "star_rating", "count_of_star_ratings", "count_of_reviews",
    "detailed_review_content", "retailer_sku_name_similar",
    "final_sku_price", "original_sku_price", "savings", "discount_type",
    "delivery_availability", "available_quantity_for_purchase",
    "sku_popularity", "sku_status", "main_rank", "bsr_rank",
]

PRODUCT_SPECIFIC_COLS = {
    "hhp": ["hhp_storage", "hhp_color", "trade_in"],
    "tv": ["screen_size", "model_year", "estimated_annual_electricity_use"],
    "ref": ["ref_refrigerator_type", "ref_capacity"],
    "ldy": ["ldy_loading_type", "ldy_capacity"],
}

AMAZON_PREVIEW_COLS = [
    "summarized_review_content", "fastest_delivery",
    "sku_assurance", "number_of_units_purchased_past_month",
]

RETAIL_COM_COLS_BY_PRODUCT = {
    product: RETAIL_BASE_COLS + PRODUCT_SPECIFIC_COLS[product] + AMAZON_PREVIEW_COLS
    for product in PRODUCT_QUERY
}

PRODUCT_LIST_COLS = [
    "country", "product", "item", "account_name", "page_type",
    "retailer_sku_name", "product_url", "calendar_week", "crawl_datetime", "batch_id",
    "star_rating", "count_of_star_ratings", "count_of_reviews",
    "final_sku_price", "original_sku_price", "savings", "discount_type",
    "available_quantity_for_purchase",
    "sku_popularity", "sku_status", "main_rank", "bsr_rank",
    "number_of_units_purchased_past_month",
]

PRODUCT_LIST_COLS_BY_PRODUCT = {product: PRODUCT_LIST_COLS for product in PRODUCT_QUERY}
DB_QUERY_VIEW_COLS_BY_PRODUCT = {
    product: RETAIL_BASE_COLS + PRODUCT_SPECIFIC_COLS[product]
    for product in PRODUCT_QUERY
}

TV_RETAIL_COM_COLS = RETAIL_COM_COLS_BY_PRODUCT["tv"]
TV_PRODUCT_LIST_COLS = PRODUCT_LIST_COLS_BY_PRODUCT["tv"]
DB_QUERY_VIEW_COLS = DB_QUERY_VIEW_COLS_BY_PRODUCT["tv"]


def safe_print(value: str) -> None:
    encoding = sys.stdout.encoding or "utf-8"
    print(value.encode(encoding, errors="replace").decode(encoding, errors="replace"), flush=True)


def ssl_context() -> ssl.SSLContext | None:
    if os.environ.get("FPKT_API_INSECURE_SSL", "").lower() in {"1", "true", "yes", "y"}:
        return ssl._create_unverified_context()
    return None


def csv_fieldnames(rows: list[dict[str, Any]]) -> list[str]:
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    return fields


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8-sig")
        return
    fields = csv_fieldnames(rows)
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def write_csv_fields(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in fields})


def to_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    match = re.search(r"\d[\d,]*", str(value))
    if not match:
        return None
    return int(match.group(0).replace(",", ""))


def text_or_none(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = normalize_text(str(value))
    return text or None


def count_text(value: Any) -> str | None:
    parsed = to_int(value)
    if parsed is None:
        return text_or_none(value)
    return f"{parsed:,}"


def best_count_text(*values: Any) -> str | None:
    parsed = [to_int(value) for value in values if value not in (None, "")]
    parsed = [value for value in parsed if value is not None]
    if parsed:
        return f"{max(parsed):,}"
    for value in values:
        text = text_or_none(value)
        if text:
            return text
    return None


def price_text(value: Any) -> str | None:
    parsed = to_int(value)
    if parsed is None:
        text = text_or_none(value)
        return text if not text or text.startswith("₹") else f"₹{text}"
    return f"₹{parsed:,}"


def original_price_text(original: Any, final: Any = None) -> str | None:
    original_value = to_int(original)
    final_value = to_int(final)
    if original_value is not None and final_value is not None and original_value == final_value:
        return None
    return price_text(original)


def sku_popularity_from_url(url: Any) -> str | None:
    url = str(url or "")
    labels = []
    if "spotlightTagId=default_BestsellerId" in url:
        labels.append("Bestseller")
    if "spotlightTagId=default_TrendingId" in url:
        labels.append("Trending")
    return ", ".join(labels) if labels else None


def now_crawl() -> datetime:
    return datetime.now()


def now_ist() -> datetime:
    return now_crawl()


def crawl_ts() -> str:
    return now_crawl().strftime("%Y-%m-%d %H:%M:%S")


def calendar_week(dt: datetime) -> str:
    return f"w{dt.isocalendar().week:02d}"


def test_batch_id(dt: datetime) -> str:
    return f"f_{dt.strftime('%Y%m%d')}_000000"


def schema_batch_id(dt: datetime, real_batch_id: bool) -> str:
    if real_batch_id:
        return next_batch_id("f", str(ROOT), dt)
    return test_batch_id(dt)


def output_dir(product: str) -> Path:
    stamp = now_crawl().strftime("%Y%m%d_%H%M%S")
    return Path(__file__).resolve().parent / "test_output" / f"ops_{product}_{stamp}"


def listing_until(
    api_dir: Path,
    stage: str,
    query: str,
    target_unique: int,
    max_pages: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    har_path = api_dir / "main_page2_page1_har.txt"
    if not har_path.exists():
        har_path = api_dir / "main_har.txt"
    sort = "popularity" if stage == "bsr" else None

    raw_rows: list[dict[str, Any]] = []
    final_rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    previous_response = None
    pages_used = 0
    html_meta_headers: dict[str, str] | None = None
    try:
        html_meta_headers = html_headers(api_dir)
    except Exception:
        html_meta_headers = None

    for page in range(1, max_pages + 1):
        pages_used = page
        response = listing_page(har_path, query, page, sort, previous_response)
        previous_response = response
        html_meta = listing_html_metadata_for_page(api_dir, query, stage, page, html_meta_headers)
        for row in extract_products(response, page=page):
            key = row_url_pid(row) or row.get("product_id") or row.get("item_id") or row.get("product_url")
            meta = html_meta.get(str(key or ""))
            api_discount = deal_label_from_text(str(row.get("discount_type") or ""))
            if meta:
                row["sku_status"] = row.get("sku_status") or meta.get("sku_status")
                row["sku_popularity"] = row.get("sku_popularity") or meta.get("sku_popularity")
                row["discount_type"] = meta.get("discount_type") or api_discount
            else:
                row["discount_type"] = api_discount
            raw = dict(row)
            raw["stage"] = stage
            raw["duplicate"] = bool(key in seen)
            raw["crawl_datetime"] = crawl_ts()
            raw_rows.append(raw)
            if key in seen:
                continue
            if key:
                seen.add(key)
            final = dict(raw)
            final["unique_rank"] = len(final_rows) + 1
            final["source_rank"] = final.get("rank")
            final[f"{stage}_rank"] = final["unique_rank"]
            final_rows.append(final)
            if len(final_rows) >= target_unique:
                return raw_rows, final_rows, pages_used
    return raw_rows, final_rows, pages_used


def html_headers(api_dir: Path) -> dict[str, str]:
    detail_curl = api_dir / "detail_curl.txt"
    if detail_curl.exists():
        for command in split_curl_commands(read_text(detail_curl)):
            url = curl_url(command) or ""
            if "www.flipkart.com/" in url and "/p/" in url:
                headers = curl_headers(command)
                headers.pop("Content-Type", None)
                headers.pop("content-type", None)
                headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
                headers.setdefault("Referer", "https://www.flipkart.com/")
                return headers

    har_path = api_dir / "main_page2_page1_har.txt"
    if not har_path.exists():
        har_path = api_dir / "main_har.txt"
    headers = request_headers(first_har_request_for_page(har_path, 1))
    headers.pop("Content-Type", None)
    headers.pop("content-type", None)
    headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    headers.setdefault("Referer", "https://www.flipkart.com/")
    return headers


def fetch_text(url: str, headers: dict[str, str]) -> str:
    request = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(request, timeout=40, context=ssl_context()) as response:
        return response.read().decode("utf-8", errors="replace")


def listing_html_url(query: str, stage: str, page: int) -> str:
    url = (
        f"https://www.flipkart.com/search?q={query}&otracker=search&otracker1=search"
        "&marketplace=FLIPKART&as-show=on&as=off"
    )
    if stage == "bsr":
        url += "&sort=popularity"
    if page > 1:
        url += f"&page={page}"
    return url


def spotlight_label_from_href(href: str) -> str | None:
    match = re.search(r"spotlightTagId=default_([^&_]+)", href or "")
    if not match:
        return None
    raw = match.group(1).strip()
    known = {
        "BestsellerId": "Bestseller",
        "TrendingId": "Trending",
    }
    return known.get(raw, re.sub(r"Id$", "", raw))


def deal_label_from_text(text: str) -> str | None:
    label = normalize_text(text)
    if not label or len(label) > 50:
        return None
    lowered = label.lower()
    if lowered in {"upto", "bank offer", "bank offers"}:
        return None
    if "bank offer" in lowered or "exchange" in lowered:
        return None
    if "only" in lowered and "left" in lowered:
        return None
    if "deal" in lowered or "special price" in lowered or "lowest price" in lowered:
        return label
    return None


def listing_html_metadata(text: str) -> dict[str, dict[str, str | None]]:
    try:
        doc = html.fromstring(text)
    except Exception:
        return {}

    metadata: dict[str, dict[str, str | None]] = {}
    for card in doc.xpath('//*[@data-id]'):
        product_id = (card.get("data-id") or "").strip()
        if not product_id:
            continue

        labels: list[str] = []
        for href in card.xpath('.//a[contains(@href,"spotlightTagId=")]/@href'):
            label = spotlight_label_from_href(href)
            if label and label not in labels:
                labels.append(label)
        for text in card.xpath('.//*[contains(@class,"o2uEoz")]//text()'):
            label = normalize_text(text)
            if label and label.lower() not in {"sponsored", "flipkart assured"} and label not in labels:
                labels.append(label)

        deal_labels: list[str] = []
        deal_badges = card.xpath(
            './/*[contains(concat(" ", normalize-space(@class), " "), " HZ0E6r ") '
            'and contains(concat(" ", normalize-space(@class), " "), " Rm9_cy ")]//text()'
        )
        for text in deal_badges:
            deal_label = deal_label_from_text(text)
            if deal_label and deal_label not in deal_labels:
                deal_labels.append(deal_label)

        sponsored = bool(card.xpath('.//*[contains(concat(" ", normalize-space(@class), " "), " t7gRps ")]'))
        if not sponsored:
            for text in card.xpath(".//text()"):
                if normalize_text(text).strip().lower() == "sponsored":
                    sponsored = True
                    break
        if not sponsored:
            for attr in card.xpath(".//@aria-label | .//@title | .//@alt"):
                if normalize_text(attr).strip().lower() == "sponsored":
                    sponsored = True
                    break
        metadata[product_id] = {
            "sku_status": "Sponsored" if sponsored else None,
            "sku_popularity": ", ".join(labels) if labels else None,
            "discount_type": ", ".join(deal_labels) if deal_labels else None,
        }
    return metadata


def listing_html_metadata_for_page(
    api_dir: Path,
    query: str,
    stage: str,
    page: int,
    headers: dict[str, str] | None,
) -> dict[str, dict[str, str | None]]:
    text: str | None = None
    if headers:
        try:
            text = fetch_text(listing_html_url(query, stage, page), headers)
        except Exception:
            text = None

    if not text and stage == "main" and page == 1:
        saved_html = api_dir / "main_html.txt"
        if saved_html.exists():
            text = read_text(saved_html)

    return listing_html_metadata(text) if text else {}


def same_pid(source_url: str, href: str) -> bool:
    source_pid = re.search(r"[?&]pid=([A-Z0-9]+)", source_url or "")
    href_pid = re.search(r"[?&]pid=([A-Z0-9]+)", href or "")
    return bool(source_pid and href_pid and source_pid.group(1) == href_pid.group(1))


def fallback_review_url(product_url: str) -> str | None:
    parsed = urlparse(product_url)
    if "/p/" not in parsed.path:
        return None
    path = parsed.path.replace("/p/", "/product-reviews/", 1)
    return parsed._replace(path=path).geturl()


def detail_from_html(text: str, source_url: str) -> dict[str, Any]:
    doc = html.fromstring(text)
    ld = product_ld(doc)
    rating = ld.get("aggregateRating") or {}
    offers = ld.get("offers") or {}
    anchors = doc.xpath(
        '//a[contains(@href,"/product-reviews/") and not(contains(@href,"buynow")) '
        'and not(contains(@href,"&an="))]/@href'
    )
    review_url = None
    for href in anchors:
        absolute = urljoin("https://www.flipkart.com", href)
        if same_pid(source_url, absolute):
            review_url = absolute
            break
    if not review_url and anchors:
        review_url = urljoin("https://www.flipkart.com", anchors[0])
    if not review_url:
        review_url = fallback_review_url(source_url)

    return {
        "source_url": source_url,
        "retailer_sku_name": ld.get("name") or first_text(doc, "string(//h1[1])"),
        "fsn": url_pid(source_url) or ld.get("sku"),
        "brand": ((ld.get("brand") or {}).get("name") if isinstance(ld.get("brand"), dict) else None),
        "final_sku_price": offers.get("price"),
        "original_sku_price": first_text(doc, '(//div[contains(@style,"line-through")])[1]'),
        "savings": first_text(
            doc,
            '(//div[contains(text(),"%") and string-length(normalize-space(text()))<=5 '
            'and not(ancestor::a[contains(@href,"/p/")])])[1]',
        ),
        "discount_type": None,
        "availability": offers.get("availability"),
        "star_rating": rating.get("ratingValue"),
        "count_of_star_ratings": rating.get("ratingCount"),
        "count_of_reviews": rating.get("reviewCount"),
        "sku": first_text(
            doc,
            '//div[normalize-space(text())="Model Name"]/following-sibling::div[1]',
        ),
        "screen_size": first_text(
            doc,
            '//div[normalize-space(text())="Display Size"]/following-sibling::div[1] | '
            '//div[normalize-space(text())="Display Size:"]/following-sibling::div[1]',
        ),
        "model_year": first_text(
            doc,
            '//div[normalize-space(text())="Launch Year" or normalize-space(text())="Launch Year:"]'
            "/following-sibling::div[1]",
        ),
        "estimated_annual_electricity_use": first_text(
            doc,
            '//div[normalize-space(text())="Power Consumption" or '
            'normalize-space(text())="Annual Energy Consumption" or '
            'normalize-space(text())="Energy Consumption" or '
            'normalize-space(text())="Power Consumption:" or '
            'normalize-space(text())="Annual Energy Consumption:"]'
            '/following-sibling::div[1]',
        ),
        "review_url": review_url,
        "jsonld_review_count": len(ld.get("review") or []),
    }


def review_uri(review_url: str) -> str:
    parsed = urlparse(review_url)
    uri = parsed.path
    if parsed.query:
        uri += "?" + parsed.query
    return uri


def page_uri(product_url: str) -> str:
    parsed = urlparse(product_url)
    uri = parsed.path
    if parsed.query:
        uri += "?" + parsed.query
    return uri


def json_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, dict):
        if "text" in value:
            return json_text(value.get("text"))
        return None
    if isinstance(value, list):
        parts = [json_text(item) for item in value]
        parts = [part for part in parts if part]
        return " ".join(parts) if parts else None
    return normalize_text(str(value))


def iter_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from iter_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_dicts(child)


def json_texts(value: Any) -> list[str]:
    found: list[str] = []
    for item in iter_dicts(value):
        text = None
        if isinstance(item.get("value"), dict) and "text" in item["value"]:
            text = json_text(item["value"])
        elif isinstance(item.get("value"), str):
            text = json_text(item.get("value"))
        elif isinstance(item.get("text"), str):
            text = json_text(item.get("text"))
        if text and text not in found:
            found.append(text)
    return found


def scalar_texts(value: Any) -> list[str]:
    found: list[str] = []

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)
        elif item not in (None, ""):
            text = normalize_text(str(item))
            if text and text not in found:
                found.append(text)

    visit(value)
    return found


def rupee_amounts_from_text(value: Any, allow_plain: bool = False) -> list[int]:
    text = normalize_text(str(value or ""))
    if not text:
        return []
    patterns = [
        r"(?:\u20b9|Rs\.?|INR)\s*([0-9][0-9,]*(?:\.\d+)?)",
    ]
    if allow_plain:
        patterns.append(r"\b([0-9]{1,3}(?:,[0-9]{3})+(?:\.\d+)?)\b")
    amounts: list[int] = []
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.I):
            try:
                amount = int(float(match.group(1).replace(",", "")))
            except ValueError:
                continue
            if amount >= 1000 and amount not in amounts:
                amounts.append(amount)
    return amounts


def percent_text_from_text(value: Any) -> str | None:
    text = normalize_text(str(value or ""))
    match = re.search(r"\b([0-9]{1,2}(?:\.\d+)?)\s*%\s*(?:off)?", text, re.I)
    if not match:
        return None
    try:
        number = float(match.group(1))
    except ValueError:
        return None
    return f"{int(number)}%" if number == int(number) else f"{number:g}%"


def detail_price_from_keyed_fields(slot: dict[str, Any]) -> dict[str, int | str | None]:
    final_keys = {
        "finalprice", "final_price", "sellingprice", "selling_price", "specialprice",
        "special_price", "currentprice", "current_price", "discountedprice", "discounted_price",
    }
    original_keys = {"mrp", "originalprice", "original_price", "listprice", "list_price"}
    final_price = None
    original_price = None
    savings = None
    for item in iter_dicts(slot):
        for key, value in item.items():
            lowered = str(key).replace("-", "_").lower()
            compact = lowered.replace("_", "")
            amounts = []
            if isinstance(value, (int, float)):
                amounts = [int(value)]
            else:
                amounts = rupee_amounts_from_text(value, allow_plain=True)
                if not amounts and isinstance(value, dict):
                    for text in scalar_texts(value):
                        amounts.extend(rupee_amounts_from_text(text, allow_plain=True))
            if compact in final_keys and amounts and final_price is None:
                final_price = amounts[0]
            elif compact in original_keys and amounts and original_price is None:
                original_price = amounts[0]
            if savings is None and ("discount" in compact or "saving" in compact):
                savings = percent_text_from_text(value)
        if final_price is not None and original_price is not None and savings:
            break
    return {
        "final_sku_price": final_price,
        "original_sku_price": original_price,
        "savings": savings,
    }


def detail_price_from_pricing_texts(slot: dict[str, Any]) -> dict[str, int | str | None]:
    top_texts: list[str] = []
    for text in scalar_texts(slot):
        lowered = text.lower()
        if any(stop in lowered for stop in (
            "apply offers",
            "bank offers",
            "exchange offer",
            "change pincode",
            "view emi offers",
        )):
            break
        if any(skip in lowered for skip in (
            "lowest price",
            "protect promise",
            "no cost emi",
            "emi",
            "month",
            "x ",
            "pay ",
            "exchange",
            "bank offer",
            "coupon",
            "cashback",
        )):
            continue
        top_texts.append(text)

    amounts: list[int] = []
    savings = None
    for text in top_texts:
        savings = savings or percent_text_from_text(text)
        for amount in rupee_amounts_from_text(text, allow_plain=True):
            if amount not in amounts:
                amounts.append(amount)

    if not amounts:
        return {"final_sku_price": None, "original_sku_price": None, "savings": savings}
    if savings and len(amounts) >= 2:
        final_price = min(amounts)
        original_price = max(amounts)
    else:
        final_price = amounts[0]
        original_price = next((amount for amount in amounts[1:] if amount > final_price), None)
    return {
        "final_sku_price": final_price,
        "original_sku_price": original_price,
        "savings": savings,
    }


def detail_price_values(response: dict[str, Any]) -> dict[str, int | str | None]:
    slots = (response.get("RESPONSE") or {}).get("slots") or []
    price_slots: list[dict[str, Any]] = []
    fallback_slots: list[dict[str, Any]] = []
    for slot in slots:
        widget = slot.get("widget") or {}
        if widget.get("viewType") == "pp_pricing_price_summary":
            price_slots.append(slot)
        elif widget.get("type") == "ATLAS_NEP_V2":
            fallback_slots.append(slot)

    for slot in price_slots + fallback_slots:
        keyed = detail_price_from_keyed_fields(slot)
        text_based = detail_price_from_pricing_texts(slot)
        final_price = keyed.get("final_sku_price") or text_based.get("final_sku_price")
        if final_price:
            return {
                "final_sku_price": final_price,
                "original_sku_price": keyed.get("original_sku_price") or text_based.get("original_sku_price"),
                "savings": keyed.get("savings") or text_based.get("savings"),
            }
    return {"final_sku_price": None, "original_sku_price": None, "savings": None}


def first_json_key(value: Any, key: str) -> str | None:
    for item in iter_dicts(value):
        if key in item:
            text = json_text(item.get(key))
            if text:
                return text
    return None


def json_label_value(value: Any, label: str) -> str | None:
    wanted = label.strip().lower().rstrip(":")
    for item in iter_dicts(value):
        label_text = json_text(((item.get("label_0") or {}).get("value") if isinstance(item.get("label_0"), dict) else None))
        if not label_text or label_text.strip().lower().rstrip(":") != wanted:
            continue
        for key in ("label_2", "label_1"):
            candidate = item.get(key)
            if isinstance(candidate, dict):
                text = json_text(candidate.get("value"))
                if text and text.strip().lower().rstrip(":") != wanted:
                    return text
    return None


def json_label_values(value: Any, label: str) -> list[str]:
    wanted = label.strip().lower().rstrip(":")
    found: list[str] = []
    for item in iter_dicts(value):
        label_text = json_text(((item.get("label_0") or {}).get("value") if isinstance(item.get("label_0"), dict) else None))
        if not label_text or label_text.strip().lower().rstrip(":") != wanted:
            continue
        for key in ("label_2", "label_1"):
            candidate = item.get(key)
            if isinstance(candidate, dict):
                text = json_text(candidate.get("value"))
                if text and text.strip().lower().rstrip(":") != wanted and text not in found:
                    found.append(text)
    return found


SPEC_LABEL_VIEW_TYPES = {
    "rpd_all_details_showcase_vertical_list_layout",
    "rpd_all_details_showcase_horizontal_list_layout",
    "size-selector-view-wrapper",
    "variant-selector-image-view-wrapper",
    "product-highlights-layout",
}


def detail_spec_slots(response: dict[str, Any]) -> list[dict[str, Any]]:
    slots = (response.get("RESPONSE") or {}).get("slots") or []
    scoped: list[dict[str, Any]] = []
    for slot in slots:
        widget = slot.get("widget") or {}
        if widget.get("viewType") in SPEC_LABEL_VIEW_TYPES:
            scoped.append(slot)
    return scoped


def detail_label_values(response: dict[str, Any], label: str) -> list[str]:
    found: list[str] = []
    for slot in detail_spec_slots(response):
        for value in json_label_values(slot, label):
            if value not in found:
                found.append(value)
    return found


def detail_label_value(response: dict[str, Any], label: str) -> str | None:
    values = detail_label_values(response, label)
    return values[0] if values else None


def detail_spec_texts(response: dict[str, Any]) -> list[str]:
    found: list[str] = []
    for slot in detail_spec_slots(response):
        for text in json_texts(slot):
            if text not in found:
                found.append(text)
    return found


def screen_size_value(response: dict[str, Any]) -> str | None:
    values = detail_label_values(response, "Display Size")
    for value in values:
        if re.search(r"\bcm\b", value, re.I) and re.search(r"\binch\b", value, re.I):
            return value
    return values[0] if values else None


def product_sku_value(response: dict[str, Any], product: str) -> str | None:
    product = product.lower()
    if product == "hhp":
        return detail_label_value(response, "Model Number") or first_json_key(response, "prependingText")
    return detail_label_value(response, "Model Name")


def hhp_storage_value(response: dict[str, Any]) -> str | None:
    for label in ("Internal Storage", "Storage"):
        for text in detail_label_values(response, label):
            match = re.search(r"\b(\d+(?:\.\d+)?)\s*(GB|TB)\b", text, re.I)
            if match:
                return f"{match.group(1)} {match.group(2).upper()}"
    for text in detail_spec_texts(response):
        storage = siel_log.parse_hhp_storage(text)
        if storage:
            return storage
    return None


def exchange_label(text: Any) -> bool:
    lowered = normalize_text(str(text or "")).lower()
    return (
        "exchange offer" in lowered
        or "with exchange" in lowered
        or "on exchange" in lowered
        or "off on exchange" in lowered
        or "off exchange" in lowered
        or "off with exchange" in lowered
        or "off_exchange" in lowered
        or "offerdetails:exchange" in lowered
    )


def trade_in_candidate(text: Any) -> str | None:
    candidate = normalize_text(str(text or ""))
    if not candidate:
        return None
    lowered = candidate.lower()
    if "pincode" in lowered or "service" in lowered or "bank offer" in lowered:
        return None
    has_offer_value = (
        "up to" in lowered
        or "upto" in lowered
        or re.search(r"\boff\b", lowered)
        or "\u20b9" in candidate
    )
    if not has_offer_value:
        return None
    return normalize_trade_in_value(candidate)


def normalize_trade_in_value(value: Any) -> str | None:
    text = normalize_text(str(value or ""))
    if not text:
        return None

    amount_match = (
        re.search(r"(?:\u20b9|Rs\.?|INR)\s*([0-9][0-9,]*(?:\.\d+)?)", text, re.I)
        or re.search(r"\b([0-9]{1,3}(?:,[0-9]{3})+(?:\.\d+)?)\b", text)
        or re.search(r"\b([0-9]{4,})(?:\.\d+)?\b", text)
    )
    if not amount_match:
        return None

    try:
        amount = int(float(amount_match.group(1).replace(",", "")))
    except ValueError:
        return None
    if amount <= 0:
        return None
    return f"Up to \u20b9{amount:,}"


def hhp_trade_in_value(response: dict[str, Any]) -> str | None:
    text_groups: list[list[str]] = []
    slots = (response.get("RESPONSE") or {}).get("slots") or []
    for slot in slots:
        widget = slot.get("widget") or {}
        if widget.get("type") != "ATLAS_NEP_V2" and widget.get("viewType") != "pp_pricing_price_summary":
            continue
        for item in iter_dicts(slot):
            texts = scalar_texts(item)
            if len(texts) <= 60 and any(exchange_label(text) for text in texts):
                text_groups.append(texts)

    for texts in text_groups:
        for index, text in enumerate(texts):
            if not exchange_label(text):
                continue
            for candidate in texts[index:index + 10]:
                trade_in = trade_in_candidate(candidate)
                if trade_in:
                    return trade_in
    return None


def ref_capacity_value(response: dict[str, Any]) -> str | None:
    for value in detail_label_values(response, "Capacity"):
        if re.search(r"\b\d+(?:\.\d+)?\s*(?:l|litre|liter)s?\b", value, re.I):
            return value
    return None


def ldy_capacity_value(response: dict[str, Any]) -> str | None:
    for label in ("Washing Capacity", "Capacity"):
        for value in detail_label_values(response, label):
            capacity = siel_log.parse_ldy_capacity(value)
            if capacity:
                return capacity
    return None


def ldy_loading_type_value(response: dict[str, Any]) -> str | None:
    return siel_log.parse_ldy_loading_type(
        detail_label_value(response, "Function Type") or detail_label_value(response, "Loading Type")
    )


def product_detail_values(response: dict[str, Any], product: str) -> dict[str, str | None]:
    product = product.lower()
    if product == "tv":
        return {
            "screen_size": screen_size_value(response),
            "model_year": detail_label_value(response, "Launch Year"),
            "estimated_annual_electricity_use": (
                detail_label_value(response, "Power Consumption")
                or detail_label_value(response, "Annual Energy Consumption")
                or detail_label_value(response, "Energy Consumption")
            ),
        }
    if product == "hhp":
        return {
            "hhp_storage": hhp_storage_value(response),
            "hhp_color": detail_label_value(response, "Color") or detail_label_value(response, "Selected Color"),
            "trade_in": hhp_trade_in_value(response),
        }
    if product == "ref":
        return {
            "ref_refrigerator_type": detail_label_value(response, "Refrigerator Type"),
            "ref_capacity": ref_capacity_value(response),
        }
    if product == "ldy":
        return {
            "ldy_loading_type": ldy_loading_type_value(response),
            "ldy_capacity": ldy_capacity_value(response),
        }
    return {}


def delivery_availability_value(response: dict[str, Any]) -> str | None:
    def has_delivery_date(value: str) -> bool:
        return bool(re.search(
            r"(\b\d{1,2}\s+[A-Za-z]{3,9},\s*(mon|tue|wed|thu|fri|sat|sun|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b"
            r"|\b(mon|tue|wed|thu|fri|sat|sun|monday|tuesday|wednesday|thursday|friday|saturday|sunday),\s*\d{1,2}\s+[A-Za-z]{3,9}\b)",
            value,
            re.I,
        ))

    def normalize_delivery(value: str) -> str | None:
        value = normalize_text(value)
        if not value:
            return None
        match = re.search(r"\bDelivery\s+by\s+(.+)$", value, re.I)
        if match and has_delivery_date(match.group(1)):
            return f"Delivery by {match.group(1).strip()}"
        match = re.search(r"^by\s+(.+)$", value, re.I)
        if match and has_delivery_date(match.group(1)):
            return f"Delivery by {match.group(1).strip()}"
        if has_delivery_date(value):
            return f"Delivery by {value.strip()}"
        return None

    def find_delivery(texts: list[str]) -> str | None:
        for index, text in enumerate(texts):
            stripped = normalize_text(text)
            lowered = stripped.lower()
            if lowered in {"delivery", "delivery by"}:
                for candidate in texts[index + 1:index + 4]:
                    delivery = normalize_delivery(candidate)
                    if delivery:
                        return delivery
            delivery = normalize_delivery(stripped)
            if delivery:
                return delivery
        return None

    delivery_view_types = {"pp_delivery_widget_v2", "default_fk_pp_delivery_widget"}
    slots = (response.get("RESPONSE") or {}).get("slots") or []
    widget_texts: list[str] = []
    for slot in slots:
        widget = slot.get("widget") or {}
        if widget.get("viewType") in delivery_view_types:
            widget_texts.extend(json_texts(slot))
    return find_delivery(widget_texts)


def similar_product_names(response: dict[str, Any]) -> str | None:
    names: list[str] = []
    slots = (response.get("RESPONSE") or {}).get("slots") or []
    for slot in slots:
        widget = slot.get("widget") or {}
        if widget.get("viewType") != "pp_reco_pmu_horizontal_scrollable_ads":
            continue
        header = None
        for text in json_texts((widget.get("data") or {}).get("dlsData", {}).get("hp_reco_header_0")):
            if text:
                header = text
                break
        if not header or header.strip().lower() != "similar products":
            continue
        dls_data = (widget.get("data") or {}).get("dlsData") or {}
        cards = ((dls_data.get("MRCSV_0") or {}).get("value") or [])
        for card in cards:
            card_value = ((card.get("value") or {}).get("hp_reco_product-card_0") or {}).get("value")
            if not isinstance(card_value, dict):
                continue
            name = json_text(((card_value.get("label_2") or {}).get("value") if isinstance(card_value.get("label_2"), dict) else None))
            if name and name not in names:
                names.append(name)
    return " ||| ".join(names) if names else None


def detail_api_response(api_dir: Path, product_url: str) -> dict[str, Any]:
    commands = page_fetch_curl_commands(api_dir / "detail_curl.txt")
    if not commands:
        raise ValueError("detail_curl.txt has no page/fetch request")
    _index, url, raw_body, headers = commands[0]
    body = json.loads(raw_body)
    body["pageUri"] = page_uri(product_url)
    context = body.setdefault("pageContext", {})
    context["pageNumber"] = 1
    context["paginatedFetch"] = False
    context["slotContextMap"] = {}
    context["paginationContextMap"] = {}
    return fetch_json(url, headers, body)


def detail_from_api_response(response: dict[str, Any], source_url: str, product: str = "tv") -> dict[str, Any]:
    texts = json_texts(response)
    price_values = detail_price_values(response)
    rating_value = None
    rating_count = None
    for item in iter_dicts(response):
        if item.get("rating") is not None and item.get("reviewText") is not None:
            rating_value = item.get("rating")
            rating_count = to_int(item.get("reviewText"))
            break
    review_count = None
    for text in texts:
        match = re.search(r"([\d,]+)\s+ratings?\s+and\s+([\d,]+)\s+reviews?", text, re.I)
        if match:
            rating_count = to_int(match.group(1))
            review_count = to_int(match.group(2))
            break

    detail = {
        "source_url": source_url,
        "retailer_sku_name": first_json_key(response, "prependingText"),
        "fsn": url_pid(source_url),
        "final_sku_price": price_values.get("final_sku_price"),
        "original_sku_price": price_values.get("original_sku_price"),
        "savings": price_values.get("savings"),
        "discount_type": None,
        "availability": None,
        "star_rating": rating_value,
        "count_of_star_ratings": rating_count,
        "count_of_reviews": review_count,
        "sku": product_sku_value(response, product),
        "delivery_availability": delivery_availability_value(response),
        "retailer_sku_name_similar": similar_product_names(response),
        "review_url": fallback_review_url(source_url),
        "jsonld_review_count": None,
    }
    detail.update(product_detail_values(response, product))
    return detail


def detail_from_api_with_retries(
    api_dir: Path,
    product_url: str,
    product: str,
    retries: int,
    label: str,
) -> tuple[dict[str, Any], list[str]]:
    messages: list[str] = []
    last_exc: Exception | None = None
    for attempt in range(0, max(retries, 0) + 1):
        try:
            detail = detail_from_api_response(
                detail_api_response(api_dir, product_url),
                product_url,
                product,
            )
            if attempt > 0:
                messages.append(f"[detail_retry] {label} attempt={attempt}/{retries} recovered")
            return detail, messages
        except Exception as exc:
            last_exc = exc
            if attempt < max(retries, 0):
                messages.append(f"[detail_retry] {label} attempt={attempt + 1}/{retries} error={repr(exc)}")
                continue
            break
    if last_exc:
        raise last_exc
    raise RuntimeError(f"detail retry failed without exception: {label}")


def review_for_product(
    api_dir: Path,
    review_url: str,
    pages: int,
    max_reviews: int,
) -> list[dict[str, Any]]:
    commands = page_fetch_curl_commands(api_dir / "review_curl.txt")
    if not commands:
        raise ValueError("review_curl.txt has no page/fetch request")
    _index, url, raw_body, headers = commands[0]
    base_body = json.loads(raw_body)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for page in range(1, pages + 1):
        body = json.loads(json.dumps(base_body))
        body["pageUri"] = review_uri(review_url)
        context = body.setdefault("pageContext", {})
        context["pageNumber"] = page
        context["paginatedFetch"] = page > 1
        context["paginationContextMap"] = {}
        context.pop("pageHashKey", None)
        response = fetch_json(url, headers, body)
        for row in review_rows_from_response(response):
            if len(rows) >= max_reviews:
                return rows
            row["review_url"] = review_url
            row["page"] = page
            row["duplicate"] = bool(row.get("id") in seen)
            if row.get("id"):
                seen.add(row["id"])
            rows.append(row)
    return rows


def review_for_product_with_retries(
    api_dir: Path,
    review_url: str,
    pages: int,
    short_max_pages: int,
    max_reviews: int,
    expected_reviews: int,
    retries: int,
    label: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    rows = review_for_product(api_dir, review_url, pages, max_reviews)
    expected = min(expected_reviews, max_reviews) if max_reviews > 0 else expected_reviews
    messages: list[str] = []
    if expected <= 0 or len(rows) >= expected:
        return rows, messages

    best_rows = rows
    retry_pages = pages
    if short_max_pages > pages:
        try:
            expanded_rows = review_for_product(api_dir, review_url, short_max_pages, max_reviews)
        except Exception as exc:
            messages.append(
                f"[review_retry] {label} expand_pages={pages}->{short_max_pages} "
                f"error={repr(exc)} best={len(best_rows)} expected={expected}"
            )
        else:
            retry_pages = short_max_pages
            if len(expanded_rows) > len(best_rows):
                messages.append(
                    f"[review_retry] {label} expand_pages={pages}->{short_max_pages} "
                    f"recovered {len(best_rows)}->{len(expanded_rows)} expected={expected}"
                )
                best_rows = expanded_rows
            else:
                messages.append(
                    f"[review_retry] {label} expand_pages={pages}->{short_max_pages} "
                    f"rows={len(expanded_rows)} best={len(best_rows)} expected={expected}"
                )

    for attempt in range(1, max(retries, 0) + 1):
        if len(best_rows) >= expected:
            break
        try:
            retry_rows = review_for_product(api_dir, review_url, retry_pages, max_reviews)
        except Exception as exc:
            messages.append(
                f"[review_retry] {label} attempt={attempt}/{retries} "
                f"error={repr(exc)} best={len(best_rows)} expected={expected}"
            )
            continue
        if len(retry_rows) > len(best_rows):
            messages.append(
                f"[review_retry] {label} attempt={attempt}/{retries} "
                f"recovered {len(best_rows)}->{len(retry_rows)} expected={expected}"
            )
            best_rows = retry_rows
        else:
            messages.append(
                f"[review_retry] {label} attempt={attempt}/{retries} "
                f"rows={len(retry_rows)} best={len(best_rows)} expected={expected}"
            )
    return best_rows, messages


def detail_targets(main_rows: list[dict[str, Any]], bsr_rows: list[dict[str, Any]], max_detail: int) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    seen: set[str] = set()
    limit = None if max_detail == 0 else max_detail
    for row in main_rows + bsr_rows:
        key = row.get("product_id") or row.get("product_url")
        if not key or key in seen or not row.get("product_url"):
            continue
        seen.add(key)
        targets.append(row)
        if limit is not None and len(targets) >= limit:
            break
    return targets


def listing_schema_record(
    row: dict[str, Any],
    product: str,
    stage: str,
    crawl_dt: str,
    batch_id: str,
) -> dict[str, Any]:
    rank_field = "bsr_rank" if stage == "bsr" else "main_rank"
    rank = row.get(rank_field) or row.get("rank")
    fsn = row_url_pid(row) or row.get("product_id") or row.get("item_id")
    row_crawl_dt = row.get("crawl_datetime") or crawl_dt
    return {
        "account_name": "flipkart",
        "product": product,
        "stage": stage,
        "page_no": row.get("page"),
        rank_field: rank,
        "fsn": fsn,
        "item": fsn,
        "retailer_sku_name": row.get("product_name"),
        "brand": row.get("brand"),
        "product_url": row.get("product_url"),
        "final_sku_price": price_text(row.get("final_price")),
        "original_sku_price": original_price_text(row.get("original_price"), row.get("final_price")),
        "savings": text_or_none(row.get("savings")),
        "discount_type": text_or_none(row.get("discount_type")),
        "star_rating": text_or_none(row.get("star_rating")),
        "count_of_star_ratings": count_text(row.get("count_of_star_ratings")),
        "count_of_reviews": count_text(row.get("count_of_reviews")),
        "sku_popularity": row.get("sku_popularity") or sku_popularity_from_url(row.get("product_url")),
        "sku_status": row.get("sku_status"),
        "available_quantity_for_purchase": row.get("available_quantity_for_purchase"),
        "batch_id": batch_id,
        "crawl_datetime": row_crawl_dt,
    }


def detail_schema_record(
    detail: dict[str, Any],
    product: str,
    crawl_dt: str,
    batch_id: str,
    detailed_review_content: str | None,
) -> dict[str, Any]:
    fsn = row_url_pid(detail) or detail.get("fsn") or detail.get("product_id")
    detail_crawl_dt = detail.get("crawl_datetime") or crawl_dt
    record = {
        "account_name": "flipkart",
        "product": product,
        "stage": "detail",
        "source_url": detail.get("source_url"),
        "product_url": detail.get("source_url"),
        "fsn": fsn,
        "item": fsn,
        "sku": detail.get("sku"),
        "retailer_sku_name": detail.get("retailer_sku_name"),
        "final_sku_price": price_text(detail.get("final_sku_price")),
        "original_sku_price": original_price_text(detail.get("original_sku_price"), detail.get("final_sku_price")),
        "savings": text_or_none(detail.get("savings")),
        "discount_type": text_or_none(detail.get("discount_type")),
        "retailer_sku_name_similar": text_or_none(detail.get("retailer_sku_name_similar")),
        "star_rating": text_or_none(detail.get("star_rating")),
        "count_of_star_ratings": count_text(detail.get("count_of_star_ratings")),
        "count_of_reviews": count_text(detail.get("count_of_reviews")),
        "detailed_review_content": detailed_review_content,
        "delivery_availability": text_or_none(detail.get("delivery_availability")),
        "batch_id": batch_id,
        "crawl_datetime": detail_crawl_dt,
    }
    for field in PRODUCT_SPECIFIC_COLS[product.lower()]:
        record[field] = text_or_none(detail.get(field))
    return record


def format_detailed_review_content(rows: list[dict[str, Any]]) -> str | None:
    parts = [normalize_text(str(row.get("text") or "")) for row in rows]
    parts = [part for part in parts if part]
    if not parts:
        return None
    return " ||| ".join(f"review{idx + 1} - {text}" for idx, text in enumerate(parts[:20]))


def url_pid(value: Any) -> str | None:
    match = re.search(r"[?&]pid=([A-Z0-9]+)", str(value or ""))
    return match.group(1) if match else None


def row_url_pid(row: dict[str, Any]) -> str | None:
    return (
        url_pid(row.get("product_url"))
        or url_pid(row.get("source_url"))
        or url_pid(row.get("review_url"))
        or url_pid(row.get("url"))
    )


def review_content_count(value: Any) -> int:
    text = str(value or "")
    if not text:
        return 0
    return len(re.findall(r"(?:^| \|\|\| )review\d+\s+-", text))


def qa_issue(check: str, item: Any, message: str, **extra: Any) -> dict[str, Any]:
    row = {"check": check, "item": item, "message": message}
    row.update(extra)
    return row


REQUIRED_DETAIL_FIELDS_BY_PRODUCT = {
    "hhp": ["sku", "hhp_storage", "hhp_color"],
    "tv": ["sku", "screen_size", "model_year"],
    "ref": ["sku", "ref_refrigerator_type", "ref_capacity"],
    "ldy": ["sku", "ldy_capacity"],
}


def validate_schema_outputs(
    product: str,
    retail_rows: list[dict[str, Any]],
    reviews: list[dict[str, Any]],
    max_reviews_per_product: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    product = product.lower()
    issues: list[dict[str, Any]] = []
    item_counts: dict[str, int] = {}
    review_counts: dict[str, int] = {}

    for row in retail_rows:
        item = row.get("item")
        if item:
            item_counts[item] = item_counts.get(item, 0) + 1
    for row in reviews:
        item = key_for_row(row)
        if item:
            review_counts[item] = review_counts.get(item, 0) + 1

    for item, count in item_counts.items():
        if count > 1:
            issues.append(qa_issue("duplicate_item", item, f"item appears {count} times"))

    for row in retail_rows:
        item = row.get("item")
        pid = url_pid(row.get("product_url"))
        if pid and item and pid != item:
            issues.append(qa_issue("pid_mismatch", item, f"url pid is {pid}"))

        fsp = to_int(row.get("final_sku_price"))
        osp = to_int(row.get("original_sku_price"))
        savings = to_int(row.get("savings"))
        if fsp is not None and osp is not None and fsp > osp:
            issues.append(qa_issue("price_inversion", item, "final_sku_price is greater than original_sku_price",
                                   final_sku_price=fsp, original_sku_price=osp))
        if fsp is not None and osp not in (None, 0) and savings is not None:
            calculated = (osp - fsp) * 100.0 / osp
            if abs(calculated - savings) > 1.0:
                issues.append(qa_issue("savings_mismatch", item, "savings differs from price-derived discount by >1pp",
                                       final_sku_price=fsp, original_sku_price=osp,
                                       savings=row.get("savings"), calculated=round(calculated, 2)))

        for field in REQUIRED_DETAIL_FIELDS_BY_PRODUCT[product]:
            if row.get(field) in (None, ""):
                issues.append(qa_issue(f"missing_{field}", item, f"{field} is empty"))

        if product == "tv":
            screen_size = text_or_none(row.get("screen_size"))
            if screen_size and not (
                re.search(r"\bcm\b", screen_size, re.I) and re.search(r"\binch\b", screen_size, re.I)
            ):
                issues.append(qa_issue("screen_size_format", item, "screen_size should include cm and inch",
                                       screen_size=screen_size))
        elif product == "hhp":
            storage = text_or_none(row.get("hhp_storage"))
            if storage and not re.search(r"\b\d+\s*[GT]B\b", storage, re.I):
                issues.append(qa_issue("hhp_storage_format", item, "hhp_storage should be GB/TB storage",
                                       hhp_storage=storage))
        elif product == "ref":
            capacity = text_or_none(row.get("ref_capacity"))
            if capacity and not re.search(r"\b\d+(?:\.\d+)?\s*(?:l|litre|liter)s?\b", capacity, re.I):
                issues.append(qa_issue("ref_capacity_format", item, "ref_capacity should include litre capacity",
                                       ref_capacity=capacity))
        elif product == "ldy":
            capacity = text_or_none(row.get("ldy_capacity"))
            if capacity and not re.search(r"\b\d+(?:\.\d+)?\s*kg\b", capacity, re.I):
                issues.append(qa_issue("ldy_capacity_format", item, "ldy_capacity should include kg capacity",
                                       ldy_capacity=capacity))
            loading_type = text_or_none(row.get("ldy_loading_type"))
            if loading_type and loading_type not in {"Top Load", "Front Load"}:
                issues.append(qa_issue("ldy_loading_type_format", item,
                                       "ldy_loading_type should be Top Load or Front Load",
                                       ldy_loading_type=loading_type))

        review_total = to_int(row.get("count_of_reviews")) or 0
        content_count = review_content_count(row.get("detailed_review_content"))
        api_count = review_counts.get(str(item), 0)
        expected = min(review_total, max_reviews_per_product)
        if expected > 0 and content_count < expected:
            issues.append(qa_issue("review_content_short", item,
                                   "detailed_review_content has fewer reviews than expected",
                                   count_of_reviews=review_total, content_count=content_count, expected=expected))
        if expected > 0 and api_count < expected:
            issues.append(qa_issue("review_api_short", item,
                                   "review.csv has fewer review rows than expected",
                                   count_of_reviews=review_total, review_rows=api_count, expected=expected))

    missing_summary = " ".join(
        f"missing_{field}={sum(row['check'] == f'missing_{field}' for row in issues)}"
        for field in REQUIRED_DETAIL_FIELDS_BY_PRODUCT[product]
    )
    format_checks = [
        "screen_size_format",
        "hhp_storage_format",
        "ref_capacity_format",
        "ldy_capacity_format",
        "ldy_loading_type_format",
    ]
    format_summary = " ".join(
        f"{check}={sum(row['check'] == check for row in issues)}"
        for check in format_checks
        if any(row["check"] == check for row in issues)
    )
    summary = [
        f"[qa] issues={len(issues)} "
        f"duplicate_item={sum(row['check'] == 'duplicate_item' for row in issues)} "
        f"pid_mismatch={sum(row['check'] == 'pid_mismatch' for row in issues)} "
        f"price_inversion={sum(row['check'] == 'price_inversion' for row in issues)} "
        f"savings_mismatch={sum(row['check'] == 'savings_mismatch' for row in issues)} "
        f"review_content_short={sum(row['check'] == 'review_content_short' for row in issues)} "
        f"review_api_short={sum(row['check'] == 'review_api_short' for row in issues)}",
        "[qa] " + " ".join(part for part in (missing_summary, format_summary) if part).strip(),
        "[qa_fill] "
        f"sku_status={sum(bool(row.get('sku_status')) for row in retail_rows)} "
        f"delivery_availability={sum(bool(row.get('delivery_availability')) for row in retail_rows)} "
        f"trade_in={sum(bool(row.get('trade_in')) for row in retail_rows) if product == 'hhp' else 'n/a'}",
    ]
    return issues, summary


def validate_tv_outputs(
    retail_rows: list[dict[str, Any]],
    reviews: list[dict[str, Any]],
    max_reviews_per_product: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    return validate_schema_outputs("tv", retail_rows, reviews, max_reviews_per_product)


def key_for_row(row: dict[str, Any]) -> str | None:
    return row_url_pid(row) or row.get("fsn") or row.get("item") or row.get("product_id") or row.get("product_url")


def build_schema_outputs(
    product: str,
    main_rows: list[dict[str, Any]],
    bsr_rows: list[dict[str, Any]],
    details: list[dict[str, Any]],
    reviews: list[dict[str, Any]],
    crawl_dt: str,
    batch_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    product_key = product.lower()
    retail_cols = RETAIL_COM_COLS_BY_PRODUCT[product_key]
    product_list_cols = PRODUCT_LIST_COLS_BY_PRODUCT[product_key]
    main_by_key = {key_for_row(row): row for row in main_rows if key_for_row(row)}
    bsr_by_key = {key_for_row(row): row for row in bsr_rows if key_for_row(row)}
    detail_by_key = {key_for_row(row): row for row in details if key_for_row(row)}
    reviews_by_key: dict[str, list[dict[str, Any]]] = {}
    for row in reviews:
        key = key_for_row(row)
        if key:
            reviews_by_key.setdefault(key, []).append(row)

    ordered_keys: list[str] = []
    for row in main_rows + bsr_rows:
        key = key_for_row(row)
        if key and key not in ordered_keys:
            ordered_keys.append(key)

    retail_rows: list[dict[str, Any]] = []
    product_list_rows: list[dict[str, Any]] = []
    jsonl_rows: list[dict[str, Any]] = []

    for row in main_rows:
        jsonl_rows.append(listing_schema_record(row, product, "main", crawl_dt, batch_id))
    for row in bsr_rows:
        jsonl_rows.append(listing_schema_record(row, product, "bsr", crawl_dt, batch_id))

    for key in ordered_keys:
        main = main_by_key.get(key)
        bsr = bsr_by_key.get(key)
        primary = main or bsr or {}
        detail = detail_by_key.get(key, {})
        detailed_reviews = format_detailed_review_content(reviews_by_key.get(key, []))
        final_price_value = primary.get("final_price") or detail.get("final_sku_price")
        original_price_value = primary.get("original_price") or detail.get("original_sku_price")
        if detail:
            jsonl_rows.append(detail_schema_record(detail, product, crawl_dt, batch_id, detailed_reviews))

        page_type = "main" if main else "bsr"
        item = row_url_pid(primary) or row_url_pid(detail) or detail.get("fsn") or primary.get("product_id") or primary.get("item_id")
        retail_crawl_dt = detail.get("crawl_datetime") or primary.get("crawl_datetime") or crawl_dt
        listing_crawl_dt = primary.get("crawl_datetime") or retail_crawl_dt
        retail = {field: None for field in retail_cols}
        retail.update({
            "country": "siel",
            "product": product.upper(),
            "item": item,
            "sku": detail.get("sku"),
            "account_name": "Flipkart",
            "page_type": page_type,
            "retailer_sku_name": detail.get("retailer_sku_name") or primary.get("product_name"),
            "product_url": primary.get("product_url") or detail.get("source_url"),
            "calendar_week": calendar_week(datetime.fromisoformat(retail_crawl_dt)),
            "crawl_datetime": retail_crawl_dt,
            "batch_id": batch_id,
            "star_rating": text_or_none(detail.get("star_rating") or primary.get("star_rating")),
            "count_of_star_ratings": best_count_text(
                detail.get("count_of_star_ratings"),
                primary.get("count_of_star_ratings"),
            ),
            "count_of_reviews": best_count_text(
                detail.get("count_of_reviews"),
                primary.get("count_of_reviews"),
            ),
            "detailed_review_content": detailed_reviews,
            "retailer_sku_name_similar": text_or_none(detail.get("retailer_sku_name_similar")),
            "final_sku_price": price_text(final_price_value),
            "original_sku_price": original_price_text(original_price_value, final_price_value),
            "savings": text_or_none(primary.get("savings") or detail.get("savings")),
            "discount_type": text_or_none(primary.get("discount_type")),
            "delivery_availability": text_or_none(detail.get("delivery_availability")),
            "available_quantity_for_purchase": primary.get("available_quantity_for_purchase"),
            "sku_popularity": primary.get("sku_popularity") or detail.get("sku_popularity")
            or sku_popularity_from_url(primary.get("product_url")),
            "sku_status": primary.get("sku_status"),
            "main_rank": to_int(main.get("main_rank") if main else None),
            "bsr_rank": to_int(bsr.get("bsr_rank") if bsr else None),
            "sku_assurance": None,
        })
        for field in PRODUCT_SPECIFIC_COLS[product_key]:
            retail[field] = text_or_none(detail.get(field))
        retail_rows.append(retail)

        listing = {field: None for field in product_list_cols}
        listing.update({
            "country": retail["country"],
            "product": retail["product"],
            "item": retail["item"],
            "account_name": retail["account_name"],
            "page_type": retail["page_type"],
            "retailer_sku_name": retail["retailer_sku_name"],
            "product_url": retail["product_url"],
            "calendar_week": calendar_week(datetime.fromisoformat(listing_crawl_dt)),
            "crawl_datetime": listing_crawl_dt,
            "batch_id": retail["batch_id"],
            "star_rating": text_or_none(primary.get("star_rating")),
            "count_of_star_ratings": count_text(primary.get("count_of_star_ratings")),
            "count_of_reviews": count_text(primary.get("count_of_reviews")),
            "final_sku_price": price_text(primary.get("final_price")),
            "original_sku_price": original_price_text(primary.get("original_price"), primary.get("final_price")),
            "savings": text_or_none(primary.get("savings")),
            "discount_type": text_or_none(primary.get("discount_type")),
            "available_quantity_for_purchase": primary.get("available_quantity_for_purchase"),
            "sku_popularity": primary.get("sku_popularity") or sku_popularity_from_url(primary.get("product_url")),
            "sku_status": primary.get("sku_status"),
            "main_rank": retail["main_rank"],
            "bsr_rank": retail["bsr_rank"],
        })
        product_list_rows.append(listing)

    return retail_rows, product_list_rows, jsonl_rows


def build_tv_schema_outputs(
    product: str,
    main_rows: list[dict[str, Any]],
    bsr_rows: list[dict[str, Any]],
    details: list[dict[str, Any]],
    reviews: list[dict[str, Any]],
    crawl_dt: str,
    batch_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    return build_schema_outputs(product, main_rows, bsr_rows, details, reviews, crawl_dt, batch_id)


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def insert_into_db(jsonl_path: Path, max_n: int, dry_run: bool = False) -> tuple[int, list[str]]:
    script = ROOT / "insert_test_retail_com.py"
    if not script.exists():
        return 2, [f"[db_insert] missing insert script: {script}"]
    command = [sys.executable, str(script), str(jsonl_path), str(max_n)]
    if dry_run:
        command.append("--dry-run")
    env = os.environ.copy()
    if dry_run:
        env["SIEL_INSERT_DRY_RUN"] = "1"
    proc = subprocess.run(
        command,
        cwd=str(ROOT),
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=1200,
    )
    lines = [
        f"[db_insert] command={' '.join(command)}",
        f"[db_insert] dry_run={str(dry_run).lower()}",
        f"[db_insert] returncode={proc.returncode}",
    ]
    if proc.stdout.strip():
        lines.extend(f"[db_insert][stdout] {line}" for line in proc.stdout.strip().splitlines())
    if proc.stderr.strip():
        lines.extend(f"[db_insert][stderr] {line}" for line in proc.stderr.strip().splitlines())
    return proc.returncode, lines


def is_null_value(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def schema_null_report(name: str, rows: list[dict[str, Any]], cols: list[str]) -> list[str]:
    total = len(rows)
    lines = [f"{name} null ratio (rows={total})"]
    if total == 0:
        lines.append("- no rows")
        return lines
    for col in cols:
        nulls = sum(1 for row in rows if is_null_value(row.get(col)))
        pct = nulls * 100.0 / total
        lines.append(f"- {col}: {nulls}/{total} ({pct:.1f}%)")
    return lines


def null_count(rows: list[dict[str, Any]], col: str) -> int:
    return sum(1 for row in rows if is_null_value(row.get(col)))


def parse_db_insert_counts(lines: list[str], product_key: str) -> tuple[int | None, int | None]:
    pattern = re.compile(
        rf"\[insert\]\s+OK:\s+{re.escape(product_key.lower())}\s+retail=(\d+)\s+list=(\d+)",
        re.IGNORECASE,
    )
    for line in lines:
        match = pattern.search(line)
        if match:
            return int(match.group(1)), int(match.group(2))
    return None, None


def email_config_value(cfg: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = cfg.get(key)
        if value not in (None, ""):
            return value
    return default


def email_recipients(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in re.split(r"[;,]", value) if part.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(part).strip() for part in value if str(part).strip()]
    return [str(value).strip()]


def build_email_report(
    product: str,
    main_final: list[dict[str, Any]],
    bsr_final: list[dict[str, Any]],
    main_target: int,
    bsr_target: int,
    retail_rows: list[dict[str, Any]],
    retail_cols: list[str],
    product_list_rows: list[dict[str, Any]],
    errors: list[dict[str, Any]],
    retail_db_ok: bool | None,
    product_list_db_ok: bool | None,
) -> tuple[str, bool]:
    required_cols = ["retailer_sku_name", "final_sku_price", "product_url"]
    warnings: list[str] = []

    if len(main_final) < main_target:
        warnings.append(f"listing 수집 부족: main {len(main_final)}/{main_target}")
    if len(bsr_final) < bsr_target:
        warnings.append(f"listing 수집 부족: bsr {len(bsr_final)}/{bsr_target}")
    if retail_db_ok is False:
        warnings.append(f"{len(retail_rows)}건 retail_com DB insert 실패")
    if product_list_db_ok is False:
        warnings.append(f"{len(product_list_rows)}건 product_list DB insert 실패")
    if errors:
        warnings.append(f"{len(errors)}건 DB insert 제외: detail/review 오류")
        warnings.append(f"detail/review 오류: {len(errors)}건")
        for row in errors[:10]:
            location = row.get("url") or row.get("review_url") or "-"
            stage = row.get("stage") or "detail/review"
            reason = row.get("error") or "-"
            meta = []
            if row.get("attempts") not in (None, ""):
                meta.append(f"attempts={row.get('attempts')}")
            if row.get("review_pages") not in (None, ""):
                meta.append(f"review_pages={row.get('review_pages')}")
            if row.get("review_short_max_pages") not in (None, ""):
                meta.append(f"review_short_max_pages={row.get('review_short_max_pages')}")
            suffix = f" ({', '.join(meta)})" if meta else ""
            warnings.append(f"{location} [{stage}] {reason}{suffix}")
        if len(errors) > 10:
            warnings.append(f"detail/review 오류 {len(errors) - 10}건 추가 생략")

    for col in required_cols:
        count = null_count(retail_rows, col)
        if count:
            warnings.append(f"{col} null 발생: {count}건")

    lines = [
        f"최종 수집대상개수: {len(retail_rows)}",
    ]
    if warnings:
        lines.append("")
        lines.append("WARNING")
        lines.extend(f"- {warning}" for warning in warnings)
    else:
        lines.append("")
        lines.append("특이사항 없음")
    return "\n".join(lines) + "\n", bool(warnings)


def send_email_report(subject: str, body: str) -> tuple[bool, list[str]]:
    try:
        import config  # type: ignore
    except Exception as exc:
        return False, [f"[email] skipped: config import failed: {repr(exc)}"]

    cfg = dict(getattr(config, "EMAIL_CONFIG", {}) or {})
    server = email_config_value(cfg, "smtp_server", "smtp_host", "host")
    port = int(email_config_value(cfg, "smtp_port", "port", default=587))
    sender = email_config_value(cfg, "sender_email", "from_email", "username", "user")
    password = email_config_value(cfg, "sender_password", "password")
    recipients = email_recipients(email_config_value(cfg, "receiver_email", "receiver_emails", "to_email", "to"))
    use_ssl = bool(email_config_value(cfg, "use_ssl", "smtp_ssl", default=(port == 465)))
    use_tls = bool(email_config_value(cfg, "use_tls", "starttls", default=(not use_ssl)))
    username = email_config_value(cfg, "smtp_username", "username", "user", default=sender)

    missing = [
        name
        for name, value in (
            ("smtp_server", server),
            ("sender_email", sender),
            ("receiver_email", recipients),
        )
        if not value
    ]
    if missing:
        return False, [f"[email] skipped: missing EMAIL_CONFIG keys: {', '.join(missing)}"]

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = str(sender)
    message["To"] = ", ".join(recipients)
    message.set_content(body)

    try:
        if use_ssl:
            with smtplib.SMTP_SSL(str(server), port, timeout=60) as smtp:
                if password:
                    smtp.login(str(username), str(password))
                smtp.send_message(message)
        else:
            with smtplib.SMTP(str(server), port, timeout=60) as smtp:
                if use_tls:
                    smtp.starttls()
                if password:
                    smtp.login(str(username), str(password))
                smtp.send_message(message)
    except Exception as exc:
        return False, [f"[email] failed: {repr(exc)}"]

    return True, [f"[email] sent: {subject} -> {', '.join(recipients)}"]


def summarize_listing(stage: str, raw: list[dict[str, Any]], final: list[dict[str, Any]], pages: int, target: int) -> str:
    missing_reviews = sum(row.get("count_of_reviews") in (None, "") for row in final)
    duplicates = sum(1 for row in raw if row.get("duplicate"))
    sponsored = sum(row.get("sku_status") == "Sponsored" for row in final)
    popularity = sum(row.get("sku_popularity") not in (None, "") for row in final)
    return (
        f"[{stage}] target={target} final_unique={len(final)} raw_rows={len(raw)} "
        f"pages={pages} duplicates_skipped={duplicates} missing_reviews={missing_reviews} "
        f"sponsored={sponsored} popularity={popularity}"
    )


def run(args: argparse.Namespace) -> tuple[Path, list[str], int]:
    if args.insecure:
        os.environ["FPKT_API_INSECURE_SSL"] = "1"
    query = args.query or PRODUCT_QUERY[args.product]
    out_dir = args.out_dir or output_dir(args.product)
    out_dir.mkdir(parents=True, exist_ok=True)
    run_dt = now_crawl()
    crawl_dt = run_dt.strftime("%Y-%m-%d %H:%M:%S")
    batch_id = schema_batch_id(run_dt, args.real_batch_id)
    exit_code = 0
    db_status = "not requested"
    retail_db_ok: bool | None = None
    product_list_db_ok: bool | None = None

    lines: list[str] = [
        f"db_insert={str(bool(args.db_insert)).lower()}",
        f"db_dry_run={str(bool(args.db_dry_run)).lower()}",
        "command: " + " ".join(sys.argv),
        "api_dir: " + str(args.api_dir),
        "output_dir: " + str(out_dir),
        "schema_batch_id: " + batch_id,
        "schema_calendar_week: " + calendar_week(run_dt),
        "",
    ]

    main_raw, main_final, main_pages = listing_until(
        args.api_dir, "main", query, args.main_target, args.max_pages_main
    )
    bsr_raw, bsr_final, bsr_pages = listing_until(
        args.api_dir, "bsr", query, args.bsr_target, args.max_pages_bsr
    )
    write_csv(out_dir / "main_raw.csv", main_raw)
    write_csv(out_dir / "main_final.csv", main_final)
    write_csv(out_dir / "bsr_raw.csv", bsr_raw)
    write_csv(out_dir / "bsr_final.csv", bsr_final)
    lines.append(summarize_listing("main", main_raw, main_final, main_pages, args.main_target))
    lines.append(summarize_listing("bsr", bsr_raw, bsr_final, bsr_pages, args.bsr_target))
    lines.append("")

    details: list[dict[str, Any]] = []
    detail_retry_lines: list[str] = []
    reviews: list[dict[str, Any]] = []
    review_retry_lines: list[str] = []
    errors: list[dict[str, Any]] = []
    targets: list[dict[str, Any]] = []

    def fetch_target_detail_reviews(
        target: dict[str, Any],
        *,
        existing_detail: dict[str, Any] | None = None,
        final_retry: bool = False,
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]], list[str], list[str], dict[str, Any] | None]:
        url = target["product_url"]
        label = str(target.get("product_id") or url)
        if existing_detail is None:
            try:
                detail, local_detail_retry_lines = detail_from_api_with_retries(
                    args.api_dir,
                    url,
                    args.product,
                    args.detail_retries,
                    label,
                )
            except Exception as exc:
                return None, [], [], [], {
                    "stage": "detail",
                    "product_id": target.get("product_id"),
                    "url": url,
                    "attempts": max(args.detail_retries, 0) + 1,
                    "retry_phase": "final" if final_retry else "initial",
                    "error": repr(exc),
                }
            detail["product_id"] = target.get("product_id")
            detail["main_rank"] = target.get("main_rank")
            detail["bsr_rank"] = target.get("bsr_rank")
            detail["crawl_datetime"] = crawl_ts()
            if detail.get("star_rating") in (None, ""):
                detail["star_rating"] = target.get("star_rating")
            if detail.get("count_of_star_ratings") in (None, ""):
                detail["count_of_star_ratings"] = target.get("count_of_star_ratings")
            if detail.get("count_of_reviews") in (None, ""):
                detail["count_of_reviews"] = target.get("count_of_reviews")
        else:
            detail = existing_detail
            local_detail_retry_lines = []

        local_review_retry_lines: list[str] = []
        review_rows: list[dict[str, Any]] = []
        count_reviews = detail.get("count_of_reviews")
        if args.review_pages > 0 and detail.get("review_url") and (to_int(count_reviews) or 0) >= 1:
            try:
                review_rows, local_review_retry_lines = review_for_product_with_retries(
                    args.api_dir,
                    detail["review_url"],
                    args.review_pages,
                    args.review_short_max_pages,
                    args.max_reviews_per_product,
                    to_int(count_reviews) or 0,
                    args.review_retries,
                    str(target.get("product_id") or detail.get("fsn") or detail.get("review_url")),
                )
            except Exception as exc:
                return detail, [], local_detail_retry_lines, [], {
                    "stage": "review",
                    "product_id": target.get("product_id"),
                    "url": url,
                    "review_url": detail.get("review_url"),
                    "review_pages": args.review_pages,
                    "review_short_max_pages": args.review_short_max_pages,
                    "attempts": max(args.review_retries, 0) + 1,
                    "retry_phase": "final" if final_retry else "initial",
                    "error": repr(exc),
                }
        for review in review_rows:
            review["product_id"] = target.get("product_id")
            review["fsn"] = detail.get("fsn")
        return detail, review_rows, local_detail_retry_lines, local_review_retry_lines, None

    def append_detail_once(detail: dict[str, Any]) -> None:
        key = key_for_row(detail)
        if key and any(key_for_row(row) == key for row in details):
            return
        details.append(detail)

    if args.max_detail >= 0:
        targets = detail_targets(main_final, bsr_final, args.max_detail)
        for idx, target in enumerate(targets, 1):
            url = target["product_url"]
            safe_print(f"[detail] {idx}/{len(targets)} {target.get('product_id')} {url}")
            detail, review_rows, retry_lines, review_lines, error = fetch_target_detail_reviews(target)
            for line in retry_lines:
                safe_print(line)
            detail_retry_lines.extend(retry_lines)
            for line in review_lines:
                safe_print(line)
            review_retry_lines.extend(review_lines)
            if detail:
                append_detail_once(detail)
            reviews.extend(review_rows)
            if error:
                errors.append(error)
                safe_print(f"[error] {error.get('stage')} {target.get('product_id')} {error.get('error')}")

        if errors:
            safe_print(f"[final_retry] start unresolved={len(errors)}")
            target_by_key = {key_for_row(row): row for row in targets if key_for_row(row)}
            target_by_url = {row.get("product_url"): row for row in targets if row.get("product_url")}
            next_errors: list[dict[str, Any]] = []
            for error in errors:
                target = (
                    target_by_key.get(str(error.get("product_id") or ""))
                    or target_by_url.get(error.get("url"))
                )
                if not target:
                    next_errors.append(error)
                    continue
                detail_key = key_for_row(target)
                existing_detail = None
                if error.get("stage") == "review" and detail_key:
                    existing_detail = next((row for row in details if key_for_row(row) == detail_key), None)
                safe_print(f"[final_retry] {error.get('stage')} {error.get('url')}")
                detail, review_rows, retry_lines, review_lines, retry_error = fetch_target_detail_reviews(
                    target,
                    existing_detail=existing_detail,
                    final_retry=True,
                )
                for line in retry_lines:
                    safe_print(line)
                detail_retry_lines.extend(retry_lines)
                for line in review_lines:
                    safe_print(line)
                review_retry_lines.extend(review_lines)
                if detail:
                    append_detail_once(detail)
                reviews.extend(review_rows)
                if retry_error:
                    next_errors.append(retry_error)
                    safe_print(f"[final_retry] failed {retry_error.get('stage')} {retry_error.get('url')} {retry_error.get('error')}")
                else:
                    safe_print(f"[final_retry] recovered {error.get('url')}")
            errors = next_errors
            safe_print(f"[final_retry] remaining={len(errors)}")

    failed_keys = {str(row.get("product_id")) for row in errors if row.get("product_id")}
    if failed_keys:
        lines.append(f"[schema_filter] excluded_failed_items={len(failed_keys)}")
        main_for_schema = [row for row in main_final if str(key_for_row(row)) not in failed_keys]
        bsr_for_schema = [row for row in bsr_final if str(key_for_row(row)) not in failed_keys]
        details_for_schema = [row for row in details if str(key_for_row(row)) not in failed_keys]
        reviews_for_schema = [row for row in reviews if str(key_for_row(row)) not in failed_keys]
    else:
        main_for_schema = main_final
        bsr_for_schema = bsr_final
        details_for_schema = details
        reviews_for_schema = reviews

    write_csv(out_dir / "detail.csv", details)
    write_csv(out_dir / "review.csv", reviews)
    write_csv(out_dir / "errors.csv", errors)

    product_key = args.product.lower()
    retail_cols = RETAIL_COM_COLS_BY_PRODUCT[product_key]
    product_list_cols = PRODUCT_LIST_COLS_BY_PRODUCT[product_key]
    db_query_view_cols = DB_QUERY_VIEW_COLS_BY_PRODUCT[product_key]
    api_run_path = out_dir / "api_run.jsonl"
    retail_rows, product_list_rows, jsonl_rows = build_schema_outputs(
        args.product, main_for_schema, bsr_for_schema, details_for_schema, reviews_for_schema, crawl_dt, batch_id
    )
    write_csv_fields(out_dir / f"{product_key}_retail_com_preview.csv", retail_rows, retail_cols)
    write_csv_fields(out_dir / f"{product_key}_product_list_preview.csv", product_list_rows, product_list_cols)
    write_csv_fields(out_dir / f"out_{product_key}_retail_com.csv", retail_rows, retail_cols)
    write_csv_fields(out_dir / f"out_{product_key}_product_list.csv", product_list_rows, product_list_cols)
    write_csv_fields(out_dir / "output.csv", retail_rows, retail_cols)
    write_csv_fields(out_dir / "db_query_view.csv", retail_rows, db_query_view_cols)
    write_jsonl(api_run_path, jsonl_rows)
    qa_issues, qa_summary = validate_schema_outputs(args.product, retail_rows, reviews_for_schema, args.max_reviews_per_product)
    write_csv(out_dir / "qa_issues.csv", qa_issues)
    lines.append(
        f"[schema:{product_key}_retail_com] rows={len(retail_rows)} "
        f"cols={len(retail_cols)} "
        f"missing_item={sum(row.get('item') in (None, '') for row in retail_rows)} "
        f"missing_url={sum(row.get('product_url') in (None, '') for row in retail_rows)}"
    )
    lines.append(
        f"[schema:{product_key}_product_list] rows={len(product_list_rows)} "
        f"cols={len(product_list_cols)} "
        f"missing_item={sum(row.get('item') in (None, '') for row in product_list_rows)} "
        f"missing_url={sum(row.get('product_url') in (None, '') for row in product_list_rows)}"
    )
    lines.append(f"[schema:jsonl] rows={len(jsonl_rows)}")
    lines.extend(qa_summary)
    if args.db_insert:
        if errors:
            lines.append(f"[db_insert] excluded detail/review errors={len(errors)}")
        if qa_issues and not args.allow_qa_insert:
            lines.append(f"[db_insert] skipped: qa_issues={len(qa_issues)} (use --allow-qa-insert to force)")
            db_status = f"skipped: qa_issues={len(qa_issues)}"
            retail_db_ok = False
            product_list_db_ok = False
            exit_code = 1
        else:
            insert_code, insert_lines = insert_into_db(api_run_path, args.insert_max_n, args.db_dry_run)
            lines.extend(insert_lines)
            if insert_code != 0:
                lines.append("[db_insert] FAIL")
                db_status = f"FAIL returncode={insert_code}"
                retail_db_ok = False
                product_list_db_ok = False
                exit_code = insert_code or 1
            else:
                inserted_retail, inserted_list = parse_db_insert_counts(insert_lines, product_key)
                expected_retail = len(retail_rows) if args.insert_max_n == 0 else min(len(retail_rows), args.insert_max_n)
                expected_list = len(product_list_rows) if args.insert_max_n == 0 else min(len(product_list_rows), args.insert_max_n)
                retail_db_ok = True if inserted_retail is None else inserted_retail == expected_retail
                product_list_db_ok = True if inserted_list is None else inserted_list == expected_list
                lines.append("[db_insert] OK")
                db_status = "DRY_RUN OK" if args.db_dry_run else "OK"
    lines.append("")

    lines.append(
        f"[detail] requested={args.max_detail} ok={len(details)} errors={len(errors)} "
        f"missing_rating={sum(row.get('star_rating') in (None, '') for row in details)} "
        f"missing_reviews={sum(row.get('count_of_reviews') in (None, '') for row in details)}"
    )
    if detail_retry_lines:
        lines.append(f"[detail_retry] events={len(detail_retry_lines)}")
        lines.extend(detail_retry_lines[:50])
        if len(detail_retry_lines) > 50:
            lines.append(f"[detail_retry] omitted={len(detail_retry_lines) - 50}")
    lines.append(
        f"[review] pages_per_product={args.review_pages} rows={len(reviews)} "
        f"short_max_pages={args.review_short_max_pages} "
        f"max_per_product={args.max_reviews_per_product} "
        f"unique={len({row.get('id') for row in reviews if row.get('id')})} "
        f"errors={len(errors)}"
    )
    if review_retry_lines:
        lines.append(f"[review_retry] events={len(review_retry_lines)}")
        lines.extend(review_retry_lines[:50])
        if len(review_retry_lines) > 50:
            lines.append(f"[review_retry] omitted={len(review_retry_lines) - 50}")
    lines.append("")
    lines.append("files:")
    file_names = ["main_final.csv", "bsr_final.csv", "detail.csv", "review.csv", "errors.csv"]
    file_names += [
        f"{product_key}_retail_com_preview.csv",
        f"{product_key}_product_list_preview.csv",
        f"out_{product_key}_retail_com.csv",
        f"out_{product_key}_product_list.csv",
        "output.csv",
        "qa_issues.csv",
        "api_run.jsonl",
        "db_query_view.csv",
    ]
    for name in file_names:
        lines.append(f"- {out_dir / name}")
    lines.append("")
    lines.append("main_sample:")
    for row in main_final[:10]:
        lines.append(
            f"{row.get('main_rank')}\t{row.get('product_id')}\t{row.get('star_rating')}\t"
            f"{row.get('count_of_star_ratings')}\t{row.get('count_of_reviews')}\t"
            f"{row.get('final_price')}\t{row.get('product_name')}"
        )
    lines.append("")
    lines.append("bsr_sample:")
    for row in bsr_final[:10]:
        lines.append(
            f"{row.get('bsr_rank')}\t{row.get('product_id')}\t{row.get('star_rating')}\t"
            f"{row.get('count_of_star_ratings')}\t{row.get('count_of_reviews')}\t"
            f"{row.get('final_price')}\t{row.get('product_name')}"
        )

    if args.email_report:
        subject = f"[SIEL] {product_key.upper()} crawling report"
        body, has_email_warning = build_email_report(
            args.product,
            main_final,
            bsr_final,
            args.main_target,
            args.bsr_target,
            retail_rows,
            retail_cols,
            product_list_rows,
            errors,
            retail_db_ok,
            product_list_db_ok,
        )
        if has_email_warning:
            subject = f"WARNING {subject}"
        (out_dir / "email_report.txt").write_text(body, encoding="utf-8")
        _sent, email_lines = send_email_report(subject, body)
        lines.extend(email_lines)

    (out_dir / "summary.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out_dir / "command.txt").write_text(" ".join(sys.argv) + "\n", encoding="utf-8")
    return out_dir, lines, exit_code


def main() -> int:
    parser = argparse.ArgumentParser(description="Flipkart API operation runner; DB insert is opt-in")
    parser.add_argument("--api-dir", type=Path, default=default_api_dir())
    parser.add_argument("--product", choices=sorted(PRODUCT_QUERY), default="tv")
    parser.add_argument("--query", default=None)
    parser.add_argument("--main-target", type=int, default=300)
    parser.add_argument("--bsr-target", type=int, default=100)
    parser.add_argument("--max-pages-main", type=int, default=30)
    parser.add_argument("--max-pages-bsr", type=int, default=15)
    parser.add_argument("--max-detail", type=int, default=10, help="detail target count; 0=all unique, -1=skip detail")
    parser.add_argument("--detail-retries", type=int, default=2, help="Retry detail API when a product response fails")
    parser.add_argument("--review-pages", type=int, default=2)
    parser.add_argument("--review-short-max-pages", type=int, default=3,
                        help="When review rows are short, expand review pages up to this value")
    parser.add_argument("--max-reviews-per-product", type=int, default=20)
    parser.add_argument("--review-retries", type=int, default=2, help="Retry review API when collected rows are below count_of_reviews")
    parser.add_argument("--out-dir", type=Path)
    parser.add_argument("--insecure", action="store_true")
    parser.add_argument("--real-batch-id", action="store_true", help="Use the normal f_YYYYMMDD_NNNNNN batch counter")
    parser.add_argument("--db-insert", action="store_true", help="Insert into existing dx_siel_* tables after output validation")
    parser.add_argument("--db-dry-run", action="store_true", help="Execute DB insert SQL and roll it back")
    parser.add_argument("--insert-max-n", type=int, default=0, help="Rows cap passed to insert_test_retail_com.py; 0=unlimited")
    parser.add_argument("--allow-qa-insert", action="store_true", help="Insert even when QA issues are present")
    parser.add_argument("--email-report", action="store_true", help="Send per-product crawl report email using config.EMAIL_CONFIG")
    args = parser.parse_args()

    out_dir, lines, exit_code = run(args)
    for line in lines:
        safe_print(line)
    safe_print(f"[saved] {out_dir}")
    safe_print(f"[summary] {out_dir / 'summary.txt'}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
