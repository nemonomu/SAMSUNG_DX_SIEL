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
import ssl
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
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


PRODUCT_QUERY = {
    "tv": "tv",
    "hhp": "smartphone",
    "ref": "refrigerator",
    "ldy": "washing+machine",
}

IST = timezone(timedelta(hours=5, minutes=30))

TV_RETAIL_COM_COLS = [
    "country", "product", "item", "sku", "account_name", "page_type",
    "retailer_sku_name", "product_url", "calendar_week", "crawl_datetime", "batch_id",
    "star_rating", "count_of_star_ratings", "count_of_reviews",
    "detailed_review_content", "retailer_sku_name_similar",
    "final_sku_price", "original_sku_price", "savings", "discount_type",
    "delivery_availability", "available_quantity_for_purchase",
    "sku_popularity", "sku_status", "main_rank", "bsr_rank",
    "screen_size", "model_year", "estimated_annual_electricity_use",
    "summarized_review_content", "fastest_delivery", "inventory_status",
    "sku_assurance", "number_of_units_purchased_past_month",
]

TV_PRODUCT_LIST_COLS = [
    "country", "product", "item", "account_name", "page_type",
    "retailer_sku_name", "product_url", "calendar_week", "crawl_datetime", "batch_id",
    "star_rating", "count_of_star_ratings", "count_of_reviews",
    "final_sku_price", "original_sku_price", "savings", "discount_type",
    "available_quantity_for_purchase",
    "sku_popularity", "sku_status", "main_rank", "bsr_rank",
    "number_of_units_purchased_past_month",
]


def safe_print(value: str) -> None:
    encoding = sys.stdout.encoding or "utf-8"
    print(value.encode(encoding, errors="replace").decode(encoding, errors="replace"))


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
        return text_or_none(value)
    return f"{parsed:,}"


def sku_popularity_from_url(url: Any) -> str | None:
    url = str(url or "")
    labels = []
    if "spotlightTagId=default_BestsellerId" in url:
        labels.append("Bestseller")
    if "spotlightTagId=default_TrendingId" in url:
        labels.append("Trending")
    return ", ".join(labels) if labels else None


def now_ist() -> datetime:
    return datetime.now(IST)


def calendar_week(dt: datetime) -> str:
    return f"w{dt.isocalendar().week:02d}"


def test_batch_id(dt: datetime) -> str:
    return f"f_{dt.strftime('%Y%m%d')}_000000"


def schema_batch_id(dt: datetime, real_batch_id: bool) -> str:
    if real_batch_id:
        return next_batch_id("f", str(ROOT), dt)
    return test_batch_id(dt)


def output_dir(product: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
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

    for page in range(1, max_pages + 1):
        pages_used = page
        response = listing_page(har_path, query, page, sort, previous_response)
        previous_response = response
        for row in extract_products(response, page=page):
            key = row.get("product_id") or row.get("item_id") or row.get("product_url")
            raw = dict(row)
            raw["stage"] = stage
            raw["duplicate"] = bool(key in seen)
            raw_rows.append(raw)
            if key in seen:
                continue
            if key:
                seen.add(key)
            final = dict(raw)
            final["unique_rank"] = len(final_rows) + 1
            final[f"{stage}_rank"] = final.get("rank")
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
        "fsn": ld.get("sku") or (re.search(r"[?&]pid=([A-Z0-9]+)", source_url or "").group(1)
                                  if re.search(r"[?&]pid=([A-Z0-9]+)", source_url or "") else None),
        "brand": ((ld.get("brand") or {}).get("name") if isinstance(ld.get("brand"), dict) else None),
        "final_sku_price": offers.get("price"),
        "original_sku_price": first_text(doc, '(//div[contains(@style,"line-through")])[1]'),
        "savings": first_text(
            doc,
            '(//div[contains(text(),"%") and string-length(normalize-space(text()))<=5 '
            'and not(ancestor::a[contains(@href,"/p/")])])[1]',
        ),
        "discount_type": first_text(
            doc,
            '//*[contains(text(),"Hot Deal") or contains(text(),"Hot deal") or '
            'contains(text(),"Super Deals") or contains(text(),"Saver Deal") or '
            'contains(text(),"Lowest Price Live") or contains(text(),"Limited time") or '
            'contains(text(),"Special Price")][1]',
        ),
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


def detail_from_api_response(response: dict[str, Any], source_url: str) -> dict[str, Any]:
    texts = json_texts(response)
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

    return {
        "source_url": source_url,
        "retailer_sku_name": first_json_key(response, "prependingText"),
        "fsn": (re.search(r"[?&]pid=([A-Z0-9]+)", source_url or "").group(1)
                if re.search(r"[?&]pid=([A-Z0-9]+)", source_url or "") else None),
        "final_sku_price": None,
        "original_sku_price": None,
        "savings": None,
        "discount_type": next((text for text in texts if text in {"Hot Deal", "Hot deal", "Special Price"}), None),
        "availability": None,
        "star_rating": rating_value,
        "count_of_star_ratings": rating_count,
        "count_of_reviews": review_count,
        "sku": json_label_value(response, "Model Name"),
        "screen_size": json_label_value(response, "Display Size"),
        "model_year": json_label_value(response, "Launch Year"),
        "estimated_annual_electricity_use": (
            json_label_value(response, "Power Consumption")
            or json_label_value(response, "Annual Energy Consumption")
            or json_label_value(response, "Energy Consumption")
        ),
        "review_url": fallback_review_url(source_url),
        "jsonld_review_count": None,
    }


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
    fsn = row.get("product_id") or row.get("item_id")
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
        "original_sku_price": price_text(row.get("original_price")),
        "savings": text_or_none(row.get("savings")),
        "star_rating": text_or_none(row.get("star_rating")),
        "count_of_star_ratings": count_text(row.get("count_of_star_ratings")),
        "count_of_reviews": count_text(row.get("count_of_reviews")),
        "sku_popularity": row.get("sku_popularity") or sku_popularity_from_url(row.get("product_url")),
        "sku_status": row.get("sku_status"),
        "available_quantity_for_purchase": row.get("available_quantity_for_purchase"),
        "batch_id": batch_id,
        "crawl_datetime": crawl_dt,
    }


def detail_schema_record(
    detail: dict[str, Any],
    product: str,
    crawl_dt: str,
    batch_id: str,
    detailed_review_content: str | None,
) -> dict[str, Any]:
    fsn = detail.get("fsn") or detail.get("product_id")
    return {
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
        "original_sku_price": price_text(detail.get("original_sku_price")),
        "savings": text_or_none(detail.get("savings")),
        "discount_type": text_or_none(detail.get("discount_type")),
        "star_rating": text_or_none(detail.get("star_rating")),
        "count_of_star_ratings": count_text(detail.get("count_of_star_ratings")),
        "count_of_reviews": count_text(detail.get("count_of_reviews")),
        "detailed_review_content": detailed_review_content,
        "screen_size": text_or_none(detail.get("screen_size")),
        "model_year": text_or_none(detail.get("model_year")),
        "estimated_annual_electricity_use": text_or_none(detail.get("estimated_annual_electricity_use")),
        "delivery_availability": text_or_none(detail.get("delivery_availability")),
        "batch_id": batch_id,
        "crawl_datetime": crawl_dt,
    }


def format_detailed_review_content(rows: list[dict[str, Any]]) -> str | None:
    parts = [normalize_text(str(row.get("text") or "")) for row in rows]
    parts = [part for part in parts if part]
    if not parts:
        return None
    return " ||| ".join(f"review{idx + 1} - {text}" for idx, text in enumerate(parts[:20]))


def url_pid(value: Any) -> str | None:
    match = re.search(r"[?&]pid=([A-Z0-9]+)", str(value or ""))
    return match.group(1) if match else None


def review_content_count(value: Any) -> int:
    text = str(value or "")
    if not text:
        return 0
    return len(re.findall(r"(?:^| \|\|\| )review\d+\s+-", text))


def qa_issue(check: str, item: Any, message: str, **extra: Any) -> dict[str, Any]:
    row = {"check": check, "item": item, "message": message}
    row.update(extra)
    return row


def validate_tv_outputs(
    retail_rows: list[dict[str, Any]],
    reviews: list[dict[str, Any]],
    max_reviews_per_product: int,
) -> tuple[list[dict[str, Any]], list[str]]:
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

        for field in ("sku", "screen_size", "model_year"):
            if row.get(field) in (None, ""):
                issues.append(qa_issue(f"missing_{field}", item, f"{field} is empty"))

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

    summary = [
        f"[qa] issues={len(issues)} "
        f"duplicate_item={sum(row['check'] == 'duplicate_item' for row in issues)} "
        f"pid_mismatch={sum(row['check'] == 'pid_mismatch' for row in issues)} "
        f"price_inversion={sum(row['check'] == 'price_inversion' for row in issues)} "
        f"savings_mismatch={sum(row['check'] == 'savings_mismatch' for row in issues)} "
        f"review_content_short={sum(row['check'] == 'review_content_short' for row in issues)} "
        f"review_api_short={sum(row['check'] == 'review_api_short' for row in issues)}",
        f"[qa] missing_sku={sum(row['check'] == 'missing_sku' for row in issues)} "
        f"missing_screen_size={sum(row['check'] == 'missing_screen_size' for row in issues)} "
        f"missing_model_year={sum(row['check'] == 'missing_model_year' for row in issues)}",
    ]
    return issues, summary


def key_for_row(row: dict[str, Any]) -> str | None:
    return row.get("product_id") or row.get("fsn") or row.get("item") or row.get("product_url")


def build_tv_schema_outputs(
    product: str,
    main_rows: list[dict[str, Any]],
    bsr_rows: list[dict[str, Any]],
    details: list[dict[str, Any]],
    reviews: list[dict[str, Any]],
    crawl_dt: str,
    batch_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
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
        if detail:
            jsonl_rows.append(detail_schema_record(detail, product, crawl_dt, batch_id, detailed_reviews))

        page_type = "main" if main else "bsr"
        item = detail.get("fsn") or primary.get("product_id") or primary.get("item_id")
        retail = {field: None for field in TV_RETAIL_COM_COLS}
        retail.update({
            "country": "siel",
            "product": product.upper(),
            "item": item,
            "sku": detail.get("sku"),
            "account_name": "Flipkart",
            "page_type": page_type,
            "retailer_sku_name": primary.get("product_name") or detail.get("retailer_sku_name"),
            "product_url": primary.get("product_url") or detail.get("source_url"),
            "calendar_week": calendar_week(datetime.fromisoformat(crawl_dt)),
            "crawl_datetime": crawl_dt,
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
            "final_sku_price": price_text(primary.get("final_price") or detail.get("final_sku_price")),
            "original_sku_price": price_text(primary.get("original_price") or detail.get("original_sku_price")),
            "savings": text_or_none(primary.get("savings") or detail.get("savings")),
            "discount_type": text_or_none(primary.get("discount_type") or detail.get("discount_type")),
            "delivery_availability": text_or_none(detail.get("delivery_availability")),
            "available_quantity_for_purchase": primary.get("available_quantity_for_purchase"),
            "sku_popularity": primary.get("sku_popularity") or detail.get("sku_popularity")
            or sku_popularity_from_url(primary.get("product_url")),
            "sku_status": primary.get("sku_status"),
            "main_rank": to_int(main.get("main_rank") if main else None),
            "bsr_rank": to_int(bsr.get("bsr_rank") if bsr else None),
            "screen_size": text_or_none(detail.get("screen_size")),
            "model_year": text_or_none(detail.get("model_year")),
            "estimated_annual_electricity_use": text_or_none(detail.get("estimated_annual_electricity_use")),
            "sku_assurance": None,
        })
        retail_rows.append(retail)

        listing = {field: None for field in TV_PRODUCT_LIST_COLS}
        listing.update({
            "country": retail["country"],
            "product": retail["product"],
            "item": retail["item"],
            "account_name": retail["account_name"],
            "page_type": retail["page_type"],
            "retailer_sku_name": retail["retailer_sku_name"],
            "product_url": retail["product_url"],
            "calendar_week": retail["calendar_week"],
            "crawl_datetime": retail["crawl_datetime"],
            "batch_id": retail["batch_id"],
            "star_rating": text_or_none(primary.get("star_rating")),
            "count_of_star_ratings": count_text(primary.get("count_of_star_ratings")),
            "count_of_reviews": count_text(primary.get("count_of_reviews")),
            "final_sku_price": price_text(primary.get("final_price")),
            "original_sku_price": price_text(primary.get("original_price")),
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


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def summarize_listing(stage: str, raw: list[dict[str, Any]], final: list[dict[str, Any]], pages: int, target: int) -> str:
    missing_reviews = sum(row.get("count_of_reviews") in (None, "") for row in final)
    duplicates = sum(1 for row in raw if row.get("duplicate"))
    return (
        f"[{stage}] target={target} final_unique={len(final)} raw_rows={len(raw)} "
        f"pages={pages} duplicates_skipped={duplicates} missing_reviews={missing_reviews}"
    )


def run(args: argparse.Namespace) -> tuple[Path, list[str]]:
    if args.insecure:
        os.environ["FPKT_API_INSECURE_SSL"] = "1"
    query = args.query or PRODUCT_QUERY[args.product]
    out_dir = args.out_dir or output_dir(args.product)
    out_dir.mkdir(parents=True, exist_ok=True)
    run_dt = now_ist()
    crawl_dt = run_dt.isoformat(timespec="seconds")
    batch_id = schema_batch_id(run_dt, args.real_batch_id)

    lines: list[str] = [
        "db_insert=false",
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
    reviews: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    if args.max_detail >= 0:
        targets = detail_targets(main_final, bsr_final, args.max_detail)
        for idx, target in enumerate(targets, 1):
            url = target["product_url"]
            safe_print(f"[detail] {idx}/{len(targets)} {target.get('product_id')} {url}")
            try:
                detail = detail_from_api_response(detail_api_response(args.api_dir, url), url)
                detail["product_id"] = target.get("product_id")
                detail["main_rank"] = target.get("main_rank")
                detail["bsr_rank"] = target.get("bsr_rank")
                if detail.get("star_rating") in (None, ""):
                    detail["star_rating"] = target.get("star_rating")
                if detail.get("count_of_star_ratings") in (None, ""):
                    detail["count_of_star_ratings"] = target.get("count_of_star_ratings")
                if detail.get("count_of_reviews") in (None, ""):
                    detail["count_of_reviews"] = target.get("count_of_reviews")
                details.append(detail)
                count_reviews = detail.get("count_of_reviews")
                if args.review_pages > 0 and detail.get("review_url") and (to_int(count_reviews) or 0) >= 1:
                    for review in review_for_product(
                        args.api_dir,
                        detail["review_url"],
                        args.review_pages,
                        args.max_reviews_per_product,
                    ):
                        review["product_id"] = target.get("product_id")
                        review["fsn"] = detail.get("fsn")
                        reviews.append(review)
            except Exception as exc:
                errors.append({
                    "stage": "detail_review",
                    "product_id": target.get("product_id"),
                    "url": url,
                    "error": repr(exc),
                })
                safe_print(f"[error] {target.get('product_id')} {repr(exc)}")

    write_csv(out_dir / "detail.csv", details)
    write_csv(out_dir / "review.csv", reviews)
    write_csv(out_dir / "errors.csv", errors)

    if args.product == "tv":
        retail_rows, product_list_rows, jsonl_rows = build_tv_schema_outputs(
            args.product, main_final, bsr_final, details, reviews, crawl_dt, batch_id
        )
        write_csv_fields(out_dir / "tv_retail_com_preview.csv", retail_rows, TV_RETAIL_COM_COLS)
        write_csv_fields(out_dir / "tv_product_list_preview.csv", product_list_rows, TV_PRODUCT_LIST_COLS)
        write_csv_fields(out_dir / "out_tv_retail_com.csv", retail_rows, TV_RETAIL_COM_COLS)
        write_csv_fields(out_dir / "out_tv_product_list.csv", product_list_rows, TV_PRODUCT_LIST_COLS)
        write_jsonl(out_dir / "api_run.jsonl", jsonl_rows)
        qa_issues, qa_summary = validate_tv_outputs(retail_rows, reviews, args.max_reviews_per_product)
        write_csv(out_dir / "qa_issues.csv", qa_issues)
        lines.append(
            f"[schema:tv_retail_com] rows={len(retail_rows)} "
            f"cols={len(TV_RETAIL_COM_COLS)} "
            f"missing_item={sum(row.get('item') in (None, '') for row in retail_rows)} "
            f"missing_url={sum(row.get('product_url') in (None, '') for row in retail_rows)}"
        )
        lines.append(
            f"[schema:tv_product_list] rows={len(product_list_rows)} "
            f"cols={len(TV_PRODUCT_LIST_COLS)} "
            f"missing_item={sum(row.get('item') in (None, '') for row in product_list_rows)} "
            f"missing_url={sum(row.get('product_url') in (None, '') for row in product_list_rows)}"
        )
        lines.append(f"[schema:jsonl] rows={len(jsonl_rows)}")
        lines.extend(qa_summary)
        lines.append("")

    lines.append(
        f"[detail] requested={args.max_detail} ok={len(details)} errors={len(errors)} "
        f"missing_rating={sum(row.get('star_rating') in (None, '') for row in details)} "
        f"missing_reviews={sum(row.get('count_of_reviews') in (None, '') for row in details)}"
    )
    lines.append(
        f"[review] pages_per_product={args.review_pages} rows={len(reviews)} "
        f"max_per_product={args.max_reviews_per_product} "
        f"unique={len({row.get('id') for row in reviews if row.get('id')})} "
        f"errors={len(errors)}"
    )
    lines.append("")
    lines.append("files:")
    file_names = ["main_final.csv", "bsr_final.csv", "detail.csv", "review.csv", "errors.csv"]
    if args.product == "tv":
        file_names += [
            "tv_retail_com_preview.csv",
            "tv_product_list_preview.csv",
            "out_tv_retail_com.csv",
            "out_tv_product_list.csv",
            "qa_issues.csv",
            "api_run.jsonl",
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

    (out_dir / "summary.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out_dir / "command.txt").write_text(" ".join(sys.argv) + "\n", encoding="utf-8")
    return out_dir, lines


def main() -> int:
    parser = argparse.ArgumentParser(description="Flipkart API operation-like test; never inserts into DB")
    parser.add_argument("--api-dir", type=Path, default=default_api_dir())
    parser.add_argument("--product", choices=sorted(PRODUCT_QUERY), default="tv")
    parser.add_argument("--query", default=None)
    parser.add_argument("--main-target", type=int, default=300)
    parser.add_argument("--bsr-target", type=int, default=100)
    parser.add_argument("--max-pages-main", type=int, default=30)
    parser.add_argument("--max-pages-bsr", type=int, default=15)
    parser.add_argument("--max-detail", type=int, default=10, help="detail target count; 0=all unique, -1=skip detail")
    parser.add_argument("--review-pages", type=int, default=2)
    parser.add_argument("--max-reviews-per-product", type=int, default=20)
    parser.add_argument("--out-dir", type=Path)
    parser.add_argument("--insecure", action="store_true")
    parser.add_argument("--real-batch-id", action="store_true", help="Use the normal f_YYYYMMDD_NNNNNN batch counter")
    args = parser.parse_args()

    out_dir, lines = run(args)
    for line in lines:
        safe_print(line)
    safe_print(f"[saved] {out_dir}")
    safe_print(f"[summary] {out_dir / 'summary.txt'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
