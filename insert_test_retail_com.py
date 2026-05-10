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

IST = timezone(timedelta(hours=5, minutes=30))

# retail_com 테이블에 INSERT 할 컬럼 (id, inserted_at 제외 — auto). 4 retail_com 동일.
COLUMNS = [
    'country', 'product', 'item', 'sku', 'account_name', 'page_type',
    'retailer_sku_name', 'product_url', 'calendar_week', 'crawl_datetime', 'batch_id',
    'star_rating', 'count_of_star_ratings', 'count_of_reviews',
    'detailed_review_content', 'retailer_sku_name_similar',
    'final_sku_price', 'original_sku_price', 'savings', 'discount_type',
    'delivery_availability', 'available_quantity_for_purchase',
    'sku_popularity', 'sku_status', 'main_rank', 'bsr_rank',
    'screen_size', 'model_year', 'estimated_annual_electricity_use',
    'hhp_storage', 'hhp_color', 'trade_in',
    'ref_refrigerator_type', 'ref_capacity',
    'ldy_loading_type', 'ldy_capacity',
    'summarized_review_content', 'fastest_delivery', 'inventory_status',
    'sku_assurance', 'number_of_units_purchased_past_month',
]

# product_list 테이블 컬럼 (detail 전용 + 의도적 미수집 제외 — 5/10 사용자 룰).
# 제외: detail 전용 (star_rating / detailed_review_content / retailer_sku_name_similar /
# delivery_availability / summarized_review_content / fastest_delivery / inventory_status /
# sku_assurance / screen_size / model_year / electricity / hhp_*/ref_*/ldy_*).
# + available_quantity_for_purchase (inventory_status 와 중복 정책, 의도적 미수집).
COLUMNS_LIST = [
    'country', 'product', 'item', 'sku', 'account_name', 'page_type',
    'retailer_sku_name', 'product_url', 'calendar_week', 'crawl_datetime', 'batch_id',
    'count_of_star_ratings', 'count_of_reviews',
    'final_sku_price', 'original_sku_price', 'savings', 'discount_type',
    'sku_popularity', 'sku_status', 'main_rank', 'bsr_rank',
    'number_of_units_purchased_past_month',
]

# 8 운영 테이블 (4 retail_com + 4 product_list). product 별 분기.
PRODUCT_LOWERS = ('hhp', 'tv', 'ref', 'ldy')


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
    return f'{prefix}{int(raw):,}{suffix}'


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


def url_path(url: str) -> str:
    """? 앞 path 만 — fallback 매칭용."""
    return (url or '').split('?', 1)[0].rstrip('/')


def listing_key(rec: dict) -> str:
    """main/bsr/detail 공통 dedupe key — Amazon ASIN / Flipkart fsn / fallback url path.
    같은 ASIN 의 main+bsr/detail URL 이 ref=sr_... vs ref=zg_bs_... 로 path 가 달라도 매칭."""
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
    iso_year, iso_week, _ = dt.isocalendar()
    return f'{iso_year}-W{iso_week:02d}'


def parse_int_safe(v):
    if v is None or v == '':
        return None
    try:
        return int(str(v).replace(',', ''))
    except (ValueError, TypeError):
        return None


def make_row(main_rec, bsr_rec, detail_rec):
    """단일 main + bsr + detail record → 1 row dict (None 가능). streaming insert 용 helper."""
    listing_one = {'_': {'main': main_rec, 'bsr': bsr_rec}}
    detail_one = {'_': detail_rec or {}}
    rows = merge(listing_one, detail_one, max_n=1)
    return rows[0] if rows else None


def merge(listing: dict, detail: dict, max_n: int = 10) -> list:
    """listing[key] = {'main': rec or None, 'bsr': rec or None} + detail merge → row list.
    main_rank / bsr_rank 둘 다 set (같은 SKU 가 main+bsr 양쪽에 있으면).
    page_type: main 우선, 없으면 bsr.
    max_n=0 → 무제한, >0 이면 cap."""
    rows = []
    items = list(listing.items())
    if max_n and max_n > 0:
        items = items[:max_n]
    for key, entry in items:
        m = entry.get('main') or {}
        b = entry.get('bsr') or {}
        primary = m or b  # 정보량 main >= bsr 가정
        if not primary:
            continue
        d = detail.get(key, {})
        # account/product 정규화 (primary 기준)
        account = (primary.get('account_name') or d.get('account_name') or '').capitalize()
        prod = (primary.get('product') or d.get('product') or '').upper()
        item = d.get('fsn') or d.get('asin') or primary.get('fsn') or primary.get('asin')
        sku = d.get('sku') or primary.get('sku')
        cdt = d.get('crawl_datetime') or primary.get('crawl_datetime')
        page_type = 'main' if m else 'bsr'
        row = {
            'country':           'siel',
            'product':           prod or None,
            'item':              item,
            'sku':               sku,
            'account_name':      account or None,
            'page_type':         page_type,
            'retailer_sku_name': primary.get('retailer_sku_name') or d.get('retailer_sku_name'),
            'product_url':       primary.get('product_url') or d.get('source_url'),
            'calendar_week':     calendar_week_iso(cdt),
            'crawl_datetime':    cdt,
            'batch_id':          primary.get('batch_id') or d.get('batch_id'),
            # 평점/리뷰: detail 우선, main fallback (양방향 보강 5/8). count 는 서양식 콤마 정규화 (5/10).
            'star_rating':               d.get('star_rating') or primary.get('star_rating'),
            'count_of_star_ratings':     normalize_count(d.get('count_of_star_ratings') or primary.get('count_of_star_ratings')),
            'count_of_reviews':          normalize_count(d.get('count_of_reviews') or primary.get('count_of_reviews')),
            'detailed_review_content':   d.get('detailed_review_content'),
            'retailer_sku_name_similar': d.get('retailer_sku_name_similar'),
            # 가격: primary (main 우선, 없으면 bsr) → detail fallback
            # bsr-only 카드는 listing 컬럼 비어있는 경우 많음 → detail page 에서 회수
            # normalize_price: 인도식 콤마 → 서양식 (1,49,998 → 149,998)
            'final_sku_price':    normalize_price(primary.get('final_sku_price') or d.get('final_sku_price')),
            'original_sku_price': normalize_price(primary.get('original_sku_price') or d.get('original_sku_price')),
            'savings':            primary.get('savings') or d.get('savings'),
            'discount_type':      primary.get('discount_type') or d.get('discount_type'),
            # 배송/재고
            'delivery_availability':           d.get('delivery_availability') or primary.get('delivery_availability'),
            'available_quantity_for_purchase': primary.get('available_quantity_for_purchase'),
            # 마케팅 — sku_popularity main NULL 시 detail fallback (Flipkart anti-bot 시 main 100% NULL 대응)
            'sku_popularity': primary.get('sku_popularity') or d.get('sku_popularity'),
            'sku_status':     primary.get('sku_status'),
            # 순위 — main + bsr 둘 다 보존 (같은 SKU 가 양쪽에 있으면 함께 set)
            'main_rank': parse_int_safe(m.get('main_rank')) if m else None,
            'bsr_rank':  parse_int_safe(b.get('bsr_rank')) if b else None,
            # TV
            'screen_size':                      d.get('screen_size'),
            'model_year':                       d.get('model_year'),
            'estimated_annual_electricity_use': d.get('estimated_annual_electricity_use'),
            # HHP
            'hhp_storage': d.get('hhp_storage'),
            'hhp_color':   d.get('hhp_color'),
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
        rows.append(row)
    return rows


def main() -> int:
    if len(sys.argv) < 2:
        print('usage: python insert_test_retail_com.py <jsonl_path> [max_n=10|0=unlimited]', file=sys.stderr)
        return 2
    jsonl_path = sys.argv[1]
    max_n = int(sys.argv[2]) if len(sys.argv) > 2 else 10

    if not os.path.exists(jsonl_path):
        print(f'[insert_test] file not found: {jsonl_path}', file=sys.stderr)
        return 2

    listing_by_url = {}  # key → {'main': rec or None, 'bsr': rec or None}
    detail_by_url = {}
    n_main = n_bsr = n_detail = n_other = 0

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
                entry[stage] = rec
                if stage == 'main':
                    n_main += 1
                else:
                    n_bsr += 1
            elif stage == 'detail':
                detail_by_url[key] = rec
                n_detail += 1
            else:
                n_other += 1

    print(f'[insert_test] read main={n_main} bsr={n_bsr} detail={n_detail} other={n_other} unique_listing={len(listing_by_url)}',
          file=sys.stderr)

    rows = merge(listing_by_url, detail_by_url, max_n=max_n)
    print(f'[insert_test] merging top {len(rows)} rows', file=sys.stderr)

    if not rows:
        print('[insert_test] no rows to insert', file=sys.stderr)
        return 1

    cfg = dict(config.DB_CONFIG)
    cfg.setdefault('database', 'postgres')
    cfg.setdefault('client_encoding', 'utf8')
    conn = psycopg2.connect(**cfg)
    # 본 운영 8 테이블 (4 retail_com + 4 product_list) — product 별 분기 INSERT.
    # 5/10 사용자 룰: test 테이블 INSERT 제거, 본 테이블만.
    cols_full = ', '.join(COLUMNS)
    placeholders_full = ', '.join(f'%({c})s' for c in COLUMNS)
    cols_list = ', '.join(COLUMNS_LIST)
    placeholders_list = ', '.join(f'%({c})s' for c in COLUMNS_LIST)
    total_inserted = 0
    failed = []
    try:
        for prod_lower in PRODUCT_LOWERS:
            rows_prod = [r for r in rows if (r.get('product') or '').lower() == prod_lower]
            if not rows_prod:
                continue
            table_retail = f'dx_siel_{prod_lower}_retail_com'
            table_list = f'dx_siel_{prod_lower}_product_list'
            sql_retail = f'INSERT INTO {table_retail} ({cols_full}) VALUES ({placeholders_full})'
            sql_list = f'INSERT INTO {table_list} ({cols_list}) VALUES ({placeholders_list})'
            with conn.cursor() as cur:
                psycopg2.extras.execute_batch(cur, sql_retail, rows_prod, page_size=50)
                psycopg2.extras.execute_batch(cur, sql_list, rows_prod, page_size=50)
            conn.commit()
            total_inserted += len(rows_prod)
            print(f'[insert] OK: {len(rows_prod)} rows → {table_retail} + {table_list}',
                  file=sys.stderr)
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
