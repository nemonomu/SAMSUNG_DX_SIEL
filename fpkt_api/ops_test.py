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
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from lxml import html

from main_probe import extract_products, request_headers
from phase_probe import (
    default_api_dir,
    fetch_json,
    first_har_request_for_page,
    first_text,
    listing_page,
    normalize_text,
    page_fetch_curl_commands,
    product_ld,
    review_rows_from_response,
)


PRODUCT_QUERY = {
    "tv": "tv",
    "hhp": "smartphone",
    "ref": "refrigerator",
    "ldy": "washing+machine",
}


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


def to_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    match = re.search(r"\d[\d,]*", str(value))
    if not match:
        return None
    return int(match.group(0).replace(",", ""))


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
        "availability": offers.get("availability"),
        "star_rating": rating.get("ratingValue"),
        "count_of_star_ratings": rating.get("ratingCount"),
        "count_of_reviews": rating.get("reviewCount"),
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
        "review_url": review_url,
        "jsonld_review_count": len(ld.get("review") or []),
    }


def review_uri(review_url: str) -> str:
    parsed = urlparse(review_url)
    uri = parsed.path
    if parsed.query:
        uri += "?" + parsed.query
    return uri


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
    for row in main_rows + bsr_rows:
        key = row.get("product_id") or row.get("product_url")
        if not key or key in seen or not row.get("product_url"):
            continue
        seen.add(key)
        targets.append(row)
        if len(targets) >= max_detail:
            break
    return targets


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

    lines: list[str] = [
        "db_insert=false",
        "command: " + " ".join(sys.argv),
        "api_dir: " + str(args.api_dir),
        "output_dir: " + str(out_dir),
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
    if args.max_detail > 0:
        headers = html_headers(args.api_dir)
        targets = detail_targets(main_final, bsr_final, args.max_detail)
        for idx, target in enumerate(targets, 1):
            url = target["product_url"]
            safe_print(f"[detail] {idx}/{len(targets)} {target.get('product_id')} {url}")
            try:
                detail = detail_from_html(fetch_text(url, headers), url)
                detail["product_id"] = target.get("product_id")
                detail["main_rank"] = target.get("main_rank")
                detail["bsr_rank"] = target.get("bsr_rank")
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
    for name in ["main_final.csv", "bsr_final.csv", "detail.csv", "review.csv", "errors.csv"]:
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
    parser.add_argument("--max-detail", type=int, default=10)
    parser.add_argument("--review-pages", type=int, default=2)
    parser.add_argument("--max-reviews-per-product", type=int, default=20)
    parser.add_argument("--out-dir", type=Path)
    parser.add_argument("--insecure", action="store_true")
    args = parser.parse_args()

    out_dir, lines = run(args)
    for line in lines:
        safe_print(line)
    safe_print(f"[saved] {out_dir}")
    safe_print(f"[summary] {out_dir / 'summary.txt'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
