"""End-to-end Flipkart API/structured-data probe.

This is a non-production probe for checking whether Flipkart stages can be
collected without XPath-heavy browser scraping:

- main listing: rome.api page/fetch
- bsr listing: same endpoint with sort=popularity
- detail: product page JSON-LD plus visible spec labels
- review: rome.api page/fetch review pages
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from lxml import html

from main_probe import extract_products, request_headers


PAGE_FETCH = "api/4/page/fetch"


def safe_print(value: str) -> None:
    encoding = sys.stdout.encoding or "utf-8"
    print(value.encode(encoding, errors="replace").decode(encoding, errors="replace"))


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8", errors="replace"))


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def default_api_dir() -> Path:
    if os.environ.get("FPKT_API_DIR"):
        return Path(os.environ["FPKT_API_DIR"])
    repo_root = Path(__file__).resolve().parents[1]
    candidates = [
        repo_root / "logs" / "api",
        repo_root / "siel_logs" / "api",
        repo_root.parent / "logs" / "api",
        repo_root.parent / "siel_logs" / "api",
        Path.cwd() / "logs" / "api",
        Path.cwd() / "siel_logs" / "api",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def cmd_unescape(value: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(value):
        if value[i] == "^" and i + 1 < len(value):
            out.append(value[i + 1])
            i += 2
        else:
            out.append(value[i])
            i += 1
    return "".join(out)


def split_curl_commands(text: str) -> list[str]:
    return [
        part.strip()
        for part in re.split(r"\s+&\s+(?=curl \^\"https?://)", text)
        if part.strip().startswith("curl ")
    ]


def curl_url(command: str) -> str | None:
    match = re.match(r"curl \^\"(.*?)\^\"", command, re.S)
    return cmd_unescape(match.group(1)) if match else None


def curl_data_raw(command: str) -> str | None:
    pos = command.find("--data-raw")
    if pos < 0:
        return None
    start = command.find("^\"", pos)
    end = command.rfind("^\"")
    if start < 0 or end <= start:
        return None
    data = cmd_unescape(command[start + 2 : end])
    return data.replace('\\"', '"')


def curl_headers(command: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    for match in re.finditer(r"-H \^\"(.*?)\^\"", command, re.S):
        header = cmd_unescape(match.group(1))
        if ":" not in header:
            continue
        key, value = header.split(":", 1)
        if key.lower() not in {"host", "content-length", "accept-encoding"}:
            headers[key] = value.strip()
    cookie = re.search(r"-b \^\"(.*?)\^\"", command, re.S)
    if cookie:
        headers["Cookie"] = cmd_unescape(cookie.group(1))
    headers.setdefault("Content-Type", "application/json")
    headers.setdefault("Accept", "*/*")
    return headers


def page_fetch_curl_commands(path: Path) -> list[tuple[int, str, str, dict[str, str]]]:
    commands = []
    for index, command in enumerate(split_curl_commands(read_text(path))):
        url = curl_url(command)
        data = curl_data_raw(command)
        if url and PAGE_FETCH in url and data:
            commands.append((index, url, data, curl_headers(command)))
    return commands


def page_fetch_har_requests(path: Path) -> list[tuple[int, dict[str, Any]]]:
    har = load_json(path)
    found = []
    for index, entry in enumerate(har.get("log", {}).get("entries", [])):
        request = entry.get("request") or {}
        if request.get("method") == "POST" and PAGE_FETCH in request.get("url", ""):
            found.append((index, request))
    return found


def fetch_json(url: str, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
    payload = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    context = None
    if os.environ.get("FPKT_API_INSECURE_SSL", "").lower() in {"1", "true", "yes", "y"}:
        context = ssl._create_unverified_context()
    timeout = int(os.environ.get("FPKT_API_TIMEOUT", "60"))
    with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def first_har_request_for_page(har_path: Path, page_number: int = 1) -> dict[str, Any]:
    fallback: dict[str, Any] | None = None
    for _index, request in page_fetch_har_requests(har_path):
        text = (request.get("postData") or {}).get("text") or "{}"
        try:
            body = json.loads(text)
        except json.JSONDecodeError:
            continue
        if fallback is None:
            fallback = request
        if (body.get("pageContext") or {}).get("pageNumber") == page_number:
            return request
    if fallback:
        return fallback
    raise ValueError(f"no page/fetch request in {har_path}")


def listing_page(
    har_path: Path,
    query: str,
    page: int,
    sort: str | None,
    previous_response: dict[str, Any] | None = None,
) -> dict[str, Any]:
    request = first_har_request_for_page(har_path, 1)
    body = json.loads((request.get("postData") or {}).get("text") or "{}")
    uri = (
        f"/search?q={query}&otracker=search&otracker1=search"
        f"&marketplace=FLIPKART&as-show=on&as=off"
    )
    if sort:
        uri += f"&sort={sort}"
    if page > 1:
        uri += f"&page={page}"
    body["pageUri"] = uri
    page_context = body.setdefault("pageContext", {})
    page_context["pageNumber"] = page
    page_context["paginatedFetch"] = page > 1
    page_context["fetchSeoData"] = True
    if page == 1:
        page_context["paginationContextMap"] = {}
    elif previous_response:
        page_context["paginationContextMap"] = (
            ((previous_response.get("RESPONSE") or {}).get("pageData") or {}).get("paginationContextMap")
            or page_context.get("paginationContextMap")
            or {}
        )
    return fetch_json(request["url"], request_headers(request), body)


def stage_listing(api_dir: Path, stage: str, query: str, max_pages: int) -> list[dict[str, Any]]:
    har_path = api_dir / "main_page2_page1_har.txt"
    if not har_path.exists():
        har_path = api_dir / "main_har.txt"
    sort = "popularity" if stage == "bsr" else None
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    previous = None
    for page in range(1, max_pages + 1):
        response = listing_page(har_path, query, page, sort, previous)
        page_rows = extract_products(response, page=page)
        for row in page_rows:
            key = row.get("product_id") or row.get("item_id") or row.get("product_url")
            row["stage"] = stage
            row["duplicate"] = bool(key in seen)
            if key:
                seen.add(key)
            rows.append(row)
        previous = response
    return rows


def doc_from_html(path: Path) -> html.HtmlElement:
    return html.fromstring(read_text(path))


def normalize_text(value: str | None) -> str | None:
    if value is None:
        return None
    value = re.sub(r"\s+", " ", value).strip()
    return value or None


def first_text(doc: html.HtmlElement, xpath: str) -> str | None:
    items = doc.xpath(xpath)
    if not items:
        return None
    item = items[0]
    if isinstance(item, str):
        return normalize_text(item)
    return normalize_text(" ".join(item.itertext()))


def product_ld(doc: html.HtmlElement) -> dict[str, Any]:
    for script in doc.xpath('//script[@type="application/ld+json"]'):
        raw = script.text_content().strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        candidates = data if isinstance(data, list) else [data]
        for item in candidates:
            if isinstance(item, dict) and item.get("aggregateRating") and item.get("offers"):
                return item
    return {}


def stage_detail(api_dir: Path) -> dict[str, Any]:
    doc = doc_from_html(api_dir / "detail_html.txt")
    ld = product_ld(doc)
    rating = ld.get("aggregateRating") or {}
    offers = ld.get("offers") or {}
    review_anchor = doc.xpath(
        '(//*[@id="slot-list-container"]/div/div[2]//a[contains(@href,"/product-reviews/") '
        'and not(contains(@href,"buynow")) and not(contains(@href,"&an="))])[1]/@href'
    )
    return {
        "retailer_sku_name": ld.get("name") or first_text(doc, "string(//h1[1])"),
        "fsn": ld.get("sku"),
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
        "review_url": ("https://www.flipkart.com" + review_anchor[0]) if review_anchor else None,
        "jsonld_review_count": len(ld.get("review") or []),
    }


def iter_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from iter_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_dicts(child)


def value_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, dict):
        if "text" in value:
            return value_text(value.get("text"))
        if "value" in value:
            return value_text(value.get("value"))
        return None
    if isinstance(value, list):
        parts = [value_text(item) for item in value]
        parts = [part for part in parts if part]
        return " ".join(parts) if parts else None
    text = normalize_text(str(value))
    return text or None


def review_row_from_value(value: dict[str, Any]) -> dict[str, Any] | None:
    rating = value.get("rating") or value.get("reviewRating") or value.get("ratingValue")
    text = (
        value_text(value.get("text"))
        or value_text(value.get("reviewText"))
        or value_text(value.get("description"))
        or value_text(value.get("content"))
    )
    title = (
        value_text(value.get("title"))
        or value_text(value.get("reviewTitle"))
        or value_text(value.get("heading"))
    )
    author = (
        value_text(value.get("author"))
        or value_text(value.get("userName"))
        or value_text(value.get("reviewerName"))
    )
    if rating in (None, "") or not text:
        return None
    if not (title or author or value.get("id")):
        return None
    return {
        "id": value.get("id") or value.get("reviewId"),
        "rating": rating,
        "title": title,
        "text": text,
        "author": author,
        "created": value.get("created") or value.get("createdAt") or value.get("date"),
        "helpful_count": value.get("helpfulCount"),
        "certified_buyer": value.get("certifiedBuyer"),
    }


def review_rows_from_response(response: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for slot in (response.get("RESPONSE") or {}).get("slots") or []:
        widget = slot.get("widget") or {}
        if widget.get("type") != "REVIEWS":
            continue
        components = (widget.get("data") or {}).get("renderableComponents") or []
        for component in components:
            value = component.get("value") or {}
            rows.append(
                {
                    "id": value.get("id"),
                    "rating": value.get("rating"),
                    "title": value.get("title"),
                    "text": value.get("text"),
                    "author": value.get("author"),
                    "created": value.get("created"),
                    "helpful_count": value.get("helpfulCount"),
                    "certified_buyer": value.get("certifiedBuyer"),
                }
            )
    if rows:
        return rows

    seen: set[str] = set()
    for value in iter_dicts(response):
        row = review_row_from_value(value)
        if not row:
            continue
        key = row.get("id") or f"{row.get('rating')}|{row.get('title')}|{row.get('text')}"
        if key in seen:
            continue
        seen.add(key)
        rows.append(row)
    return rows


def stage_review(api_dir: Path, max_pages: int) -> list[dict[str, Any]]:
    commands = page_fetch_curl_commands(api_dir / "review_curl.txt")
    if not commands:
        raise ValueError("review_curl.txt has no page/fetch request")
    _index, url, raw_body, headers = commands[0]
    base_body = json.loads(raw_body)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for page in range(1, max_pages + 1):
        body = json.loads(json.dumps(base_body))
        context = body.setdefault("pageContext", {})
        context["pageNumber"] = page
        context["paginatedFetch"] = page > 1
        context["paginationContextMap"] = {}
        response = fetch_json(url, headers, body)
        for row in review_rows_from_response(response):
            row["page"] = page
            row["duplicate"] = bool(row.get("id") in seen)
            if row.get("id"):
                seen.add(row["id"])
            rows.append(row)
    return rows


def print_listing(stage: str, rows: list[dict[str, Any]]) -> None:
    print(listing_summary(stage, rows)[0])
    for row in rows[:10]:
        safe_print(
            "{rank}\t{product_id}\t{star_rating}\t{count_of_star_ratings}\t"
            "{count_of_reviews}\t{final_price}\t{product_name}".format(**row)
        )


def print_detail(row: dict[str, Any]) -> None:
    safe_print("[detail] " + json.dumps(row, ensure_ascii=False))


def print_reviews(rows: list[dict[str, Any]]) -> None:
    print(review_summary(rows)[0])
    for index, row in enumerate(rows[:10], 1):
        text = normalize_text(row.get("text")) or ""
        safe_print(f"{index}\t{row.get('rating')}\t{row.get('title')}\t{text[:140]}")


def listing_summary(stage: str, rows: list[dict[str, Any]]) -> list[str]:
    ids = [row.get("product_id") for row in rows if row.get("product_id")]
    lines = [
        f"[{stage}] rows={len(rows)} unique={len(set(ids))} "
        f"duplicates={sum(1 for row in rows if row.get('duplicate'))} "
        f"missing_reviews={sum(row.get('count_of_reviews') in (None, '') for row in rows)}"
    ]
    for row in rows[:20]:
        lines.append(
            "{rank}\t{product_id}\t{star_rating}\t{count_of_star_ratings}\t"
            "{count_of_reviews}\t{final_price}\t{product_name}".format(**row)
        )
    return lines


def detail_summary(row: dict[str, Any]) -> list[str]:
    return ["[detail] " + json.dumps(row, ensure_ascii=False)]


def review_summary(rows: list[dict[str, Any]]) -> list[str]:
    lines = [
        f"[review] rows={len(rows)} unique={len({row.get('id') for row in rows if row.get('id')})} "
        f"duplicates={sum(1 for row in rows if row.get('duplicate'))}"
    ]
    for index, row in enumerate(rows[:20], 1):
        text = normalize_text(row.get("text")) or ""
        lines.append(f"{index}\t{row.get('rating')}\t{row.get('title')}\t{text[:180]}")
    return lines


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


def default_test_dir(query: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_query = re.sub(r"[^A-Za-z0-9_-]+", "_", query).strip("_") or "query"
    return Path(__file__).resolve().parent / "test_output" / f"{safe_query}_{stamp}"


def save_outputs(out_dir: Path, results: dict[str, Any], argv: list[str]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    summary: list[str] = []
    summary.append("command: " + " ".join(argv))
    summary.append("output_dir: " + str(out_dir))
    summary.append("")

    if "main" in results:
        write_csv(out_dir / "main.csv", results["main"])
        summary.extend(listing_summary("main", results["main"]))
        summary.append("")
    if "bsr" in results:
        write_csv(out_dir / "bsr.csv", results["bsr"])
        summary.extend(listing_summary("bsr", results["bsr"]))
        summary.append("")
    if "detail" in results:
        (out_dir / "detail.json").write_text(
            json.dumps(results["detail"], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        summary.extend(detail_summary(results["detail"]))
        summary.append("")
    if "review" in results:
        write_csv(out_dir / "review.csv", results["review"])
        summary.extend(review_summary(results["review"]))
        summary.append("")

    (out_dir / "summary.txt").write_text("\n".join(summary).rstrip() + "\n", encoding="utf-8")
    (out_dir / "command.txt").write_text(" ".join(argv) + "\n", encoding="utf-8")
    safe_print(f"[saved] {out_dir}")
    safe_print(f"[saved] {out_dir / 'summary.txt'}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-dir", type=Path, default=default_api_dir())
    parser.add_argument("--stage", choices=["main", "bsr", "detail", "review", "all"], default="all")
    parser.add_argument("--query", default="tv")
    parser.add_argument("--listing-pages", type=int, default=2)
    parser.add_argument("--review-pages", type=int, default=2)
    parser.add_argument("--save-test", action="store_true", help="Save summary/CSV/JSON under fpkt_api/test_output")
    parser.add_argument("--out-dir", type=Path, help="Directory for saved test outputs")
    parser.add_argument("--insecure", action="store_true", help="Disable SSL certificate verification for RDP/proxy tests")
    args = parser.parse_args()
    if args.insecure:
        os.environ["FPKT_API_INSECURE_SSL"] = "1"

    results: dict[str, Any] = {}
    if args.stage in {"main", "all"}:
        results["main"] = stage_listing(args.api_dir, "main", args.query, args.listing_pages)
        print_listing("main", results["main"])
    if args.stage in {"bsr", "all"}:
        results["bsr"] = stage_listing(args.api_dir, "bsr", args.query, args.listing_pages)
        print_listing("bsr", results["bsr"])
    if args.stage in {"detail", "all"}:
        results["detail"] = stage_detail(args.api_dir)
        print_detail(results["detail"])
    if args.stage in {"review", "all"}:
        results["review"] = stage_review(args.api_dir, args.review_pages)
        print_reviews(results["review"])
    if args.save_test or args.out_dir:
        save_outputs(args.out_dir or default_test_dir(args.query), results, sys.argv)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except urllib.error.HTTPError as exc:
        sys.stderr.write(f"HTTP {exc.code}: {exc.read(500).decode('utf-8', errors='replace')}\n")
        raise SystemExit(2)
