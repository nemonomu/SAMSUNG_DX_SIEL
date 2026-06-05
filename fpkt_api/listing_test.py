"""Listing-only Flipkart API probe.

This is intentionally narrower than ops_test.py: it only replays main/bsr
listing GraphQL calls and records page-level success/failure so transient
timeouts can be isolated without detail, review, or DB insert work.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from main_probe import extract_products
from phase_probe import default_api_dir, listing_page


PRODUCT_QUERY = {
    "tv": "tv",
    "hhp": "smartphone",
    "ref": "refrigerator",
    "ldy": "washing machine",
}


def safe_print(value: str) -> None:
    encoding = sys.stdout.encoding or "utf-8"
    print(value.encode(encoding, errors="replace").decode(encoding, errors="replace"), flush=True)


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
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=csv_fieldnames(rows))
        writer.writeheader()
        writer.writerows(rows)


def output_dir(product: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path(__file__).resolve().parent / "test_output" / f"listing_{product}_{stamp}"


def har_path(api_dir: Path) -> Path:
    page2 = api_dir / "main_page2_page1_har.txt"
    if page2.exists():
        return page2
    return api_dir / "main_har.txt"


def url_pid(value: Any) -> str | None:
    match = re.search(r"[?&]pid=([A-Z0-9]+)", str(value or ""))
    return match.group(1) if match else None


def row_key(row: dict[str, Any]) -> str | None:
    return (
        url_pid(row.get("product_url"))
        or url_pid(row.get("source_url"))
        or url_pid(row.get("url"))
        or row.get("product_id")
        or row.get("item_id")
        or row.get("product_url")
    )


def run_stage(
    api_dir: Path,
    stage: str,
    query: str,
    target_unique: int,
    max_pages: int,
    retries: int,
    retry_delay: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    raw_rows: list[dict[str, Any]] = []
    final_rows: list[dict[str, Any]] = []
    page_results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    seen: set[str] = set()
    previous_response: dict[str, Any] | None = None
    sort = "popularity" if stage == "bsr" else None
    path = har_path(api_dir)

    for page in range(1, max_pages + 1):
        for attempt in range(1, retries + 2):
            started = time.perf_counter()
            try:
                response = listing_page(path, query, page, sort, previous_response)
                elapsed = time.perf_counter() - started
                previous_response = response
                page_rows = extract_products(response, page=page)
                page_new = 0
                page_dupes = 0
                for row in page_rows:
                    key = row_key(row)
                    raw = dict(row)
                    raw["stage"] = stage
                    raw["page"] = page
                    raw["duplicate"] = bool(key in seen)
                    raw_rows.append(raw)
                    if key in seen:
                        page_dupes += 1
                        continue
                    if key:
                        seen.add(str(key))
                    final = dict(raw)
                    final["unique_rank"] = len(final_rows) + 1
                    final[f"{stage}_rank"] = final["unique_rank"]
                    final_rows.append(final)
                    page_new += 1

                page_results.append(
                    {
                        "stage": stage,
                        "page": page,
                        "status": "ok",
                        "attempt": attempt,
                        "elapsed_seconds": f"{elapsed:.3f}",
                        "page_rows": len(page_rows),
                        "page_new_unique": page_new,
                        "page_duplicates": page_dupes,
                        "total_unique": len(final_rows),
                        "error": "",
                    }
                )
                safe_print(
                    f"[listing_test] {stage} page={page} ok rows={len(page_rows)} "
                    f"new={page_new} unique={len(final_rows)} elapsed={elapsed:.1f}s"
                )
                break
            except Exception as exc:
                elapsed = time.perf_counter() - started
                error_row = {
                    "stage": stage,
                    "page": page,
                    "status": "error",
                    "attempt": attempt,
                    "elapsed_seconds": f"{elapsed:.3f}",
                    "page_rows": "",
                    "page_new_unique": "",
                    "page_duplicates": "",
                    "total_unique": len(final_rows),
                    "error": repr(exc),
                    "traceback": traceback.format_exc(),
                }
                if attempt <= retries + 1 and attempt <= retries:
                    safe_print(
                        f"[listing_test] {stage} page={page} retry "
                        f"attempt={attempt}/{retries + 1} error={repr(exc)}"
                    )
                    time.sleep(retry_delay)
                    continue
                page_results.append(error_row)
                errors.append(error_row)
                safe_print(f"[listing_test] {stage} page={page} error={repr(exc)}")
                return raw_rows, final_rows, page_results, errors

        if len(final_rows) >= target_unique:
            break

    return raw_rows, final_rows, page_results, errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Flipkart listing-only API test")
    parser.add_argument("--api-dir", type=Path, default=default_api_dir())
    parser.add_argument("--product", choices=sorted(PRODUCT_QUERY), default="tv")
    parser.add_argument("--query", default=None)
    parser.add_argument("--main-target", type=int, default=300)
    parser.add_argument("--bsr-target", type=int, default=100)
    parser.add_argument("--max-pages-main", type=int, default=30)
    parser.add_argument("--max-pages-bsr", type=int, default=15)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--retry-delay", type=float, default=5.0)
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--insecure", action="store_true")
    parser.add_argument("--out-dir", type=Path, default=None)
    args = parser.parse_args()

    if args.insecure:
        os.environ["FPKT_API_INSECURE_SSL"] = "1"
    os.environ["FPKT_API_TIMEOUT"] = str(args.timeout)

    query = args.query or PRODUCT_QUERY[args.product]
    out_dir = args.out_dir or output_dir(args.product)
    out_dir.mkdir(parents=True, exist_ok=True)

    safe_print(f"[listing_test] product={args.product} query={query}")
    safe_print(f"[listing_test] api_dir={args.api_dir}")
    safe_print(f"[listing_test] output_dir={out_dir}")
    safe_print(f"[listing_test] timeout={args.timeout} retries={args.retries}")

    main_raw, main_final, main_pages, main_errors = run_stage(
        args.api_dir, "main", query, args.main_target, args.max_pages_main, args.retries, args.retry_delay
    )
    bsr_raw, bsr_final, bsr_pages, bsr_errors = run_stage(
        args.api_dir, "bsr", query, args.bsr_target, args.max_pages_bsr, args.retries, args.retry_delay
    )

    page_results = main_pages + bsr_pages
    errors = main_errors + bsr_errors
    write_csv(out_dir / "page_results.csv", page_results)
    write_csv(out_dir / "listing_errors.csv", errors)
    write_csv(out_dir / "main_raw.csv", main_raw)
    write_csv(out_dir / "main_final.csv", main_final)
    write_csv(out_dir / "bsr_raw.csv", bsr_raw)
    write_csv(out_dir / "bsr_final.csv", bsr_final)

    lines = [
        "command: " + " ".join(sys.argv),
        f"api_dir: {args.api_dir}",
        f"output_dir: {out_dir}",
        f"product: {args.product}",
        f"query: {query}",
        f"timeout: {args.timeout}",
        f"retries: {args.retries}",
        "",
        f"[main] unique={len(main_final)} raw={len(main_raw)} pages_ok={sum(1 for row in main_pages if row.get('status') == 'ok')} errors={len(main_errors)}",
        f"[bsr] unique={len(bsr_final)} raw={len(bsr_raw)} pages_ok={sum(1 for row in bsr_pages if row.get('status') == 'ok')} errors={len(bsr_errors)}",
        "",
        "files:",
        f"- {out_dir / 'page_results.csv'}",
        f"- {out_dir / 'listing_errors.csv'}",
        f"- {out_dir / 'main_final.csv'}",
        f"- {out_dir / 'bsr_final.csv'}",
    ]
    if errors:
        lines.append("")
        lines.append("errors:")
        for row in errors:
            lines.append(f"- {row.get('stage')} page={row.get('page')} {row.get('error')}")
    (out_dir / "summary.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out_dir / "command.txt").write_text(" ".join(sys.argv) + "\n", encoding="utf-8")

    safe_print(f"[listing_test] main_unique={len(main_final)} bsr_unique={len(bsr_final)} errors={len(errors)}")
    safe_print(f"[listing_test] saved={out_dir}")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
