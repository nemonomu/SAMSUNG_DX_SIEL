"""Backfill only Flipkart Similar Products for an existing retail batch.

The script reads Flipkart rows from the existing ``dx_siel_*_retail_com``
tables, calls the same detail API/parser used by ``ops_test.py``, and updates
only ``retailer_sku_name_similar``.  Preview (dry-run) is the default; database
writes require the explicit ``--apply`` flag.

The value collected is the Similar Products shown at backfill time.  It is not
an historical reconstruction of what Flipkart showed when the batch ran.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import ssl
import sys
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
API_ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from ops_test import page_uri, similar_product_names, url_pid
from phase_probe import default_api_dir, page_fetch_curl_commands


PRODUCT_TABLES = {
    "hhp": "dx_siel_hhp_retail_com",
    "tv": "dx_siel_tv_retail_com",
    "ref": "dx_siel_ref_retail_com",
    "ldy": "dx_siel_ldy_retail_com",
}
ACCOUNT_NAME = "Flipkart"
COUNTRY = "SIEL"
SIMILAR_COLUMN = "retailer_sku_name_similar"


@dataclass(frozen=True)
class Candidate:
    product: str
    row_id: int
    item: str | None
    product_url: str | None
    current_value: str | None


@dataclass(frozen=True)
class DetailCapture:
    endpoint: str
    body_json: str
    headers: tuple[tuple[str, str], ...]


@dataclass
class CollectionResult:
    candidate: Candidate
    value: str | None
    status: str
    error: str | None = None
    cached: bool = False
    apply_status: str = "not_applicable"


@dataclass
class SelectionStats:
    matched_rows: int = 0
    eligible_rows: int = 0
    skipped_existing: int = 0
    skipped_no_url: int = 0
    skipped_invalid_url: int = 0
    skipped_missing_item: int = 0
    skipped_item_mismatch: int = 0
    duplicate_rows: int = 0
    limited_rows: int = 0


@dataclass
class RunSummary:
    selection: SelectionStats
    unique_fetches: int = 0
    resolved: int = 0
    no_similar: int = 0
    fetch_errors: int = 0
    not_attempted: int = 0
    planned_updates: int = 0
    updated_rows: int = 0
    concurrent_skips: int = 0


class AuditWriteAfterCommitError(RuntimeError):
    def __init__(self, updated_rows: int, output_dir: Path, cause: BaseException):
        super().__init__(
            f"DB COMMITTED ({updated_rows} rows), but audit output failed in "
            f"{output_dir}: {type(cause).__name__}: {cause}"
        )
        self.updated_rows = updated_rows
        self.output_dir = output_dir


def safe_print(value: str, *, file: Any = None) -> None:
    stream = file or sys.stdout
    encoding = getattr(stream, "encoding", None) or "utf-8"
    print(value.encode(encoding, errors="replace").decode(encoding, errors="replace"), file=stream, flush=True)


def table_for_product(product: str) -> str:
    try:
        return PRODUCT_TABLES[product.lower()]
    except (AttributeError, KeyError) as exc:
        raise ValueError(f"unsupported product: {product!r}") from exc


def selected_products(product: str) -> list[str]:
    table_for_product(product)
    return [product]


def validate_batch_id(value: str) -> str:
    batch_id = str(value or "").strip()
    if not batch_id:
        raise ValueError("batch_id must not be blank")
    if len(batch_id) > 200 or any(ord(char) < 32 for char in batch_id):
        raise ValueError("batch_id contains invalid characters")
    return batch_id


def env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(minimum, value)


def env_truthy(name: str) -> bool:
    return os.environ.get(name, "").lower() in {"1", "true", "yes", "y"}


def connect_db() -> Any:
    """Create a short-lived PostgreSQL connection using the project config."""
    try:
        import config
        import psycopg2
    except ImportError as exc:
        raise RuntimeError("config.py and psycopg2 are required on the RDP host") from exc

    cfg = dict(config.DB_CONFIG)
    cfg.setdefault("database", "postgres")
    cfg.setdefault("client_encoding", "utf8")
    cfg.setdefault("connect_timeout", env_int("SIEL_DB_CONNECT_TIMEOUT_SECONDS", 15, minimum=1))
    cfg.setdefault("application_name", "siel_fpkt_similar_backfill")

    lock_timeout_ms = env_int("SIEL_DB_LOCK_TIMEOUT_MS", 30000)
    statement_timeout_ms = env_int("SIEL_DB_STATEMENT_TIMEOUT_MS", 180000)
    options = str(cfg.get("options") or "").strip()
    option_parts = [options] if options else []
    if lock_timeout_ms:
        option_parts.append(f"-c lock_timeout={lock_timeout_ms}")
    if statement_timeout_ms:
        option_parts.append(f"-c statement_timeout={statement_timeout_ms}")
    if option_parts:
        cfg["options"] = " ".join(option_parts)
    return psycopg2.connect(**cfg)


def select_batch_rows(conn: Any, products: Iterable[str], batch_id: str) -> list[Candidate]:
    """Read exact-batch Flipkart rows without holding locks."""
    rows: list[Candidate] = []
    cursor = conn.cursor()
    try:
        for product in products:
            table = table_for_product(product)
            sql = f"""
                SELECT id, item, product_url, {SIMILAR_COLUMN}
                  FROM {table}
                 WHERE batch_id = %s
                   AND account_name = %s
                   AND UPPER(COALESCE(country, '')) = %s
                   AND UPPER(COALESCE(product, '')) = %s
                 ORDER BY id
            """
            cursor.execute(sql, (batch_id, ACCOUNT_NAME, COUNTRY, product.upper()))
            for row_id, item, product_url, current_value in cursor.fetchall():
                rows.append(
                    Candidate(
                        product=product,
                        row_id=int(row_id),
                        item=str(item).strip() if item not in (None, "") else None,
                        product_url=str(product_url).strip() if product_url not in (None, "") else None,
                        current_value=(
                            str(current_value).strip()
                            if current_value not in (None, "")
                            else None
                        ),
                    )
                )
    finally:
        cursor.close()
    return rows


def is_flipkart_url(value: str | None) -> bool:
    if not value:
        return False
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme.lower() == "https"
        and (host == "flipkart.com" or host.endswith(".flipkart.com"))
        and parsed.username is None
        and parsed.password is None
        and port in (None, 443)
        and "/p/" in parsed.path.lower()
        and url_pid(value)
    )


def prepare_candidates(
    rows: Iterable[Candidate],
    *,
    max_items: int = 0,
) -> tuple[list[Candidate], SelectionStats]:
    """Keep only blank Similar rows with safe product URLs, in stable order."""
    stats = SelectionStats()
    eligible: list[Candidate] = []
    seen_rows: set[tuple[str, int]] = set()

    for row in rows:
        stats.matched_rows += 1
        row_key = (row.product, row.row_id)
        if row_key in seen_rows:
            stats.duplicate_rows += 1
            continue
        seen_rows.add(row_key)
        if row.current_value and row.current_value.strip():
            stats.skipped_existing += 1
            continue
        if not row.product_url:
            stats.skipped_no_url += 1
            continue
        if not is_flipkart_url(row.product_url):
            stats.skipped_invalid_url += 1
            continue
        pid = url_pid(row.product_url)
        if not row.item:
            stats.skipped_missing_item += 1
            continue
        if not pid or row.item.upper() != pid.upper():
            stats.skipped_item_mismatch += 1
            continue
        eligible.append(row)

    stats.eligible_rows = len(eligible)
    if max_items > 0:
        allowed_keys: set[tuple[str, str]] = set()
        limited: list[Candidate] = []
        for row in eligible:
            key = candidate_fetch_key(row)
            if key not in allowed_keys and len(allowed_keys) >= max_items:
                stats.limited_rows += 1
                continue
            allowed_keys.add(key)
            limited.append(row)
        eligible = limited
    return eligible, stats


def candidate_fetch_key(candidate: Candidate) -> tuple[str, str]:
    product_url = candidate.product_url or ""
    identity = url_pid(product_url) or candidate.item or product_url
    return candidate.product, identity.upper()


def compact_error(exc: BaseException) -> str:
    text = re.sub(r"\s+", " ", f"{type(exc).__name__}: {exc}").strip()
    text = re.sub(
        r"(?i)\b(cookie|authorization|bearer|token)\b\s*[:=]?\s*[^\s,;]+",
        r"\1=<redacted>",
        text,
    )
    return text[:500]


def response_product_ids(response: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    response_root = response.get("RESPONSE")
    if not isinstance(response_root, dict):
        return ids
    page_data = response_root.get("pageData")
    if isinstance(page_data, dict):
        page_context = page_data.get("pageContext")
        if isinstance(page_context, dict):
            product_id = page_context.get("productId")
            if product_id not in (None, ""):
                ids.add(str(product_id).strip().upper())
    slots = response_root.get("slots")
    if isinstance(slots, list):
        for slot in slots:
            if not isinstance(slot, dict):
                continue
            widget = slot.get("widget")
            data = widget.get("data") if isinstance(widget, dict) else None
            parent = data.get("parentProduct") if isinstance(data, dict) else None
            value = parent.get("value") if isinstance(parent, dict) else None
            product_id = value.get("id") if isinstance(value, dict) else None
            if product_id not in (None, ""):
                ids.add(str(product_id).strip().upper())
    return ids


def response_page_product_id(response: dict[str, Any]) -> str | None:
    response_root = response.get("RESPONSE")
    page_data = response_root.get("pageData") if isinstance(response_root, dict) else None
    page_context = page_data.get("pageContext") if isinstance(page_data, dict) else None
    product_id = page_context.get("productId") if isinstance(page_context, dict) else None
    if product_id in (None, ""):
        return None
    return str(product_id).strip().upper()


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def fetch_json_without_redirects(
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
) -> dict[str, Any]:
    payload = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    handlers: list[Any] = [NoRedirectHandler()]
    if os.environ.get("FPKT_API_INSECURE_SSL", "").lower() in {"1", "true", "yes", "y"}:
        handlers.append(
            urllib.request.HTTPSHandler(context=ssl._create_unverified_context())
        )
    opener = urllib.request.build_opener(*handlers)
    timeout = int(os.environ.get("FPKT_API_TIMEOUT", "60"))
    with opener.open(request, timeout=timeout) as response:
        if response.geturl() != url:
            raise ValueError("Flipkart API redirect is not allowed")
        value = json.loads(response.read().decode("utf-8", errors="replace"))
    if not isinstance(value, dict):
        raise ValueError("Flipkart API response must be a JSON object")
    return value


def detail_response_from_capture(
    capture: DetailCapture,
    product_url: str,
) -> dict[str, Any]:
    body = json.loads(capture.body_json)
    body["pageUri"] = page_uri(product_url)
    context = body.setdefault("pageContext", {})
    context["pageNumber"] = 1
    context["paginatedFetch"] = False
    context["slotContextMap"] = {}
    context["paginationContextMap"] = {}
    return fetch_json_without_redirects(capture.endpoint, dict(capture.headers), body)


def fetch_similar_with_retries(
    candidate: Candidate,
    *,
    capture: DetailCapture,
    retries: int,
    retry_delay: float,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> str | None:
    last_exc: BaseException | None = None
    for attempt in range(max(retries, 0) + 1):
        try:
            requested_pid = url_pid(candidate.product_url)
            if not requested_pid:
                raise ValueError("candidate URL has no pid")
            response = detail_response_from_capture(capture, candidate.product_url or "")
            page_product_id = response_page_product_id(response)
            if not page_product_id:
                raise ValueError("detail response pageData has no productId")
            if page_product_id != requested_pid.upper():
                raise ValueError("detail response pageData productId does not match requested pid")
            response_ids = response_product_ids(response)
            if response_ids != {requested_pid.upper()}:
                raise ValueError("detail response contains mixed or mismatched productIds")
            return similar_product_names(response)
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            last_exc = exc
            if attempt < max(retries, 0) and retry_delay > 0:
                sleep_fn(retry_delay * (2**attempt))
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("detail fetch failed without an exception")


def collect_candidates(
    candidates: list[Candidate],
    fetch_fn: Callable[[Candidate], str | None],
    *,
    request_delay: float = 0.0,
    max_consecutive_errors: int = 5,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> tuple[list[CollectionResult], int]:
    """Fetch each unique product once while preserving every DB row id."""
    results: list[CollectionResult] = []
    cache: dict[tuple[str, str], tuple[str, str | None, str | None]] = {}
    unique_fetches = 0
    consecutive_errors = 0
    circuit_open = False

    for candidate in candidates:
        key = candidate_fetch_key(candidate)
        if key in cache:
            status, value, error = cache[key]
            results.append(CollectionResult(candidate, value, status, error, cached=True))
            continue
        if circuit_open:
            results.append(
                CollectionResult(
                    candidate,
                    None,
                    "not_attempted",
                    "stopped after consecutive API errors",
                )
            )
            continue
        if unique_fetches and request_delay > 0:
            sleep_fn(request_delay)
        unique_fetches += 1
        try:
            raw_value = fetch_fn(candidate)
            value = raw_value.strip() if isinstance(raw_value, str) else None
            if not value:
                value = None
            consecutive_errors = 0
            if value:
                status = "ready"
                error = None
            else:
                status = "no_similar"
                error = None
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            value = None
            status = "error"
            error = compact_error(exc)
            consecutive_errors += 1
            if max_consecutive_errors > 0 and consecutive_errors >= max_consecutive_errors:
                circuit_open = True
        cache[key] = (status, value, error)
        results.append(CollectionResult(candidate, value, status, error))
    return results, unique_fetches


def apply_updates(conn: Any, results: list[CollectionResult], batch_id: str) -> tuple[int, int]:
    """Update only still-empty Similar cells for the selected physical rows."""
    updated = 0
    concurrent_skips = 0
    cursor = conn.cursor()
    try:
        for result in results:
            if result.status != "ready" or not result.value:
                continue
            candidate = result.candidate
            table = table_for_product(candidate.product)
            sql = f"""
                UPDATE {table}
                   SET {SIMILAR_COLUMN} = %s
                 WHERE id = %s
                   AND batch_id = %s
                   AND account_name = %s
                   AND UPPER(COALESCE(country, '')) = %s
                   AND UPPER(COALESCE(product, '')) = %s
                   AND BTRIM(COALESCE(product_url, '')) = %s
                   AND UPPER(BTRIM(COALESCE(item, ''))) = %s
                   AND NULLIF(BTRIM({SIMILAR_COLUMN}), '') IS NULL
            """
            cursor.execute(
                sql,
                (
                    result.value,
                    candidate.row_id,
                    batch_id,
                    ACCOUNT_NAME,
                    COUNTRY,
                    candidate.product.upper(),
                    candidate.product_url,
                    (candidate.item or "").upper(),
                ),
            )
            if cursor.rowcount == 1:
                updated += 1
                result.apply_status = "updated"
            elif cursor.rowcount == 0:
                concurrent_skips += 1
                result.apply_status = "skipped_not_empty_or_changed"
            else:
                raise RuntimeError(
                    f"unexpected UPDATE rowcount={cursor.rowcount} for "
                    f"{candidate.product} id={candidate.row_id}"
                )
        cursor.close()
        cursor = None
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        if cursor is not None:
            cursor.close()
    return updated, concurrent_skips


def load_detail_capture(api_dir: Path) -> DetailCapture:
    capture_path = api_dir / "detail_curl.txt"
    commands = page_fetch_curl_commands(capture_path)
    if not commands:
        raise ValueError(f"{capture_path} has no page/fetch request")
    _index, endpoint, raw_body, headers = commands[0]
    parsed = urlparse(endpoint)
    host = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("detail_curl.txt endpoint has an invalid port") from exc
    if not (
        parsed.scheme.lower() == "https"
        and (host == "api.flipkart.com" or host.endswith(".api.flipkart.com"))
        and parsed.username is None
        and parsed.password is None
        and port in (None, 443)
        and parsed.path.rstrip("/") == "/api/4/page/fetch"
    ):
        raise ValueError(
            "detail_curl.txt endpoint must be HTTPS *.api.flipkart.com/api/4/page/fetch"
        )
    try:
        body = json.loads(raw_body)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("detail_curl.txt request body is not valid JSON") from exc
    if not isinstance(body, dict):
        raise ValueError("detail_curl.txt request body must be a JSON object")
    page_context = body.get("pageContext")
    if page_context is None:
        body["pageContext"] = {}
    elif not isinstance(page_context, dict):
        raise ValueError("detail_curl.txt pageContext must be a JSON object")
    normalized_body = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    return DetailCapture(endpoint, normalized_body, tuple(headers.items()))


def default_output_dir(product: str, batch_id: str) -> Path:
    safe_batch = re.sub(r"[^A-Za-z0-9_.-]+", "_", batch_id).strip("_") or "batch"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return Path(__file__).resolve().parent / "test_output" / f"similar_backfill_{product}_{safe_batch}_{stamp}"


def result_rows(results: Iterable[CollectionResult], batch_id: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for result in results:
        candidate = result.candidate
        similar_count = len(result.value.split(" ||| ")) if result.value else 0
        rows.append(
            {
                "product": candidate.product.upper(),
                "batch_id": batch_id,
                "id": candidate.row_id,
                "item": candidate.item,
                "product_url": candidate.product_url,
                "status": result.status,
                "apply_status": result.apply_status,
                "cached_fetch": result.cached,
                "similar_count": similar_count,
                "retailer_sku_name_similar": result.value,
                "error": result.error,
            }
        )
    return rows


def csv_safe(value: Any) -> Any:
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "product",
        "batch_id",
        "id",
        "item",
        "product_url",
        "status",
        "apply_status",
        "cached_fetch",
        "similar_count",
        "retailer_sku_name_similar",
        "error",
    ]
    temp_path = path.with_name(path.name + ".tmp")
    try:
        with temp_path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(
                {key: csv_safe(value) for key, value in row.items()} for row in rows
            )
        temp_path.replace(path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(path.name + ".tmp")
    try:
        temp_path.write_text(text, encoding="utf-8")
        temp_path.replace(path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def ensure_output_writable(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    probe = output_dir / f".similar_backfill_write_test_{os.getpid()}"
    try:
        probe.write_text("ok\n", encoding="utf-8")
    finally:
        if probe.exists():
            probe.unlink()


def summarize(selection: SelectionStats, results: list[CollectionResult], unique_fetches: int) -> RunSummary:
    summary = RunSummary(selection=selection, unique_fetches=unique_fetches)
    summary.resolved = sum(result.status == "ready" for result in results)
    summary.no_similar = sum(result.status == "no_similar" for result in results)
    summary.fetch_errors = sum(result.status == "error" for result in results)
    summary.not_attempted = sum(result.status == "not_attempted" for result in results)
    summary.planned_updates = summary.resolved
    return summary


def summary_lines(
    *,
    product: str,
    batch_id: str,
    apply: bool,
    summary: RunSummary,
    output_dir: Path,
) -> list[str]:
    selection = summary.selection
    return [
        f"mode={'APPLY' if apply else 'DRY-RUN'}",
        f"product={product}",
        f"batch_id={batch_id}",
        "meaning=current Similar Products at backfill time (not historical snapshot)",
        f"matched_rows={selection.matched_rows}",
        f"eligible_rows={selection.eligible_rows}",
        f"skipped_existing={selection.skipped_existing}",
        f"skipped_no_url={selection.skipped_no_url}",
        f"skipped_invalid_url={selection.skipped_invalid_url}",
        f"skipped_missing_item={selection.skipped_missing_item}",
        f"skipped_item_mismatch={selection.skipped_item_mismatch}",
        f"duplicate_rows={selection.duplicate_rows}",
        f"limited_rows={selection.limited_rows}",
        f"unique_fetches={summary.unique_fetches}",
        f"resolved={summary.resolved}",
        f"no_similar={summary.no_similar}",
        f"fetch_errors={summary.fetch_errors}",
        f"not_attempted={summary.not_attempted}",
        f"planned_updates={summary.planned_updates}",
        f"updated_rows={summary.updated_rows}",
        f"concurrent_skips={summary.concurrent_skips}",
        f"output_dir={output_dir}",
    ]


def write_audit_outputs(
    *,
    output_dir: Path,
    product: str,
    batch_id: str,
    apply: bool,
    summary: RunSummary,
    results: list[CollectionResult],
) -> None:
    write_csv(output_dir / "similar_backfill.csv", result_rows(results, batch_id))
    lines = summary_lines(
        product=product,
        batch_id=batch_id,
        apply=apply,
        summary=summary,
        output_dir=output_dir,
    )
    write_text_atomic(output_dir / "summary.txt", "\n".join(lines) + "\n")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill only Flipkart retailer_sku_name_similar for an existing batch"
    )
    parser.add_argument("--product", choices=list(PRODUCT_TABLES), required=True)
    parser.add_argument("--batch-id", required=True, help="Exact batch_id already stored in retail_com")
    parser.add_argument("--api-dir", type=Path, default=default_api_dir())
    parser.add_argument("--out-dir", type=Path)
    parser.add_argument(
        "--max-items",
        type=int,
        default=0,
        help="Unique-product cap for a smoke run; duplicate DB rows are preserved; 0=all",
    )
    parser.add_argument("--detail-retries", type=int, default=2)
    parser.add_argument("--retry-delay", type=float, default=2.0, help="Initial exponential retry delay in seconds")
    parser.add_argument("--request-delay", type=float, default=0.25, help="Delay between unique detail requests")
    parser.add_argument(
        "--max-consecutive-errors",
        type=int,
        default=5,
        help="Stop new requests after this many consecutive API errors; 0=disabled",
    )
    parser.add_argument("--api-timeout", type=int, default=90)
    parser.add_argument("--insecure", action="store_true")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="Commit Similar values to the existing rows")
    mode.add_argument("--dry-run", action="store_true", help="Preview only (default)")
    parser.add_argument(
        "--allow-insecure-apply",
        action="store_true",
        help="Explicitly allow --apply while TLS verification is disabled (not recommended)",
    )
    args = parser.parse_args(argv)
    if args.max_items < 0:
        parser.error("--max-items must be 0 or greater")
    if args.detail_retries < 0:
        parser.error("--detail-retries must be 0 or greater")
    if (
        not math.isfinite(args.retry_delay)
        or not math.isfinite(args.request_delay)
        or args.retry_delay < 0
        or args.request_delay < 0
    ):
        parser.error("delay values must be finite and 0 or greater")
    if args.max_consecutive_errors < 0:
        parser.error("--max-consecutive-errors must be 0 or greater")
    if args.api_timeout <= 0:
        parser.error("--api-timeout must be greater than 0")
    if args.allow_insecure_apply and not args.apply:
        parser.error("--allow-insecure-apply requires --apply")
    return args


def close_quietly(conn: Any) -> None:
    try:
        conn.close()
    except Exception:
        pass


def run(
    args: argparse.Namespace,
    *,
    connect_fn: Callable[[], Any] = connect_db,
    fetch_fn: Callable[[Candidate], str | None] | None = None,
) -> tuple[RunSummary, list[CollectionResult], Path, int]:
    batch_id = validate_batch_id(args.batch_id)
    products = selected_products(args.product)
    output_dir = args.out_dir or default_output_dir(args.product, batch_id)
    ensure_output_writable(output_dir)

    insecure_effective = args.insecure or env_truthy("FPKT_API_INSECURE_SSL")
    if args.apply and insecure_effective and not args.allow_insecure_apply:
        raise ValueError(
            "TLS verification is disabled; refuse --apply unless "
            "--allow-insecure-apply is explicitly supplied"
        )
    if args.insecure:
        os.environ["FPKT_API_INSECURE_SSL"] = "1"
    if args.api_timeout:
        os.environ["FPKT_API_TIMEOUT"] = str(args.api_timeout)
    if fetch_fn is None:
        capture = load_detail_capture(args.api_dir)

        def fetch_fn(candidate: Candidate) -> str | None:
            return fetch_similar_with_retries(
                candidate,
                capture=capture,
                retries=args.detail_retries,
                retry_delay=args.retry_delay,
            )

    read_conn = connect_fn()
    try:
        rows = select_batch_rows(read_conn, products, batch_id)
        read_conn.rollback()
    except BaseException:
        try:
            read_conn.rollback()
        finally:
            close_quietly(read_conn)
        raise
    close_quietly(read_conn)

    candidates, selection = prepare_candidates(rows, max_items=args.max_items)
    results, unique_fetches = collect_candidates(
        candidates,
        fetch_fn,
        request_delay=args.request_delay,
        max_consecutive_errors=args.max_consecutive_errors,
    )
    summary = summarize(selection, results, unique_fetches)

    for result in results:
        if result.status == "ready":
            result.apply_status = "planned_apply" if args.apply else "planned_dry_run"

    # Persist the complete plan before any commit.  If the final audit rewrite
    # fails after commit, this file still records every intended row/value.
    write_audit_outputs(
        output_dir=output_dir,
        product=args.product,
        batch_id=batch_id,
        apply=args.apply,
        summary=summary,
        results=results,
    )

    if args.apply and summary.planned_updates:
        write_conn = connect_fn()
        try:
            summary.updated_rows, summary.concurrent_skips = apply_updates(
                write_conn, results, batch_id
            )
        finally:
            close_quietly(write_conn)
        try:
            write_audit_outputs(
                output_dir=output_dir,
                product=args.product,
                batch_id=batch_id,
                apply=args.apply,
                summary=summary,
                results=results,
            )
        except Exception as exc:
            raise AuditWriteAfterCommitError(
                summary.updated_rows, output_dir, exc
            ) from exc

    partial = bool(
        selection.matched_rows == 0
        or summary.no_similar
        or summary.fetch_errors
        or summary.not_attempted
        or summary.concurrent_skips
        or selection.skipped_no_url
        or selection.skipped_invalid_url
        or selection.skipped_missing_item
        or selection.skipped_item_mismatch
    )
    return summary, results, output_dir, 2 if partial else 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if (
        args.apply
        and args.allow_insecure_apply
        and (args.insecure or env_truthy("FPKT_API_INSECURE_SSL"))
    ):
        safe_print(
            "[similar_backfill] WARNING: APPLY is running without TLS certificate verification",
            file=sys.stderr,
        )
    try:
        summary, _results, output_dir, exit_code = run(args)
    except AuditWriteAfterCommitError as exc:
        safe_print(f"[similar_backfill] {compact_error(exc)}", file=sys.stderr)
        safe_print(
            "[similar_backfill] IMPORTANT: the DB commit succeeded; do not assume rollback",
            file=sys.stderr,
        )
        return 3
    except Exception as exc:
        safe_print(f"[similar_backfill] FAIL: {compact_error(exc)}", file=sys.stderr)
        return 1

    mode = "APPLY" if args.apply else "DRY-RUN"
    safe_print(
        f"[similar_backfill] {mode} matched={summary.selection.matched_rows} "
        f"eligible={summary.selection.eligible_rows} resolved={summary.resolved} "
        f"updated={summary.updated_rows} unresolved="
        f"{summary.no_similar + summary.fetch_errors + summary.not_attempted}"
    )
    safe_print(f"[saved] {output_dir / 'similar_backfill.csv'}")
    safe_print(f"[saved] {output_dir / 'summary.txt'}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
