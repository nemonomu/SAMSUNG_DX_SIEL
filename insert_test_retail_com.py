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


def url_path(url: str) -> str:
    """? 앞 path 만 — main 의 product_url 과 detail 의 source_url 매칭용."""
    return (url or '').split('?', 1)[0].rstrip('/')


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


def merge(main: dict, detail: dict, max_n: int = 10) -> list:
    """main_by_url + detail_by_url merge → list of retail_com row dict (max_n)."""
    rows = []
    for key, m in list(main.items())[:max_n]:
        d = detail.get(key, {})
        # account/product 정규화
        account = (m.get('account_name') or d.get('account_name') or '').capitalize()  # 'Flipkart'/'Amazon'
        prod = (m.get('product') or d.get('product') or '').upper()  # 'HHP'/'TV'/'REF'/'LDY'
        # item: fsn / asin
        item = d.get('fsn') or d.get('asin') or m.get('fsn') or m.get('asin')
        # sku: detail 우선
        sku = d.get('sku') or m.get('sku')
        # crawl_datetime: detail 우선 (더 최근)
        cdt = d.get('crawl_datetime') or m.get('crawl_datetime')
        row = {
            'country':           'siel',
            'product':           prod or None,
            'item':              item,
            'sku':               sku,
            'account_name':      account or None,
            'page_type':         'main',  # 통합 row 는 main rank 보존이 의미라 main 기본
            'retailer_sku_name': m.get('retailer_sku_name') or d.get('retailer_sku_name'),
            'product_url':       m.get('product_url') or d.get('source_url'),
            'calendar_week':     calendar_week_iso(cdt),
            'crawl_datetime':    cdt,
            'batch_id':          m.get('batch_id') or d.get('batch_id'),
            # 평점/리뷰: detail 에 있으면 detail 우선 (더 정확)
            'star_rating':               d.get('star_rating'),
            'count_of_star_ratings':     d.get('count_of_star_ratings') or m.get('count_of_star_ratings'),
            'count_of_reviews':          d.get('count_of_reviews') or m.get('count_of_reviews'),
            'detailed_review_content':   d.get('detailed_review_content'),
            'retailer_sku_name_similar': d.get('retailer_sku_name_similar'),
            # 가격: main (ERD)
            'final_sku_price':    m.get('final_sku_price'),
            'original_sku_price': m.get('original_sku_price'),
            'savings':            m.get('savings'),
            'discount_type':      m.get('discount_type'),
            # 배송/재고
            'delivery_availability':           d.get('delivery_availability') or m.get('delivery_availability'),
            'available_quantity_for_purchase': m.get('available_quantity_for_purchase'),
            # 마케팅
            'sku_popularity': m.get('sku_popularity'),
            'sku_status':     m.get('sku_status'),
            # 순위
            'main_rank': parse_int_safe(m.get('main_rank')),
            'bsr_rank':  parse_int_safe(m.get('bsr_rank')),
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
            'number_of_units_purchased_past_month': m.get('number_of_units_purchased_past_month'),
        }
        rows.append(row)
    return rows


def main() -> int:
    if len(sys.argv) < 2:
        print('usage: python insert_test_retail_com.py <jsonl_path> [max_n=10]', file=sys.stderr)
        return 2
    jsonl_path = sys.argv[1]
    max_n = int(sys.argv[2]) if len(sys.argv) > 2 else 10

    if not os.path.exists(jsonl_path):
        print(f'[insert_test] file not found: {jsonl_path}', file=sys.stderr)
        return 2

    main_by_url = {}
    detail_by_url = {}
    n_main = n_detail = n_other = 0

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
            if stage == 'main':
                key = url_path(rec.get('product_url', ''))
                if key:
                    main_by_url[key] = rec
                    n_main += 1
            elif stage == 'detail':
                key = url_path(rec.get('source_url', ''))
                if key:
                    detail_by_url[key] = rec
                    n_detail += 1
            else:
                n_other += 1

    print(f'[insert_test] read main={n_main} detail={n_detail} other={n_other}',
          file=sys.stderr)

    rows = merge(main_by_url, detail_by_url, max_n=max_n)
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
