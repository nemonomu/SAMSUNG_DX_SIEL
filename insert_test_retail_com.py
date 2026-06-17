"""jsonl (run.py 출력) → dx_siel_test_retail_com 통합 INSERT.

사용:
  python fpkt/run.py --product hhp --stages main detail --max-rank 10 --max-detail 10 > test10.jsonl
  python apply_sql.py sql/dx_siel_test_retail_com.sql
  python insert_test_retail_com.py test10.jsonl

main record + detail record 를 product_url 로 매칭 후 합쳐 INSERT.
- 가격/마케팅/count/main_rank: main 에서
- spec/review/sku/storage 등: detail 에서
- product 키 = main['product'].upper() (HHP/TV/REF/LDY → 'HHP' 등)
- item: detail['fsn'] (Flipkart) / detail['asin'] (Amazon)
"""
from __future__ import annotations

import json
import os
import re
import sys
import traceback
from datetime import datetime, timezone, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import psycopg2
import psycopg2.extras
import config
import siel_item_mst
import siel_log

IST = timezone(timedelta(hours=5, minutes=30))


def env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.environ.get(name)
    if raw in (None, ''):
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(minimum, value)


def apply_pg_timeouts(cfg: dict) -> tuple[int, int]:
    lock_timeout_ms = env_int('SIEL_DB_LOCK_TIMEOUT_MS', 30000)
    statement_timeout_ms = env_int('SIEL_DB_STATEMENT_TIMEOUT_MS', 180000)
    options = str(cfg.get('options') or '').strip()
    chunks = [options] if options else []
    if lock_timeout_ms:
        chunks.append(f'-c lock_timeout={lock_timeout_ms}')
    if statement_timeout_ms:
        chunks.append(f'-c statement_timeout={statement_timeout_ms}')
    if chunks:
        cfg['options'] = ' '.join(chunks)
    cfg.setdefault('application_name', 'siel_insert_test_retail_com')
    return lock_timeout_ms, statement_timeout_ms

# retail_com 컬럼 분류 — 5/10 사용자 룰: 제품별 retail_com 에 다른 product 의 전용 컬럼 제외.
# 공통 (양 account 모두 채움) + amzn 전용 + fpkt 전용 + 해당 제품 전용.
_COMMON_COLS = [
    'country', 'product', 'item', 'sku', 'account_name', 'page_type',
    'retailer_sku_name', 'product_url', 'calendar_week', 'crawl_datetime', 'batch_id',
    'star_rating', 'count_of_star_ratings',
    'detailed_review_content', 'retailer_sku_name_similar',
    'final_sku_price', 'original_sku_price', 'discount_type',
    'delivery_availability',
    'sku_popularity', 'sku_status', 'main_rank', 'bsr_rank',
]
_AMZN_REDIRECT_COLS = ['redirect']
_AMZN_ONLY_COLS = [
    'summarized_review_content', 'fastest_delivery', 'inventory_status',
    'sku_assurance', 'number_of_units_purchased_past_month',
]
_AMZN_ONLY_COLS_FPKT_INSERT = [
    col for col in _AMZN_ONLY_COLS if col != 'inventory_status'
]
_FPKT_ONLY_COLS = [
    'count_of_reviews', 'available_quantity_for_purchase', 'savings',
]
_PRODUCT_SPECIFIC = {
    'hhp': ['hhp_storage', 'hhp_color', 'hhp_memory_ram', 'trade_in'],
    'tv':  ['screen_size', 'model_year', 'estimated_annual_electricity_use'],
    'ref': ['ref_refrigerator_type', 'ref_capacity'],
    'ldy': ['ldy_loading_type', 'ldy_capacity'],
}
_PRODUCT_SPECIFIC_FPKT = {
    'hhp': ['hhp_storage', 'hhp_color', 'trade_in'],
    'tv':  _PRODUCT_SPECIFIC['tv'],
    'ref': _PRODUCT_SPECIFIC['ref'],
    'ldy': _PRODUCT_SPECIFIC['ldy'],
}

# 8 운영 테이블 (4 retail_com + 4 product_list). product 별 분기.
PRODUCT_LOWERS = ('hhp', 'tv', 'ref', 'ldy')

# retail_com — product 별 컬럼 분기
COLUMNS_BY_PRODUCT = {
    p: _COMMON_COLS + _AMZN_REDIRECT_COLS + _AMZN_ONLY_COLS + _FPKT_ONLY_COLS + _PRODUCT_SPECIFIC[p]
    for p in PRODUCT_LOWERS
}
COLUMNS_BY_PRODUCT_FPKT = {
    p: _COMMON_COLS + _AMZN_ONLY_COLS_FPKT_INSERT + _FPKT_ONLY_COLS + _PRODUCT_SPECIFIC_FPKT[p]
    for p in PRODUCT_LOWERS
}

# 호환용 alias (전체 컬럼 union — main() 의 row 검증용, INSERT 안 함)
COLUMNS = (_COMMON_COLS + _AMZN_REDIRECT_COLS + _AMZN_ONLY_COLS + _FPKT_ONLY_COLS
           + _PRODUCT_SPECIFIC['hhp'] + _PRODUCT_SPECIFIC['tv']
           + _PRODUCT_SPECIFIC['ref'] + _PRODUCT_SPECIFIC['ldy'])

# product_list 컬럼 — main + bsr 단계 raw 만 (detail 출처 + sku 제외).
# 사용자 룰 5/10: sku 는 detail 의 spec 또는 코드 분기 (HHP detail.py:303) 채움 → main/bsr 단계 미수집.
# 보존: available_quantity_for_purchase (fpkt main "Only X left", fpkt 유일 재고).
COLUMNS_LIST = [
    'country', 'product', 'item', 'account_name', 'page_type',
    'retailer_sku_name', 'product_url', 'calendar_week', 'crawl_datetime', 'batch_id',
    'star_rating', 'count_of_star_ratings', 'count_of_reviews',
    'final_sku_price', 'original_sku_price', 'savings', 'discount_type',
    'available_quantity_for_purchase',
    'sku_popularity', 'sku_status', 'main_rank', 'bsr_rank',
    'number_of_units_purchased_past_month',
]
COLUMNS_LIST_AMZN = (
    COLUMNS_LIST[:COLUMNS_LIST.index('product_url') + 1]
    + _AMZN_REDIRECT_COLS
    + COLUMNS_LIST[COLUMNS_LIST.index('product_url') + 1:]
)


_ASIN_RE = re.compile(r'/(?:dp|gp/product)/([A-Z0-9]{10})')
# Flipkart product URL 의 pid query param = fsn (Flipkart Standard Number).
# main/bsr selector 가 fsn 을 직접 추출하지 않아 main rec.fsn = None.
# detail rec 는 fsn 직접 수집 → key mismatch (main url-path vs detail fsn). 본 regex 가
# main url 에서 pid 를 fsn 로 사용 — main+detail merge 매칭 회복.
_FPKT_PID_RE = re.compile(r'[?&]pid=([A-Z0-9]+)')

# 인도식 콤마 (1,49,998) → 서양식 (149,998) 변환 — 사용자 룰 (5/9)
# Amazon.in / Flipkart 모두 인도식 표기 사용 (마지막 3자리 + 그 앞 2자리씩).
# raw jsonl 은 보존, DB INSERT 시점에만 정규화.
_PRICE_RE = re.compile(r'^(₹)([\d,]+)(.*)$')


def normalize_price(p):
    """₹1,49,998 → ₹149,998 (인도식 → 서양식 천 단위 콤마)."""
    if not p or '₹' not in str(p):
        return p
    m = _PRICE_RE.match(str(p))
    if not m:
        return p
    prefix, digits, suffix = m.groups()
    raw = digits.replace(',', '')
    if not raw.isdigit():
        return p
    if suffix == '.00':
        suffix = ''
    return f'{prefix}{int(raw):,}{suffix}'


def price_to_int(v):
    if v is None or v == '':
        return None
    if isinstance(v, (int, float)):
        return int(v)
    match = re.search(r'(\d[\d,]*)', str(v))
    if not match:
        return None
    try:
        return int(match.group(1).replace(',', ''))
    except ValueError:
        return None


def computed_savings_text(final, original):
    final_value = price_to_int(final)
    original_value = price_to_int(original)
    if final_value is None or original_value is None:
        return None
    if final_value <= 0 or original_value <= final_value:
        return None
    if original_value > final_value * 10:
        return None
    raw = (original_value - final_value) * 100.0 / original_value
    if raw <= 0:
        return None
    if raw < 1:
        return '1%'
    return f'{int(raw + 0.5)}%'


def normalize_fpkt_price_values(final_price, original_price):
    final_norm = normalize_price(final_price)
    original_norm = normalize_price(original_price)
    final_value = price_to_int(final_norm)
    original_value = price_to_int(original_norm)
    if final_value is None or original_value is None:
        return final_norm, None, None
    if original_value <= final_value or original_value > final_value * 10:
        return final_norm, None, None
    return final_norm, original_norm, computed_savings_text(final_norm, original_norm)


def amazon_price_fields(final_price, original_price, savings):
    final_norm = normalize_price(final_price)
    original_norm = normalize_price(original_price)
    if final_norm not in (None, '') and price_to_int(final_norm) is None:
        return final_norm, None, None
    return final_norm, original_norm, savings


def normalize_count(v):
    """count_of_reviews / count_of_star_ratings 인도식 → 서양식 콤마.
    예: '12,34,567' → '1,234,567'. 4자리 미만 ('6,759' 등) 은 인도식=서양식 동일."""
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    raw = s.replace(',', '')
    if not raw.isdigit():
        return v  # parse 실패 시 raw 보존
    return f'{int(raw):,}'


def normalize_star_value(v):
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    if s.lower() == 'no customer reviews':
        return s
    return siel_log.parse_star_rating(s)


def normalize_rating_count_value(v):
    parsed = siel_log.parse_count_of_ratings(v)
    if parsed is not None:
        return normalize_count(parsed)
    normalized = normalize_count(v)
    return normalized if normalized != v else None


def normalize_review_count_value(v):
    parsed = siel_log.parse_count_of_reviews(v)
    if parsed is not None:
        return normalize_count(parsed)
    normalized = normalize_count(v)
    return normalized if normalized != v else None


def url_path(url: str) -> str:
    """? 앞 path 만 — fallback 매칭용."""
    return (url or '').split('?', 1)[0].rstrip('/')


def fpkt_pid_from_record(rec: dict) -> str | None:
    url = rec.get('product_url') or rec.get('source_url') or rec.get('review_url') or rec.get('url') or ''
    m = _FPKT_PID_RE.search(str(url))
    return m.group(1) if m else None


def listing_key(rec: dict) -> str:
    """main/bsr/detail 공통 dedupe key — Amazon ASIN / Flipkart fsn / fallback url path.
    같은 ASIN 의 main+bsr/detail URL 이 ref=sr_... vs ref=zg_bs_... 로 path 가 달라도 매칭."""
    fpkt_pid = fpkt_pid_from_record(rec)
    if fpkt_pid:
        return fpkt_pid
    asin = rec.get('asin') or rec.get('fsn')
    if asin:
        return asin
    url = rec.get('product_url') or rec.get('source_url') or ''
    m = _ASIN_RE.search(url)
    if m:
        return m.group(1)
    m = _FPKT_PID_RE.search(url)
    if m:
        return m.group(1)
    return url_path(url)


def calendar_week_iso(dt) -> str:
    """ISO calendar week — '2026-W18' 형식."""
    if dt is None:
        return None
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace('Z', '+00:00'))
        except ValueError:
            return None
    _iso_year, iso_week, _ = dt.isocalendar()
    return f'w{iso_week:02d}'


def parse_int_safe(v):
    if v is None or v == '':
        return None
    try:
        return int(str(v).replace(',', ''))
    except (ValueError, TypeError):
        return None


def null_if_blank(v):
    if isinstance(v, str) and v.strip() == '':
        return None
    return v


def nullify_blank_strings(row: dict) -> dict:
    return {key: null_if_blank(value) for key, value in row.items()}


def make_row(main_rec, bsr_rec, detail_rec):
    """단일 main + bsr + detail record → 1 row dict (None 가능). retail_com (full) 용."""
    listing_one = {'_': {'main': main_rec, 'bsr': bsr_rec}}
    detail_one = {'_': detail_rec or {}}
    rows = merge(listing_one, detail_one, max_n=1)
    return rows[0] if rows else None


def make_row_listing(main_rec, bsr_rec, detail_rec=None):
    """단일 main + bsr only → 1 row dict (detail 출처 컬럼은 NULL). product_list 용.
    사용자 룰 (5/10): product_list = main + bsr 단계 raw 데이터만, detail 출처 X."""
    listing_one = {'_': {'main': main_rec, 'bsr': bsr_rec}}
    redirect_by_key = None
    if detail_rec is not None:
        redirect_by_key = {'_': detail_rec.get('redirect')}
    rows = merge(
        listing_one,
        {},
        max_n=1,
        redirect_by_key=redirect_by_key,
    )  # detail dict empty → detail 출처 컬럼 NULL
    return rows[0] if rows else None


def retail_columns_for_row(prod_lower: str, row: dict) -> list[str]:
    if (row.get('account_name') or '').lower() == 'flipkart':
        return COLUMNS_BY_PRODUCT_FPKT[prod_lower]
    return COLUMNS_BY_PRODUCT[prod_lower]


def list_columns_for_row(row: dict) -> list[str]:
    if (row.get('account_name') or '').lower() == 'amazon':
        return COLUMNS_LIST_AMZN
    return COLUMNS_LIST


def rank_maps_for_listing(items: list) -> tuple[dict, dict]:
    def build(stage: str, rank_field: str) -> dict:
        ranked = []
        unranked = []
        for idx, (key, entry) in enumerate(items):
            rec = entry.get(stage) or {}
            if not rec:
                continue
            rank = parse_int_safe(rec.get(rank_field))
            if rank is None:
                unranked.append((idx, key))
            else:
                ranked.append((rank, idx, key))
        ranked.sort(key=lambda x: (x[0], x[1]))
        ordered = [(idx, key) for _rank, idx, key in ranked] + unranked
        return {key: pos for pos, (_idx, key) in enumerate(ordered, start=1)}

    return build('main', 'main_rank'), build('bsr', 'bsr_rank')


def merge(listing: dict, detail: dict, max_n: int = 10,
          redirect_by_key=None, exclude_keys=None, renumber_ranks: bool = False) -> list:
    """listing[key] = {'main': rec or None, 'bsr': rec or None} + detail merge → row list.
    main_rank / bsr_rank 둘 다 set (같은 SKU 가 main+bsr 양쪽에 있으면).
    page_type: main 우선, 없으면 bsr.
    max_n=0 → 무제한, >0 이면 cap."""
    rows = []
    items = list(listing.items())
    main_rank_by_key, bsr_rank_by_key = ({}, {})
    if renumber_ranks:
        main_rank_by_key, bsr_rank_by_key = rank_maps_for_listing(items)
    if max_n and max_n > 0:
        items = items[:max_n]
    for key, entry in items:
        if exclude_keys and key in exclude_keys:
            continue
        m = entry.get('main') or {}
        b = entry.get('bsr') or {}
        primary = m or b  # 정보량 main >= bsr 가정
        if not primary:
            continue
        d = detail.get(key, {})
        # account/product 정규화 (primary 기준)
        account = (primary.get('account_name') or d.get('account_name') or '').capitalize()
        prod = (primary.get('product') or d.get('product') or '').upper()
        item = fpkt_pid_from_record(primary) or fpkt_pid_from_record(d) or d.get('fsn') or d.get('asin') or primary.get('fsn') or primary.get('asin')
        sku = d.get('sku') or primary.get('sku')
        cdt = d.get('crawl_datetime') or primary.get('crawl_datetime')
        page_type = 'main' if m else 'bsr'
        redirect_value = d.get('redirect')
        if redirect_value is None and redirect_by_key and key in redirect_by_key:
            redirect_value = redirect_by_key.get(key)
        if (account or '').lower() == 'amazon' and redirect_value is None:
            redirect_value = False
        redirect_listing_only = (
            d.get('_detail_skip') == 'asin_mismatch'
            and d.get('redirect') is True
        )
        final_price = normalize_price(primary.get('final_sku_price') or d.get('final_sku_price'))
        original_price = normalize_price(primary.get('original_sku_price') or d.get('original_sku_price'))
        savings = primary.get('savings') or d.get('savings')
        if (account or '').lower() == 'amazon':
            final_price, original_price, savings = amazon_price_fields(
                final_price, original_price, savings)
        elif (account or '').lower() == 'flipkart':
            final_price, original_price, savings = normalize_fpkt_price_values(final_price, original_price)

        row = {
            'country':           'SIEL',
            'product':           prod or None,
            'item':              item,
            'sku':               sku,
            'account_name':      account or None,
            'page_type':         page_type,
            'retailer_sku_name': d.get('retailer_sku_name') or primary.get('retailer_sku_name'),
            'product_url':       primary.get('product_url') or d.get('source_url'),
            'redirect':          redirect_value,
            'calendar_week':     calendar_week_iso(cdt),
            'crawl_datetime':    cdt,
            'batch_id':          primary.get('batch_id') or d.get('batch_id'),
            # 평점/리뷰: detail 우선, main fallback (양방향 보강 5/8). count 는 서양식 콤마 정규화 (5/10).
            'star_rating':               normalize_star_value(d.get('star_rating') or primary.get('star_rating')),
            'count_of_star_ratings':     normalize_rating_count_value(d.get('count_of_star_ratings') or primary.get('count_of_star_ratings')),
            # 고객사 spec: count_of_reviews 는 listing(primary) 단계 값 우선. detail fallback.
            'count_of_reviews':          normalize_review_count_value(primary.get('count_of_reviews') or d.get('count_of_reviews')),
            'detailed_review_content':   d.get('detailed_review_content'),
            'retailer_sku_name_similar': d.get('retailer_sku_name_similar'),
            # 가격: primary (main 우선, 없으면 bsr) → detail fallback
            # bsr-only 카드는 listing 컬럼 비어있는 경우 많음 → detail page 에서 회수
            # normalize_price: 인도식 콤마 → 서양식 (1,49,998 → 149,998)
            'final_sku_price':    final_price,
            'original_sku_price': original_price,
            'savings':            savings,
            'discount_type':      primary.get('discount_type'),
            # 배송/재고
            'delivery_availability':           d.get('delivery_availability') or primary.get('delivery_availability'),
            'available_quantity_for_purchase': primary.get('available_quantity_for_purchase'),
            # 마케팅 — sku_popularity main NULL 시 detail fallback (Flipkart anti-bot 시 main 100% NULL 대응)
            'sku_popularity': primary.get('sku_popularity') or d.get('sku_popularity'),
            'sku_status':     primary.get('sku_status'),
            # 순위 — main + bsr 둘 다 보존 (같은 SKU 가 양쪽에 있으면 함께 set)
            'main_rank': (main_rank_by_key.get(key) if (renumber_ranks and m)
                          else (parse_int_safe(m.get('main_rank')) if m else None)),
            'bsr_rank':  (bsr_rank_by_key.get(key) if (renumber_ranks and b)
                          else (parse_int_safe(b.get('bsr_rank')) if b else None)),
            # TV
            'screen_size':                      d.get('screen_size'),
            'model_year':                       d.get('model_year'),
            'estimated_annual_electricity_use': d.get('estimated_annual_electricity_use'),
            # HHP
            'hhp_storage': d.get('hhp_storage'),
            'hhp_color':   d.get('hhp_color'),
            'hhp_memory_ram':  d.get('hhp_memory_ram'),
            'trade_in':    d.get('trade_in'),
            # REF
            'ref_refrigerator_type': d.get('ref_refrigerator_type'),
            'ref_capacity':          d.get('ref_capacity'),
            # LDY
            'ldy_loading_type': d.get('ldy_loading_type'),
            'ldy_capacity':     d.get('ldy_capacity'),
            # Amazon
            'summarized_review_content':            d.get('summarized_review_content'),
            'fastest_delivery':                     d.get('fastest_delivery'),
            'inventory_status':                     d.get('inventory_status'),
            'sku_assurance':                        d.get('sku_assurance'),
            'number_of_units_purchased_past_month': primary.get('number_of_units_purchased_past_month'),
        }
        if redirect_listing_only:
            row['_redirect_listing_only'] = True
        rows.append(nullify_blank_strings(row))
    return rows


def main() -> int:
    if len(sys.argv) < 2:
        print('usage: python insert_test_retail_com.py <jsonl_path> [max_n=10|0=unlimited] [--dry-run]', file=sys.stderr)
        return 2
    args = sys.argv[1:]
    jsonl_path = args[0]
    max_n = 10
    dry_run = os.environ.get('SIEL_INSERT_DRY_RUN', '').lower() in ('1', 'true', 'yes', 'y')
    for arg in args[1:]:
        if arg == '--dry-run':
            dry_run = True
        else:
            max_n = int(arg)

    if not os.path.exists(jsonl_path):
        print(f'[insert_test] file not found: {jsonl_path}', file=sys.stderr)
        return 2

    listing_by_url = {}  # key → {'main': rec or None, 'bsr': rec or None}
    detail_by_url = {}
    redirect_by_key = {}
    detail_skip_keys = set()
    n_main = n_bsr = n_detail = n_other = 0
    n_dup_main = n_dup_bsr = 0

    with open(jsonl_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            stage = rec.get('stage')
            key = listing_key(rec)
            if not key:
                n_other += 1
                continue
            if stage in ('main', 'bsr'):
                entry = listing_by_url.setdefault(key, {'main': None, 'bsr': None})
                if entry.get(stage) is None:
                    entry[stage] = rec
                elif stage == 'main':
                    n_dup_main += 1
                else:
                    n_dup_bsr += 1
                if stage == 'main':
                    n_main += 1
                else:
                    n_bsr += 1
            elif stage == 'detail':
                detail_skip_reason = rec.get('_detail_skip')
                redirect_skip = detail_skip_reason == 'asin_mismatch' and rec.get('redirect') is True
                if detail_skip_reason and not redirect_skip:
                    detail_skip_keys.add(key)
                else:
                    detail_by_url[key] = rec
                if 'redirect' in rec:
                    redirect_by_key[key] = rec.get('redirect')
                n_detail += 1
            else:
                n_other += 1

    print(f'[insert_test] read main={n_main} bsr={n_bsr} detail={n_detail} other={n_other} unique_listing={len(listing_by_url)} dup_main={n_dup_main} dup_bsr={n_dup_bsr}',
          file=sys.stderr)

    # retail_com 용 — main + bsr + detail merge (full)
    rows_full = merge(listing_by_url, detail_by_url, max_n=max_n,
                      exclude_keys=detail_skip_keys,
                      renumber_ranks=True)
    # product_list 용 — main + bsr only (detail 출처 컬럼 NULL, 사용자 룰 5/10)
    rows_listing = merge(listing_by_url, {}, max_n=max_n,
                         redirect_by_key=redirect_by_key,
                         renumber_ranks=True)
    print(f'[insert] merge: {len(rows_full)} rows (full) / {len(rows_listing)} rows (listing-only)',
          file=sys.stderr)
    if dry_run:
        print('[insert] dry_run=true: SQL will be executed and rolled back', file=sys.stderr)

    if not rows_full and not rows_listing:
        print('[insert] no rows to insert', file=sys.stderr)
        return 1

    cfg = dict(config.DB_CONFIG)
    cfg.setdefault('database', 'postgres')
    cfg.setdefault('client_encoding', 'utf8')
    lock_timeout_ms, statement_timeout_ms = apply_pg_timeouts(cfg)
    print(
        f'[insert] db_timeouts lock_timeout_ms={lock_timeout_ms} '
        f'statement_timeout_ms={statement_timeout_ms}',
        file=sys.stderr,
    )
    conn = psycopg2.connect(**cfg)
    # 본 운영 8 테이블 (4 retail_com + 4 product_list) — product 별 분기 INSERT.
    # 5/10 사용자 룰: 제품별 retail_com 은 해당 제품 전용 컬럼만 (다른 product 전용 X).
    total_inserted = 0
    try:
        for prod_lower in PRODUCT_LOWERS:
            rows_full_prod = [r for r in rows_full if (r.get('product') or '').lower() == prod_lower]
            rows_full_mst_prod = [r for r in rows_full_prod if not r.get('_redirect_listing_only')]
            rows_listing_prod = [r for r in rows_listing if (r.get('product') or '').lower() == prod_lower]
            if not rows_full_prod and not rows_listing_prod:
                continue
            table_retail = f'dx_siel_{prod_lower}_retail_com'
            table_list = f'dx_siel_{prod_lower}_product_list'
            # mst fallback — 이전 run 의 mst 값으로 NULL spec 채움. 실패해도 INSERT 진행.
            mst_filled = 0
            mst_fb_err = None
            if rows_full_mst_prod:
                with conn.cursor() as sp_cur:
                    sp_cur.execute('SAVEPOINT mst_fb_sp')
                    try:
                        mst_filled = siel_item_mst.fill_from_mst(conn, prod_lower, rows_full_mst_prod)
                        sp_cur.execute('RELEASE SAVEPOINT mst_fb_sp')
                    except Exception as e:
                        sp_cur.execute('ROLLBACK TO SAVEPOINT mst_fb_sp')
                        mst_fb_err = f'{type(e).__name__}: {e}'
            rows_full_by_cols = {}
            for row in rows_full_prod:
                cols_key = tuple(retail_columns_for_row(prod_lower, row))
                rows_full_by_cols.setdefault(cols_key, []).append(row)
            rows_listing_by_cols = {}
            for row in rows_listing_prod:
                cols_key = tuple(list_columns_for_row(row))
                rows_listing_by_cols.setdefault(cols_key, []).append(row)
            with conn.cursor() as cur:
                for cols_key, rows_for_cols in rows_full_by_cols.items():
                    cols_retail = ', '.join(cols_key)
                    placeholders_retail = ', '.join(f'%({c})s' for c in cols_key)
                    sql_retail = f'INSERT INTO {table_retail} ({cols_retail}) VALUES ({placeholders_retail})'
                    psycopg2.extras.execute_batch(cur, sql_retail, rows_for_cols, page_size=50)
                for cols_key, rows_for_cols in rows_listing_by_cols.items():
                    cols_list = ', '.join(cols_key)
                    placeholders_list = ', '.join(f'%({c})s' for c in cols_key)
                    sql_list = f'INSERT INTO {table_list} ({cols_list}) VALUES ({placeholders_list})'
                    psycopg2.extras.execute_batch(cur, sql_list, rows_for_cols, page_size=50)
            # mst upsert 실패해도 retail/list INSERT 는 commit. SAVEPOINT 로 격리.
            mst_upserted = 0
            mst_err = None
            with conn.cursor() as sp_cur:
                sp_cur.execute('SAVEPOINT mst_sp')
                try:
                    mst_upserted = siel_item_mst.upsert_item_mst_batch(conn, prod_lower, rows_full_mst_prod)
                    sp_cur.execute('RELEASE SAVEPOINT mst_sp')
                except Exception as e:
                    sp_cur.execute('ROLLBACK TO SAVEPOINT mst_sp')
                    mst_err = f'{type(e).__name__}: {e}'
            total_inserted += len(rows_full_prod) + len(rows_listing_prod)
            if dry_run:
                conn.rollback()
            else:
                conn.commit()
            mst_log = f'mst={mst_upserted}' if not mst_err else f'mst=FAIL ({mst_err})'
            fb_log = f'mst_fb={mst_filled}' if not mst_fb_err else f'mst_fb=FAIL ({mst_fb_err})'
            print(f'[insert] OK: {prod_lower} retail={len(rows_full_prod)} list={len(rows_listing_prod)} {fb_log} {mst_log}',
                  file=sys.stderr)
        if dry_run:
            print(f'[insert] DRY_RUN DONE: total {total_inserted} rows tested and rolled back',
                  file=sys.stderr)
        else:
            print(f'[insert] DONE: total {total_inserted} rows inserted across 8 tables',
                  file=sys.stderr)
        return 0
    except Exception as e:
        conn.rollback()
        print(f'[insert] FAIL: {type(e).__name__}: {e}', file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == '__main__':
    sys.exit(main())
