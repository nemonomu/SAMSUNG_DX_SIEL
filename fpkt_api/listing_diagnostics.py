"""Allow-listed evidence for API listing anomalies; never persist HAR/session data.

Browser evidence is a separate, fresh anonymous observation, not the historical
API session. Importing this module does not launch a browser or send requests.
"""
from __future__ import annotations

import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, unquote_plus, urlencode, urlsplit, urlunsplit


STOP_EXIT_CODE = 86  # The scheduler must not retry a diagnostic stop.
QUERY_FIELDS = {"q", "sort", "page", "sid", "marketplace", "as", "as-show",
                "otracker", "otracker1", "cacheFirst"}
HEADER_FIELDS = {"user-agent", "x-user-agent", "accept", "accept-language",
                 "content-type", "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform"}
COUNT_FIELDS = {"pageNumber", "pageSize", "page_size", "fetchSize", "size",
                "count", "productCount", "productsPerPage", "offset", "start", "end"}
BLOCKED_FIELD = re.compile(r"cookie|session|token|auth|secret|credential|password|private", re.I)
PID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def safe_url(value):
    parts = urlsplit(str(value or ""))
    # Reconstruct netloc without userinfo; only public search fields survive.
    host = parts.hostname or ""
    if parts.port:
        host += f":{parts.port}"
    query = urlencode([(k, v[:500]) for k, v in parse_qsl(parts.query)
                       if k in QUERY_FIELDS])
    return urlunsplit((parts.scheme, host, parts.path, query, ""))


def size_fields(value, path="", depth=0):
    """Persist numeric page controls only, never opaque pagination cursors."""
    found = {}
    if depth > 8 or not isinstance(value, dict):
        return found
    for key, child in value.items():
        if BLOCKED_FIELD.search(str(key)):
            continue
        name = f"{path}.{key}" if path else str(key)
        if key in COUNT_FIELDS and (child is None or isinstance(child, (int, float, bool))
                                    or isinstance(child, str) and child.isdigit()):
            found[name] = child
        elif isinstance(child, dict):
            found.update(size_fields(child, name, depth + 1))
    return found


def request_snapshot(url, headers, body):
    context = body.get("pageContext") or {}
    pagination = context.get("paginationContextMap") or {}
    return {
        "method": "POST", "endpoint": safe_url(url),
        "page_uri": safe_url(body.get("pageUri")),
        "headers": {k.lower(): str(v)[:500] for k, v in headers.items()
                    if k.lower() in HEADER_FIELDS},
        "page_controls": size_fields(body),
        "paginated_fetch": context.get("paginatedFetch") if isinstance(context.get("paginatedFetch"), bool) else None,
        "fetch_seo_data": context.get("fetchSeoData") if isinstance(context.get("fetchSeoData"), bool) else None,
        "pagination_context_present": bool(pagination),
        "pagination_numeric_controls": size_fields(pagination),
    }


def product_id(value):
    value = str(value or "")
    return value if PID.fullmatch(value) else None


def response_snapshot(response, products):
    root = response.get("RESPONSE") or {}
    page_data = root.get("pageData") or {}
    cards, widget_types, product_counts = [], {}, {}
    result = {"recorded_at": timestamp(),
              "api_status": response.get("STATUS_CODE") if isinstance(response.get("STATUS_CODE"), int) else None,
              "has_more_pages": page_data.get("hasMorePages") if isinstance(page_data.get("hasMorePages"), bool) else None,
              "server_start": None, "server_end": None, "server_total": None,
              "server_page": None, "server_total_pages": None}
    for slot in root.get("slots") or []:
        widget = slot.get("widget") or {}
        kind = widget.get("type")
        data = widget.get("data") or {}
        if kind:
            widget_types[str(kind)] = widget_types.get(str(kind), 0) + 1
        # Count all product arrays, including new widget types the parser skips.
        if data.get("products"):
            product_counts[str(kind)] = product_counts.get(str(kind), 0) + len(data["products"])
        for card in data.get("products") or []:
            info = card.get("productInfo") or {}
            value = info.get("value") or {}
            params = (info.get("action") or {}).get("params") or {}
            cards.append(product_id(value.get("id") or value.get("productId") or params.get("productId")))
        if kind == "FILTER_SORT_OPTIONS":
            for dest, source in (("server_start", "productStartIndex"), ("server_end", "productEndIndex"), ("server_total", "totalProducts")):
                value = data.get(source)
                result[dest] = value if isinstance(value, (int, float)) else None
            result["server_query"] = str(data.get("query") or "")[:500]
        elif kind == "PAGINATION_BAR":
            for dest, source in (("server_page", "currentPage"), ("server_total_pages", "totalPages")):
                value = data.get(source)
                result[dest] = value if isinstance(value, int) else None
    parsed_ids = [product_id(row.get("product_id")) for row in products]
    result.update(raw_product_count=len(cards), parsed_product_count=len(products),
                  raw_product_ids=cards, parsed_product_ids=parsed_ids,
                  parsed_unique_count=len({pid for pid in parsed_ids if pid}),
                  widget_types=widget_types, raw_product_counts_by_widget=product_counts)
    return result


def classify(evidence):
    raw, parsed = evidence["raw_product_count"], evidence["parsed_product_count"]
    if raw != parsed:
        return "parser_count_mismatch"
    if raw == 10:
        end, total = evidence.get("server_end"), evidence.get("server_total")
        if (evidence.get("has_more_pages") is False and isinstance(end, (int, float))
                and isinstance(total, (int, float)) and end >= total):
            return "last_page_ten_products"
        return "server_returned_ten_products"
    if evidence.get("parsed_unique_count", parsed) < parsed:
        return "duplicates_in_response"
    return "counts_match"


def changes(before, after, path=""):
    result = []
    if isinstance(before, dict) and isinstance(after, dict):
        for key in sorted(before.keys() | after.keys()):
            result.extend(changes(before.get(key), after.get(key), f"{path}.{key}" if path else key))
    elif before != after:
        result.append({"field": path, "before": before, "after": after})
    return result


def write_json(path, data):
    Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def record_page(out_dir, stage, evidence):
    folder = Path(out_dir) / "diagnostics"
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "listing_observations.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(evidence, ensure_ascii=False) + "\n")
    if evidence["raw_product_count"] == evidence["parsed_product_count"] == 24:
        # Latest baseline from this stage/run; never silently use another batch.
        write_json(folder / f"{stage}_baseline_24.json", evidence)


def browser_snapshot(page):
    """Visible DOM only. Exclude review/footer product links from the card count."""
    return page.evaluate("""() => {
      const text = document.body.innerText;
      const summary = text.match(/Showing\\s+[\\d,]+\\s*[–-]\\s*[\\d,]+\\s+of\\s+[\\d,]+[^\\n]*/i);
      const visible = [...document.querySelectorAll('a[href*="/p/"][href*="pid="]')]
        .filter(a => a.getClientRects().length && a.getBoundingClientRect().height > 0);
      const cards = visible.filter(a => /Add to Compare/i.test(a.innerText));
      const links = cards.length ? cards : visible;
      const ids = [...new Set(links.map(a => new URL(a.href).searchParams.get('pid')))];
      return {summary: summary ? summary[0] : null, dom_product_count: ids.length,
        count_method: cards.length ? 'compare_card_unique_pid' : 'visible_product_link_unique_pid_heuristic',
        product_ids: ids, title: document.title,
        blocked: /captcha|verify you are human|access denied/i.test(text),
        visible_text: text.slice(0, 80000)};
    }""")


def capture_browser(folder, query, stage, page_number):
    """One anonymous browser search and one real screenshot, bounded waits.

    No API cookies, persistent profile or storage state are imported. Browser
    traffic is projected through the same allow list as the API request.
    """
    from playwright.sync_api import sync_playwright
    from main_probe import extract_products

    folder = Path(folder)
    result = {"observation": "separate_anonymous_browser_search", "started_at": timestamp(),
              "requested_query": unquote_plus(query), "requested_stage": stage,
              "requested_page": page_number, "network": [], "errors": []}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context(viewport={"width": 1440, "height": 1000})
            page = context.new_page()
            page.set_default_timeout(15000)
            page.set_default_navigation_timeout(30000)

            def observe(response):
                if len(result["network"]) >= 12:
                    return
                parsed = urlsplit(response.url)
                if (not (parsed.hostname or "").endswith(".flipkart.com")
                        or "/api/4/page/fetch" not in parsed.path):
                    return
                try:
                    req = response.request
                    body = req.post_data_json or {}
                    uri = urlsplit(body.get("pageUri") or "")
                    params = dict(parse_qsl(uri.query))
                    if uri.path != "/search" or params.get("q") != unquote_plus(query):
                        return
                    payload = response.json()
                    result["network"].append({
                        "request": request_snapshot(req.url, req.headers, body),
                        "http_status": response.status,
                        "response": response_snapshot(payload, extract_products(payload)),
                    })
                except Exception as exc:
                    result["errors"].append("network_observation:" + type(exc).__name__)

            page.on("response", observe)
            try:
                page.goto("https://www.flipkart.com/", wait_until="domcontentloaded")
                field = page.locator('input[name="q"]').first
                field.fill(unquote_plus(query))
                field.press("Enter")
                page.wait_for_url("**/search?**", wait_until="domcontentloaded")
                result["search_entry_url"] = safe_url(page.url)
                # Retain browser-generated search parameters, set only stage/page.
                parts = urlsplit(page.url)
                params = dict(parse_qsl(parts.query))
                if stage == "bsr":
                    params["sort"] = "popularity"
                if page_number > 1:
                    params["page"] = str(page_number)
                target = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(params), ""))
                if stage == "bsr" or page_number > 1:
                    page.goto(target, wait_until="domcontentloaded")
                page.locator('a[href*="/p/"][href*="pid="]').first.wait_for(state="visible")
                result["initial"] = browser_snapshot(page)
                # One bounded scroll observation, not an unbounded collection loop.
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(2000)
                result["after_scroll"] = browser_snapshot(page)
                result["matching_search"] = (
                    dict(parse_qsl(urlsplit(page.url).query)).get("q") == unquote_plus(query))
            except Exception as exc:
                result["errors"].append("browser_search:" + type(exc).__name__)
            finally:
                result["final_url"] = safe_url(page.url)
                try:
                    page.evaluate("window.scrollTo(0, 0)")
                    page.screenshot(path=str(folder / "browser.png"), full_page=True, timeout=15000)
                    result["screenshot"] = "browser.png"
                except Exception as exc:
                    result["errors"].append("screenshot:" + type(exc).__name__)
                # Do not persist scripts, hidden inputs or embedded session state.
                for label in ("initial", "after_scroll"):
                    snapshot = result.get(label)
                    if snapshot:
                        text = snapshot.pop("visible_text", "")
                        (folder / f"browser_{label}.html").write_text(
                            '<meta charset="utf-8"><pre>' + html.escape(text) + "</pre>", encoding="utf-8")
        finally:
            browser.close()
    result["finished_at"] = timestamp()
    return result


def diagnose(out_dir, stage, page_number, query, evidence, baseline, compare=None):
    folder = Path(out_dir) / "diagnostics" / f"{stage}_page{page_number}_ten_products"
    folder.mkdir(parents=True, exist_ok=True)
    report = {"classification": classify(evidence), "api": evidence,
              "baseline_24": baseline, "request_changes_from_baseline": [],
              "api_comparisons": [], "browser": None, "errors": [],
              "decision": "stop_product_before_detail_and_db", "retryable": False}
    if baseline:
        report["request_changes_from_baseline"] = changes(baseline.get("request", {}), evidence.get("request", {}))
    # Persist the original evidence before any browser launch or comparison.
    write_json(folder / "report.json", report)
    if compare:
        try:
            report["api_comparisons"] = compare()
        except Exception as exc:
            report["errors"].append("api_comparison:" + type(exc).__name__)
    try:
        report["browser"] = capture_browser(folder, query, stage, page_number)
    except Exception as exc:
        report["errors"].append("browser:" + type(exc).__name__)
    comparisons = report["api_comparisons"]
    counts = [row.get("response", {}).get("raw_product_count") for row in comparisons]
    report["as_show_effect"] = (
        "10-24-10_pattern_observed_not_proof_of_server_policy"
        if counts == [10, 24, 10] else "not_established")
    browser = report["browser"] or {}
    before = browser.get("initial", {}).get("dom_product_count")
    after = browser.get("after_scroll", {}).get("dom_product_count")
    report["browser_loading_increase"] = before is not None and after is not None and after > before
    report["browser_api_request_differences"] = []
    for entry in browser.get("network", []):
        request = entry["request"]
        params = dict(parse_qsl(urlsplit(request["page_uri"]).query))
        number = request.get("page_controls", {}).get("pageContext.pageNumber") or params.get("page", "1")
        if str(number) == str(page_number) and (params.get("sort") == "popularity") == (stage == "bsr"):
            report["browser_api_request_differences"].append(changes(evidence.get("request", {}), request))
    report["limitation"] = "Browser is a separate anonymous session; server policy is not inferred from counts alone."
    write_json(folder / "report.json", report)
    summary = [
        "Flipkart API 10-product diagnostic / 10개 응답 진단", "",
        f"판정: {report['classification']}",
        f"API 원본={evidence['raw_product_count']} / 추출={evidence['parsed_product_count']} / 페이지 내 고유={evidence['parsed_unique_count']}",
        f"서버 범위={evidence.get('server_start')}~{evidence.get('server_end')} / 전체={evidence.get('server_total')}",
        f"정상 24개 비교 기준: {'있음 (같은 실행·단계)' if baseline else '없음'}",
        f"API 비교 순서: 원래 값 → as-show 변경 → 원래 값 / 원본 개수={counts}",
        f"as-show 영향: {report['as_show_effect']}",
        f"별도 브라우저 카드 수: 최초={before}, 스크롤 후={after}",
        f"브라우저 결과 표시: {browser.get('after_scroll', {}).get('summary')}",
        f"캡처: {browser.get('screenshot', '없음')}",
        f"진단 오류: {report['errors'] + browser.get('errors', [])}",
        "해당 품목 상세 수집·DB 적재 중단. 자동 재시도 제외.",
        "브라우저는 별도 익명 관측입니다. 서버 내부 정책은 이 자료만으로 단정하지 않습니다.",
        "요청 필드 차이와 상세 증거: report.json",
    ]
    (folder / "summary.txt").write_text("\n".join(summary) + "\n", encoding="utf-8")
    return str(folder)
