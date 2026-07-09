"""JSONL (main + detail) → dx_siel_test_retail_com INSERT.
사용:
  python fpkt/run.py --product tv --stages main detail --max-rank 10 > tv10.jsonl
  python ingest_retail_com.py --jsonl tv10.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import psycopg2
import psycopg2.extras

import config
import siel_item_mst

COLS = [
    'country', 'product', 'item', 'sku', 'account_name', 'page_type',
    'retailer_sku_name', 'product_url', 'calendar_week', 'crawl_datetime', 'batch_id',
    'star_rating', 'count_of_star_ratings', 'count_of_reviews',
    'detailed_review_content', 'retailer_sku_name_similar',
    'final_sku_price', 'original_sku_price', 'savings', 'discount_type',
    'delivery_availability', 'available_quantity_for_purchase',
    'sku_popularity', 'sku_status',
    'main_rank', 'bsr_rank',
    'screen_size', 'model_year', 'estimated_annual_electricity_use',
    'hhp_storage', 'hhp_color', 'hhp_memory_ram', 'trade_in',
    'ref_refrigerator_type', 'ref_capacity',
    'ldy_loading_type', 'ldy_capacity',
    'summarized_review_content', 'fastest_delivery', 'inventory_status',
    'number_of_units_purchased_past_month',
]

INT_COLS = {'main_rank', 'bsr_rank'}


def _to_int(v):
    if v is None:
        return None
    if isinstance(v, int):
        return v
    s = str(v).replace(',', '').strip()
    m = re.match(r'-?\d+', s)
    return int(m.group()) if m else None


def calendar_week_from_iso(v):
    """ISO datetime ('2026-05-01T11:30:00+05:30') → 'YYYY-Www' (ISO week)."""
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(str(v))
    except ValueError:
        return None
    iso = dt.isocalendar()
    return f'w{iso[1]:02d}'


def normalize_account(v):
    if not v:
        return None
    s = str(v).lower()
    if s == 'flipkart':
        return 'Flipkart'
    if s == 'amazon':
        return 'Amazon'
    return v


def normalize_product(v):
    if not v:
        return None
    return str(v).upper()


def main():
    ap = argparse.ArgumentParser(description='SIEL retail_com ingest')
    ap.add_argument('--jsonl', required=True)
    ap.add_argument('--table', default='dx_siel_test_retail_com')
    ap.add_argument('--country', default='SIEL')
    args = ap.parse_args()

    main_by_url = {}
    bsr_by_url = {}
    detail_by_url = {}

    with open(args.jsonl, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or not line.startswith('{'):
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            stage = r.get('stage')
            url = r.get('product_url') or r.get('source_url')
            if not url:
                continue
            if stage == 'detail':
                detail_by_url[url] = r
            elif stage == 'bsr':
                bsr_by_url[url] = r
            elif stage == 'main':
                main_by_url[url] = r
            else:
                # stage 미상 — main 으로 가정
                main_by_url[url] = r

    rows = []
    all_urls = set(main_by_url) | set(bsr_by_url) | set(detail_by_url)
    for url in all_urls:
        m = main_by_url.get(url, {})
        b = bsr_by_url.get(url, {})
        d = detail_by_url.get(url, {})
        # main → bsr → detail 순으로 merge (detail 이 가장 최신 spec). main_rank/bsr_rank 둘 다 보존.
        merged = {**m, **b, **d}
        merged['product_url'] = m.get('product_url') or b.get('product_url') or d.get('source_url')

        # main_rank / bsr_rank 는 union merge 후 각각 살림
        if m.get('main_rank') is not None:
            merged['main_rank'] = m.get('main_rank')
        if b.get('bsr_rank') is not None:
            merged['bsr_rank'] = b.get('bsr_rank')

        page_parts = []
        if m: page_parts.append('main')
        if b: page_parts.append('bsr')
        if d: page_parts.append('detail')

        row = {c: None for c in COLS}
        row['country'] = args.country
        row['product'] = normalize_product(merged.get('product'))
        row['account_name'] = normalize_account(merged.get('account_name'))
        row['item'] = merged.get('fsn') or merged.get('item')
        row['page_type'] = '+'.join(page_parts) if page_parts else None

        for k in COLS:
            if row[k] is None and k in merged and merged[k] not in (None, ''):
                row[k] = merged[k]

        for k in INT_COLS:
            row[k] = _to_int(row[k])

        # crawl_datetime → ISO week 자동 계산 (ERD: '2026-W18')
        if not row.get('calendar_week'):
            row['calendar_week'] = calendar_week_from_iso(row.get('crawl_datetime'))

        rows.append(row)

    if not rows:
        print(f'no rows to insert (main_n={len(main_by_url)} bsr_n={len(bsr_by_url)} detail_n={len(detail_by_url)})')
        return 0

    cfg = dict(config.DB_CONFIG)
    cfg.setdefault('database', 'postgres')
    conn = psycopg2.connect(**cfg)
    try:
        # mst fallback — 이전 run 의 mst 값으로 NULL spec 채움. 실패해도 INSERT 진행.
        mst_fb_total = 0
        mst_fb_errs = []
        for prod_lower in ('hhp', 'tv', 'ref', 'ldy'):
            rows_prod = [r for r in rows if (r.get('product') or '').lower() == prod_lower]
            if not rows_prod:
                continue
            with conn.cursor() as sp_cur:
                sp_cur.execute('SAVEPOINT mst_fb_sp')
                try:
                    mst_fb_total += siel_item_mst.fill_from_mst(conn, prod_lower, rows_prod)
                    sp_cur.execute('RELEASE SAVEPOINT mst_fb_sp')
                except Exception as e:
                    sp_cur.execute('ROLLBACK TO SAVEPOINT mst_fb_sp')
                    mst_fb_errs.append(f'{prod_lower}: {type(e).__name__}: {e}')
        with conn.cursor() as cur:
            placeholders = ', '.join(['%s'] * len(COLS))
            sql = f'INSERT INTO {args.table} ({", ".join(COLS)}) VALUES ({placeholders})'
            data = [tuple(row[c] for c in COLS) for row in rows]
            psycopg2.extras.execute_batch(cur, sql, data)
        # mst upsert 실패해도 retail INSERT 는 commit. SAVEPOINT 로 격리.
        mst_total = 0
        mst_errs = []
        for prod_lower in ('hhp', 'tv', 'ref', 'ldy'):
            rows_prod = [r for r in rows if (r.get('product') or '').lower() == prod_lower]
            if not rows_prod:
                continue
            with conn.cursor() as sp_cur:
                sp_cur.execute('SAVEPOINT mst_sp')
                try:
                    mst_total += siel_item_mst.upsert_item_mst_batch(conn, prod_lower, rows_prod)
                    sp_cur.execute('RELEASE SAVEPOINT mst_sp')
                except Exception as e:
                    sp_cur.execute('ROLLBACK TO SAVEPOINT mst_sp')
                    mst_errs.append(f'{prod_lower}: {type(e).__name__}: {e}')
        conn.commit()
        mst_log = f'mst={mst_total}' if not mst_errs else f'mst={mst_total} FAIL=[{"; ".join(mst_errs)}]'
        fb_log = f'mst_fb={mst_fb_total}' if not mst_fb_errs else f'mst_fb={mst_fb_total} FAIL=[{"; ".join(mst_fb_errs)}]'
        print(f'inserted {len(rows)} rows into {args.table} '
              f'(main_n={len(main_by_url)} bsr_n={len(bsr_by_url)} detail_n={len(detail_by_url)}) '
              f'{fb_log} {mst_log}')
    finally:
        conn.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
