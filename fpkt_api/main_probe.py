"""Probe Flipkart main listing page/fetch API responses.

This module is intentionally separate from the current crawler. It reads a HAR
captured from Flipkart search/main listing, replays the page/fetch request when
asked, and extracts product rows from the structured JSON response.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


PAGE_FETCH_MARKER = "rome.api.flipkart.com/api/4/page/fetch"


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8", errors="replace"))


def iter_page_fetch_requests(har: dict[str, Any]) -> list[tuple[int, dict[str, Any], dict[str, Any]]]:
    found: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
    entries = har.get("log", {}).get("entries", [])
    for index, entry in enumerate(entries):
        request = entry.get("request", {})
        if request.get("method") == "POST" and PAGE_FETCH_MARKER in request.get("url", ""):
            found.append((index, request, entry.get("response") or {}))
    return found


def find_page_fetch_request(har: dict[str, Any], entry_index: int | None = None) -> dict[str, Any]:
    found = iter_page_fetch_requests(har)
    if entry_index is not None:
        for index, request, _response in found:
            if index == entry_index:
                return request
        raise ValueError(f"HAR entry {entry_index} is not a POST {PAGE_FETCH_MARKER} request")
    if found:
        return found[0][1]
    raise ValueError(f"POST {PAGE_FETCH_MARKER} request not found in HAR")


def list_page_fetch_requests(har: dict[str, Any]) -> None:
    for index, request, response in iter_page_fetch_requests(har):
        body = json.loads(request.get("postData", {}).get("text") or "{}")
        page_context = body.get("pageContext") or {}
        print(
            "entry={entry} status={status} bodySize={body_size} pageNumber={page} "
            "paginatedFetch={paginated} pageUri={uri}".format(
                entry=index,
                status=response.get("status"),
                body_size=response.get("bodySize"),
                page=page_context.get("pageNumber"),
                paginated=page_context.get("paginatedFetch"),
                uri=body.get("pageUri"),
            )
        )


def request_headers(request: dict[str, Any]) -> dict[str, str]:
    skipped = {"host", "content-length", "accept-encoding"}
    headers: dict[str, str] = {}
    for header in request.get("headers", []):
        name = header.get("name")
        value = header.get("value")
        if not name or value is None or name.lower() in skipped:
            continue
        headers[name] = value
    headers["content-type"] = "application/json"
    headers.setdefault("accept", "*/*")
    return headers


def page_uri(query: str, page: int) -> str:
    return (
        f"/search?q={query}&otracker=search&otracker1=search"
        f"&marketplace=FLIPKART&as-show=on&as=off&page={page}"
    )


def build_body(
    request: dict[str, Any],
    page: int,
    query: str,
    pagination_response: dict[str, Any] | None = None,
) -> bytes:
    body = json.loads(request.get("postData", {}).get("text") or "{}")
    body["pageUri"] = page_uri(query, page)
    page_context = body.setdefault("pageContext", {})
    page_context["pageNumber"] = page
    page_context["paginatedFetch"] = True

    if pagination_response:
        page_data = (pagination_response.get("RESPONSE") or {}).get("pageData") or {}
        pagination_map = page_data.get("paginationContextMap") or {}
        if pagination_map:
            page_context["paginationContextMap"] = pagination_map

    return json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def fetch_page(
    har_path: Path,
    page: int,
    query: str,
    pagination_response_path: Path | None,
    entry_index: int | None = None,
) -> dict[str, Any]:
    har = load_json(har_path)
    request = find_page_fetch_request(har, entry_index=entry_index)
    pagination_response = load_json(pagination_response_path) if pagination_response_path else None
    payload = build_body(request, page, query, pagination_response)
    http_request = urllib.request.Request(
        request["url"],
        data=payload,
        headers=request_headers(request),
        method="POST",
    )
    with urllib.request.urlopen(http_request, timeout=30) as response:
        raw = response.read()
    return json.loads(raw.decode("utf-8", errors="replace"))


def price_value(price: Any) -> Any:
    if isinstance(price, dict):
        return price.get("value") or price.get("decimalValue")
    return price


def discount_text(pricing: dict[str, Any]) -> str | None:
    candidates = [pricing.get("totalDiscount")]
    prices = pricing.get("prices")
    if isinstance(prices, list):
        candidates.extend(item.get("discount") for item in prices if isinstance(item, dict))
    for value in candidates:
        if value in (None, "", 0, "0"):
            continue
        text = str(value).strip()
        return text if text.endswith("%") else f"{text}%"
    return None


def product_url(action: dict[str, Any], value: dict[str, Any]) -> str | None:
    params = action.get("params") or {}
    url = params.get("url") or action.get("url") or value.get("baseUrl") or value.get("smartUrl")
    if isinstance(url, str) and url.startswith("/"):
        return "https://www.flipkart.com" + url
    return url


def extract_products(response: dict[str, Any], page: int | None = None, page_size: int = 24) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    slots = (response.get("RESPONSE") or {}).get("slots") or []
    offset = ((page - 1) * page_size) if page else 0
    for slot in slots:
        widget = slot.get("widget") or {}
        if widget.get("type") != "PRODUCT_SUMMARY":
            continue
        products = (widget.get("data") or {}).get("products") or []
        for product in products:
            product_info = product.get("productInfo") or {}
            action = product_info.get("action") or {}
            params = action.get("params") or {}
            value = product_info.get("value") or {}
            pricing = value.get("pricing") or {}
            rating = value.get("rating") or {}
            titles = value.get("titles") or {}
            rows.append(
                {
                    "rank": offset + len(rows) + 1 if page else len(rows) + 1,
                    "product_id": value.get("id") or value.get("productId") or params.get("productId"),
                    "item_id": value.get("itemId") or params.get("itemId"),
                    "listing_id": value.get("listingId") or params.get("listingId"),
                    "product_name": titles.get("title") or titles.get("newTitle"),
                    "brand": value.get("productBrand"),
                    "product_url": product_url(action, value),
                    "final_price": price_value(pricing.get("finalPrice")),
                    "original_price": price_value(pricing.get("mrp")),
                    "savings": discount_text(pricing),
                    "star_rating": rating.get("average"),
                    "count_of_star_ratings": rating.get("count"),
                    "count_of_reviews": rating.get("reviewCount"),
                    "availability": (value.get("availability") or {}).get("displayState"),
                }
            )
    return rows


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--har", type=Path, help="HAR with Flipkart page/fetch request")
    parser.add_argument("--response", type=Path, help="Existing page/fetch JSON response to parse")
    parser.add_argument("--entry-index", type=int, help="Replay this exact HAR entry index")
    parser.add_argument("--list-requests", action="store_true", help="List page/fetch requests in a HAR")
    parser.add_argument("--page", type=int, help="Page number to fetch or use for rank offset")
    parser.add_argument("--query", default="tv")
    parser.add_argument("--pagination-response", type=Path)
    parser.add_argument("--out", type=Path, help="Optional CSV output path")
    args = parser.parse_args()

    if args.list_requests:
        if not args.har:
            parser.error("--list-requests requires --har")
        list_page_fetch_requests(load_json(args.har))
        return 0

    if args.response:
        response = load_json(args.response)
    elif args.har and args.page:
        response = fetch_page(args.har, args.page, args.query, args.pagination_response, args.entry_index)
    else:
        parser.error("provide --response, or provide --har with --page")

    rows = extract_products(response, page=args.page)
    product_ids = [row["product_id"] for row in rows if row.get("product_id")]
    duplicate_ids = sorted({pid for pid in product_ids if product_ids.count(pid) > 1})
    print(
        "products={products} unique_product_ids={unique} duplicate_product_ids={dups} "
        "missing_ratings={missing_ratings} missing_reviews={missing_reviews}".format(
            products=len(rows),
            unique=len(set(product_ids)),
            dups=duplicate_ids,
            missing_ratings=sum(row.get("count_of_star_ratings") in (None, "") for row in rows),
            missing_reviews=sum(row.get("count_of_reviews") in (None, "") for row in rows),
        )
    )
    for row in rows[:10]:
        print(
            "{rank}\t{product_id}\t{star_rating}\t{count_of_star_ratings}\t"
            "{count_of_reviews}\t{final_price}\t{product_name}".format(**row)
        )
    if args.out:
        write_csv(args.out, rows)
        print(f"wrote={args.out}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except urllib.error.HTTPError as exc:
        sys.stderr.write(f"HTTP {exc.code}: {exc.read(500).decode('utf-8', errors='replace')}\n")
        raise SystemExit(2)
