"""SIEL Amazon 크롤링 결과 이메일 발송 — fpkt_api/ops_test.py 의 send_email_report 패턴 이식.

조건부 URL 포함 (per-record):
  1) final_sku_price >= original_sku_price (가격 역전/동일)
  2) sku is null
  3) redirect is True (ASIN mismatch)

Cross-field 논리 모순 (per-record):
  4) count_of_star_ratings >= 1 인데 star_rating null
  5) count_of_reviews        >= 1 인데 detailed_review_content null

Aggregate (XPath drift 신호):
  6) detail record 전체에서 어떤 field 값이 100% null → XPath/layout 변경 의심

main 단계 가격 (HHP) 은 jsonl 의 stage=main 레코드를 ASIN 으로 merge 하여 detail 보정.
"""
from __future__ import annotations

import json
import os
import re
import smtplib
import sys
from email.message import EmailMessage
from pathlib import Path
from typing import Any

_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    from siel_log import parse_price as _parse_price  # type: ignore
except Exception:
    _parse_price = None

try:
    from siel_log import parse_int_field as _parse_int_field  # type: ignore
except Exception:
    _parse_int_field = None


def _parse_price_safe(v):
    if _parse_price is not None:
        try:
            return _parse_price(v)
        except Exception:
            pass
    if v is None:
        return None
    try:
        s = str(v).replace(',', '')
        m = re.search(r'\d+(?:\.\d+)?', s)
        return float(m.group(0)) if m else None
    except Exception:
        return None


def _parse_int_safe(v):
    if _parse_int_field is not None:
        try:
            return _parse_int_field(v)
        except Exception:
            pass
    if v is None:
        return None
    try:
        s = str(v).replace(',', '')
        m = re.search(r'\d+', s)
        return int(m.group(0)) if m else None
    except Exception:
        return None


# metadata / 운영 식별자 — drift 검사 대상에서 제외
_METADATA_KEYS = {
    'account_name', 'product', 'stage', 'company', 'division',
    'source_url', 'asin', 'item', 'batch_id', 'crawl_datetime',
    'redirect', 'landing_url', 'landing_asin',
    'main_rank', 'bsr_rank', 'page_no',
}

# 형(shape) 화이트리스트 — XPath drift 시 파서가 엉뚱한 값 (예: 'Bestseller', '5 results')
# 을 추출해도 null 이 아니라 통과하는 경우 잡기 위함. 값이 null/빈문자열이면 검사 X
# (그건 별도 null/drift 검사가 담당). 패턴 미일치 시 형 위반.
_FIELD_PATTERNS = {
    # parse_star_rating → '4.3' / '4' / None
    'star_rating': re.compile(r'^\d+(?:\.\d+)?$'),
    # parse_count_of_ratings → '6,743' (콤마 허용). 정수 표기도 허용.
    'count_of_star_ratings': re.compile(r'^\d[\d,]*$'),
    # 정수형 (raw or post-parse). 콤마 허용.
    'count_of_reviews': re.compile(r'^\d[\d,]*$'),
    # parse_amzn_apex_price → '₹11,990' / 소수점 허용. main raw 는 ₹ 없을 수도 있어 prefix optional.
    'final_sku_price': re.compile(r'^₹?\d[\d,]*(?:\.\d+)?$'),
    'original_sku_price': re.compile(r'^₹?\d[\d,]*(?:\.\d+)?$'),
}


# 정상 추출 sentinel — siel_log.parse_amzn_apex_price + detail.py No-reviews 후처리에서
# 의도적으로 통과시키는 값. 형 검사에서 통과시켜 false positive 방지.
_PRICE_SENTINELS = (
    'Currently unavailable',
    'No featured offers',
    'See price in cart',
    'Temporarily out of stock',
    'Price higher than typical',
)
_STAR_SENTINELS = ('No customer reviews',)


def _check_field_pattern(field: str, value) -> bool:
    """value 가 field 의 형 패턴과 일치하는지. null 은 항상 True (별 검사가 담당).
    정상 sentinel (재고 부재 / 리뷰 0건) 도 통과.
    """
    pat = _FIELD_PATTERNS.get(field)
    if pat is None:
        return True
    if value in (None, ''):
        return True
    s = str(value).strip()
    if field in ('final_sku_price', 'original_sku_price'):
        if any(sen in s for sen in _PRICE_SENTINELS):
            return True
    if field == 'star_rating':
        if any(sen in s for sen in _STAR_SENTINELS):
            return True
    return bool(pat.match(s))


def email_config_value(cfg: dict, *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = cfg.get(key)
        if value not in (None, ''):
            return value
    return default


def email_recipients(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in re.split(r'[;,]', value) if part.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(part).strip() for part in value if str(part).strip()]
    return [str(value).strip()]


# 제품군별 valid-NULL field — 페이지 layout 상 정보 자체가 없는 의도된 NULL.
# all_null_fields 검사에서 제외해 false positive 회피.
# HHP (인도 모바일): 'fastest delivery' 표기 없음, 'Amazon Fulfilled' badge 별도 노출 없음
# (URL param isAmazonFulfilled 로만 확인 가능). 5/29 HHP 362건 100% NULL 검증.
_VALID_NULL_BY_PRODUCT = {
    'HHP': {'fastest_delivery', 'sku_assurance'},
    'TV': set(),
    'REF': set(),
    'LDY': set(),
}


def collect_url_issues(jsonl_path: str, product: str = '') -> tuple[dict, int]:
    """jsonl 을 1 pass 로 읽어 main(가격 보정) + detail 검사.
    return: (issues, detail_count)
      issues:
        - redirect: [url, ...]
        - sku_null: [url, ...]
        - price_inversion: [{url, final, original}, ...]
        - rating_count_no_rating: [{url, count_of_star_ratings}, ...]
        - review_count_no_review_text: [{url, count_of_reviews}, ...]
        - all_null_fields: [{field, total}, ...]  # 전 detail record 가 null → XPath drift
    """
    issues: dict = {
        'redirect': [],
        'sku_null': [],
        'price_inversion': [],
        'rating_count_no_rating': [],
        'review_count_no_review_text': [],
        'all_null_fields': [],
        'type_mismatch': [],
        'run_error': [],
        'detail_zero': [],
        'stage_counts': {},
        'stage_summaries': {},
        'listing_page_failure': [],
        'db_insert_summary': None,
        'db_insert_zero': [],
    }
    if not jsonl_path or not os.path.exists(jsonl_path):
        return issues, 0

    main_by_key: dict = {}
    detail_recs: list = []
    with open(jsonl_path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            stage = rec.get('stage')
            if stage:
                issues['stage_counts'][stage] = issues['stage_counts'].get(stage, 0) + 1
            if rec.get('_error'):
                error_stage = rec.get('error_stage') or stage or 'unknown'
                is_listing_page_failure = (
                    error_stage in ('main', 'bsr')
                    and rec.get('page_no') not in (None, '')
                    and rec.get('_error') in (
                        'listing page load failed',
                        'listing page has no cards',
                    )
                )
                if is_listing_page_failure:
                    issues['listing_page_failure'].append({
                        'stage': error_stage,
                        'page_no': rec.get('page_no'),
                        'url': rec.get('source_url') or rec.get('product_url') or '',
                        'message': rec.get('message') or rec.get('_error'),
                    })
                else:
                    issues['run_error'].append({
                        'stage': error_stage,
                        'message': rec.get('message') or rec.get('_error'),
                    })
            if rec.get('_summary') and rec.get('summary_stage'):
                summary_stage = rec.get('summary_stage')
                issues['stage_summaries'][summary_stage] = rec
            if stage == 'db_insert_summary':
                issues['db_insert_summary'] = rec
            if stage == 'main':
                key = rec.get('asin')
                if key:
                    main_by_key.setdefault(key, rec)
            elif stage == 'detail':
                detail_recs.append(rec)

    # per-record 검사
    detail_count = 0
    valid_recs: list = []  # _detail_skip 제외 (본문 미수집 → drift 검사에서 제거)
    for rec in detail_recs:
        detail_count += 1
        url = rec.get('source_url') or rec.get('product_url') or ''
        asin = rec.get('asin')

        if rec.get('redirect') is True:
            issues['redirect'].append(url)
            # redirect 시 detail 본문 신뢰 X → 이하 모든 본문 검사 스킵
            continue
        if rec.get('_detail_skip'):
            continue
        valid_recs.append(rec)

        if rec.get('sku') in (None, ''):
            issues['sku_null'].append(url)

        fsp = rec.get('final_sku_price')
        osp = rec.get('original_sku_price')
        # HHP 등 가격이 main 에 있는 경우 보정
        m = main_by_key.get(asin) or {}
        if fsp in (None, ''):
            fsp = m.get('final_sku_price')
        if osp in (None, ''):
            osp = m.get('original_sku_price')
        fpv = _parse_price_safe(fsp)
        opv = _parse_price_safe(osp)
        if fpv is not None and opv is not None and fpv >= opv:
            issues['price_inversion'].append({
                'url': url, 'final': fsp, 'original': osp,
            })

        # cross-field 논리 모순 — rating count 있는데 star_rating 없음
        cor = _parse_int_safe(rec.get('count_of_star_ratings'))
        if cor is not None and cor >= 1 and rec.get('star_rating') in (None, ''):
            issues['rating_count_no_rating'].append({
                'url': url, 'count_of_star_ratings': rec.get('count_of_star_ratings'),
            })
        # cross-field — review 개수 있는데 review 본문 없음
        crv = _parse_int_safe(rec.get('count_of_reviews'))
        if crv is not None and crv >= 1 and rec.get('detailed_review_content') in (None, ''):
            issues['review_count_no_review_text'].append({
                'url': url, 'count_of_reviews': rec.get('count_of_reviews'),
            })

        # 형(shape) 화이트리스트 — null 아닌데 패턴 미일치 → XPath drift 가 엉뚱한 값 추출 의심
        for field in _FIELD_PATTERNS:
            v = rec.get(field)
            if v in (None, ''):
                continue
            if not _check_field_pattern(field, v):
                issues['type_mismatch'].append({
                    'url': url, 'field': field, 'value': v,
                })

    # aggregate — '전부 null' field 감지 (XPath drift 신호)
    # 검사 대상: valid_recs 에 한 번이라도 등장한 non-metadata field 중
    # 모든 record 에서 None/빈 문자열 인 것 (최소 2건 이상이어야 의미).
    if len(valid_recs) >= 2:
        field_counts: dict = {}
        for rec in valid_recs:
            for k, v in rec.items():
                if k in _METADATA_KEYS or k.startswith('_'):
                    continue
                slot = field_counts.setdefault(k, [0, 0])
                slot[0] += int(v not in (None, ''))
                slot[1] += 1
        valid_null_set = _VALID_NULL_BY_PRODUCT.get((product or '').upper(), set())
        for k, (non_null, total) in sorted(field_counts.items()):
            if k in valid_null_set:
                continue
            if total >= 2 and non_null == 0:
                issues['all_null_fields'].append({'field': k, 'total': total})

    if detail_count == 0:
        issues['detail_zero'].append({
            'main_records': issues['stage_counts'].get('main', 0),
            'bsr_records': issues['stage_counts'].get('bsr', 0),
        })
    db_insert_summary = issues.get('db_insert_summary') or {}
    if db_insert_summary:
        inserted_total = db_insert_summary.get('inserted_total')
        returncode = db_insert_summary.get('returncode')
        inserted_int = None
        try:
            inserted_int = int(inserted_total)
        except Exception:
            pass
        if inserted_int == 0 or (returncode not in (None, 0) and not inserted_int):
            issues['db_insert_zero'].append({
                'inserted_total': inserted_total,
                'returncode': returncode,
                'rows_full': db_insert_summary.get('rows_full'),
                'rows_listing': db_insert_summary.get('rows_listing'),
                'message': db_insert_summary.get('message'),
            })
    elif detail_count == 0:
        issues['db_insert_zero'].append({
            'inserted_total': None,
            'returncode': None,
            'rows_full': None,
            'rows_listing': None,
            'message': 'detail records = 0 and no db_insert_summary found',
        })

    return issues, detail_count


def build_email_report_with_severity(product: str, jsonl_path: str) -> tuple[str, str]:
    issues, detail_count = collect_url_issues(jsonl_path, product)
    redirects = issues['redirect']
    sku_nulls = issues['sku_null']
    price_inv = issues['price_inversion']
    rating_mis = issues['rating_count_no_rating']
    review_mis = issues['review_count_no_review_text']
    all_null = issues['all_null_fields']
    type_mis = issues['type_mismatch']
    run_errors = issues.get('run_error', [])
    detail_zero = issues.get('detail_zero', [])
    stage_counts = issues.get('stage_counts', {})
    listing_page_failures = issues.get('listing_page_failure', [])
    db_insert_zero = issues.get('db_insert_zero', [])
    db_insert_summary = issues.get('db_insert_summary') or {}
    has_warning = bool(
        redirects or sku_nulls or price_inv
        or rating_mis or review_mis or all_null or type_mis
        or run_errors or detail_zero or listing_page_failures
    )
    has_sos = bool(db_insert_zero)
    severity = 'sos' if has_sos else ('warning' if has_warning else 'ok')

    lines = [
        f'product: {product.upper()}',
        f"main records: {stage_counts.get('main', 0)}",
        f"bsr records: {stage_counts.get('bsr', 0)}",
        f'detail records: {detail_count}',
        '',
    ]
    if db_insert_summary:
        lines.insert(-1, f"db insert rows: {db_insert_summary.get('inserted_total')}")
    if severity == 'ok':
        lines.append('특이사항 없음')
        return '\n'.join(lines) + '\n', severity

    lines.append('SOS' if severity == 'sos' else 'WARNING')
    if db_insert_zero:
        item = db_insert_zero[0]
        lines.append(
            '- db insert rows = 0 '
            f"(returncode={item.get('returncode')}, "
            f"rows_full={item.get('rows_full')}, rows_listing={item.get('rows_listing')})"
        )
        if item.get('message'):
            lines.append(f"  - reason={item.get('message')}")
    if detail_zero:
        item = detail_zero[0]
        lines.append(
            '- detail records = 0 '
            f"(main={item.get('main_records', 0)}, bsr={item.get('bsr_records', 0)})"
        )
    if run_errors:
        lines.append(f'- run errors: {len(run_errors)}건')
        for item in run_errors[:20]:
            lines.append(f"  - stage={item.get('stage')} message={item.get('message')}")
    if listing_page_failures:
        lines.append(f'- listing page failures: {len(listing_page_failures)}건')
        for item in listing_page_failures[:20]:
            lines.append(
                f"  - {item.get('stage')} page={item.get('page_no')} "
                f"url={item.get('url')} reason={item.get('message')}"
            )
    if redirects:
        lines.append(f'- redirect=true: {len(redirects)}건')
        for u in redirects:
            lines.append(f'  - {u}')
    if sku_nulls:
        lines.append(f'- sku null: {len(sku_nulls)}건')
        for u in sku_nulls:
            lines.append(f'  - {u}')
    if price_inv:
        lines.append(f'- price inversion (final >= original): {len(price_inv)}건')
        for item in price_inv:
            lines.append(
                f"  - {item['url']} (final={item['final']}, original={item['original']})"
            )
    if rating_mis:
        lines.append(
            f'- count_of_star_ratings>=1 but star_rating null: {len(rating_mis)}건'
        )
        for item in rating_mis:
            lines.append(
                f"  - {item['url']} (count_of_star_ratings={item['count_of_star_ratings']})"
            )
    if review_mis:
        lines.append(
            f'- count_of_reviews>=1 but detailed_review_content null: {len(review_mis)}건'
        )
        for item in review_mis:
            lines.append(
                f"  - {item['url']} (count_of_reviews={item['count_of_reviews']})"
            )
    if all_null:
        lines.append(
            f'- detail record 전부 null field (XPath/layout drift 의심): {len(all_null)}건'
        )
        for item in all_null:
            lines.append(f"  - {item['field']} (total={item['total']})")
    if type_mis:
        lines.append(
            f'- field 형(shape) 불일치 (XPath drift 가 엉뚱한 값 추출 의심): {len(type_mis)}건'
        )
        for item in type_mis:
            v = item['value']
            v_disp = str(v) if len(str(v)) <= 80 else str(v)[:80] + '...'
            lines.append(f"  - {item['url']} field={item['field']} value={v_disp!r}")
    return '\n'.join(lines) + '\n', severity


def build_email_report(product: str, jsonl_path: str) -> tuple[str, bool]:
    body, severity = build_email_report_with_severity(product, jsonl_path)
    return body, severity != 'ok'


def send_email_report(subject: str, body: str) -> tuple[bool, list]:
    try:
        import config  # type: ignore
    except Exception as exc:
        return False, [f'[email] skipped: config import failed: {repr(exc)}']

    cfg = dict(getattr(config, 'EMAIL_CONFIG', {}) or {})
    server = email_config_value(cfg, 'smtp_server', 'smtp_host', 'host')
    port = int(email_config_value(cfg, 'smtp_port', 'port', default=587))
    sender = email_config_value(cfg, 'sender_email', 'from_email', 'username', 'user')
    password = email_config_value(cfg, 'sender_password', 'password')
    recipients = email_recipients(email_config_value(
        cfg, 'receiver_email', 'receiver_emails', 'to_email', 'to'))
    use_ssl = bool(email_config_value(cfg, 'use_ssl', 'smtp_ssl', default=(port == 465)))
    use_tls = bool(email_config_value(cfg, 'use_tls', 'starttls', default=(not use_ssl)))
    username = email_config_value(cfg, 'smtp_username', 'username', 'user', default=sender)

    missing = [
        name
        for name, value in (
            ('smtp_server', server),
            ('sender_email', sender),
            ('receiver_email', recipients),
        )
        if not value
    ]
    if missing:
        return False, [f"[email] skipped: missing EMAIL_CONFIG keys: {', '.join(missing)}"]

    message = EmailMessage()
    message['Subject'] = subject
    message['From'] = str(sender)
    message['To'] = ', '.join(recipients)
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
        return False, [f'[email] failed: {repr(exc)}']

    return True, [f"[email] sent: {subject} -> {', '.join(recipients)}"]
