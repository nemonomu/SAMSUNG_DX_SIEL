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

# retail_com 테이블에 INSERT 할 컬럼 (id, inserted_at 제외 — auto)
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


# final_sku_price 가 이 값이면 재고 부재 — OSP 도 None 강제 (사용자 룰)
# Amazon main / detail 의 unavailable 표시 (selector union 의 outOfStock / fod-cx-message 결과)
_UNAVAIL_FSP_LABELS = ('Currently unavailable.', 'No featured offers available')


_ASIN_RE = re.compile(r'/(?:dp|gp/product)/([A-Z0-9]{10})')


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
            # 평점/리뷰: detail 우선
            'star_rating':               d.get('star_rating'),
            'count_of_star_ratings':     d.get('count_of_star_ratings') or primary.get('count_of_star_ratings'),
            'count_of_reviews':          d.get('count_of_reviews') or primary.get('count_of_reviews'),
            'detailed_review_content':   d.get('detailed_review_content'),
            'retailer_sku_name_similar': d.get('retailer_sku_name_similar'),
            # 가격: primary (main 우선, 없으면 bsr) → detail fallback
            # bsr-only 카드는 listing 컬럼 비어있는 경우 많음 → detail page 에서 회수
            'final_sku_price':    primary.get('final_sku_price') or d.get('final_sku_price'),
            'original_sku_price': primary.get('original_sku_price') or d.get('original_sku_price'),
            'savings':            primary.get('savings'),
            'discount_type':      primary.get('discount_type') or d.get('discount_type'),
            # 배송/재고
            'delivery_availability':           d.get('delivery_availability') or primary.get('delivery_availability'),
            'available_quantity_for_purchase': primary.get('available_quantity_for_purchase'),
            # 마케팅
            'sku_popularity': primary.get('sku_popularity'),
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
        # FSP 가 재고 부재 표시 ('Currently unavailable.' / 'No featured offers available') 면
        # OSP 도 None 강제 — unavailable 상태에서 M.R.P. 만 남는 noise 방지 (사용자 룰).
        if row['final_sku_price'] in _UNAVAIL_FSP_LABELS:
            row['original_sku_price'] = None
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
    placeholders = ', '.join(f'%({c})s' for c in COLUMNS)
    cols = ', '.join(COLUMNS)
    sql = f'INSERT INTO dx_siel_test_retail_com ({cols}) VALUES ({placeholders})'
    try:
        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(cur, sql, rows, page_size=50)
        conn.commit()
        print(f'[insert_test] OK: inserted {len(rows)} rows into dx_siel_test_retail_com',
              file=sys.stderr)
        return 0
    except Exception as e:
        conn.rollback()
        print(f'[insert_test] FAIL: {type(e).__name__}: {e}', file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == '__main__':
    sys.exit(main())
