"""dx_siel_{product}_item_mst upsert 헬퍼.

retail_com INSERT 시 함께 호출. (item, account_name) 단위로 upsert.
- 기존 row 의 NULL / 공백 필드만 새 값으로 채움 (COALESCE + NULLIF 패턴).
- product_url 은 항상 최신값으로 update (URL 변경 추적).
- updated_at = NOW().

호출 측 — insert_test_retail_com.py / ingest_retail_com.py 의 retail_com INSERT 루프
안에서 product_lower 별로 1 회 호출.
"""
from __future__ import annotations

from typing import Iterable

import psycopg2.extras


# 제품군별 spec 컬럼 — DDL 의 고유 컬럼만 포함.
# (id / item / sku / account_name / product_url / created_at / updated_at /
#  is_product / is_checked 는 코드가 별도 처리)
_SPEC_COLS = {
    'tv':  ['screen_size', 'model_year', 'estimated_annual_electricity_use'],
    'hhp': ['hhp_carrier', 'hhp_color', 'hhp_storage', 'hhp_memory_ram'],
    'ref': ['ref_refrigerator_type', 'ref_capacity'],
    'ldy': ['ldy_loading_type', 'ldy_capacity'],
}

# sku 는 모든 테이블 공통 — NULL/공백 시만 채우는 정책.
_NULL_FILL_BASE = ['sku']

_VALID_PRODUCTS = tuple(_SPEC_COLS.keys())


def upsert_item_mst_batch(conn, product_lower: str, rows: Iterable[dict]) -> int:
    """rows (각 dict 에 item / account_name / product_url / sku / spec_cols 보유) 를
    dx_siel_{product}_item_mst 에 upsert.

    반환: 실제 INSERT/UPDATE 시도된 row 수 (item 또는 account_name 누락 row 는 skip).
    """
    product_lower = (product_lower or '').lower()
    if product_lower not in _VALID_PRODUCTS:
        return 0

    spec_cols = _SPEC_COLS[product_lower]
    null_fill_cols = _NULL_FILL_BASE + spec_cols
    insert_cols = ['item', 'account_name', 'product_url'] + null_fill_cols
    table = f'dx_siel_{product_lower}_item_mst'

    values_list = []
    for row in rows:
        item = row.get('item')
        account = row.get('account_name')
        item = item.strip() if isinstance(item, str) else item
        account = account.strip() if isinstance(account, str) else account
        if not item or not account:
            continue
        values = [
            item,
            account,
            row.get('product_url') or None,
        ]
        for col in null_fill_cols:
            v = row.get(col)
            if isinstance(v, str):
                v = v.strip() or None
            values.append(v)
        values_list.append(tuple(values))

    if not values_list:
        return 0

    cols_csv = ', '.join(insert_cols)
    placeholders = ', '.join(['%s'] * len(insert_cols))
    null_fill_set = ',\n  '.join(
        f"{col} = COALESCE(NULLIF({table}.{col}, ''), EXCLUDED.{col})"
        for col in null_fill_cols
    )
    sql = (
        f"INSERT INTO {table} ({cols_csv})\n"
        f"VALUES ({placeholders})\n"
        f"ON CONFLICT (item, account_name) DO UPDATE SET\n"
        f"  product_url = EXCLUDED.product_url,\n"
        f"  {null_fill_set},\n"
        f"  updated_at = NOW()"
    )

    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(cur, sql, values_list, page_size=50)

    return len(values_list)


def fill_from_mst(conn, product_lower: str, rows: list) -> int:
    """rows 의 NULL / 공백 spec 컬럼을 dx_siel_{product}_item_mst 의 기존 값으로 채움.

    fallback 대상: sku + 제품군별 spec 컬럼 (item_mst DDL 의 NULL fillable 컬럼).
    row 자체는 in-place 수정. 반환: 채운 (cell-level) 수.
    """
    product_lower = (product_lower or '').lower()
    if product_lower not in _VALID_PRODUCTS or not rows:
        return 0

    fill_cols = _NULL_FILL_BASE + _SPEC_COLS[product_lower]
    keys: list = []
    for row in rows:
        item = row.get('item')
        account = row.get('account_name')
        item = item.strip() if isinstance(item, str) else item
        account = account.strip() if isinstance(account, str) else account
        if item and account:
            keys.append((item, account))
    if not keys:
        return 0

    table = f'dx_siel_{product_lower}_item_mst'
    select_cols = ['item', 'account_name'] + fill_cols
    select_csv = ', '.join(select_cols)
    placeholders = ', '.join(['(%s, %s)'] * len(keys))
    flat_keys: list = []
    for k in keys:
        flat_keys.extend(k)
    sql = (
        f'SELECT {select_csv} FROM {table} '
        f'WHERE (item, account_name) IN ({placeholders})'
    )

    mst_data: dict = {}
    with conn.cursor() as cur:
        cur.execute(sql, flat_keys)
        for r in cur.fetchall():
            item, account = r[0], r[1]
            specs = {col: r[2 + i] for i, col in enumerate(fill_cols)}
            mst_data[(item, account)] = specs

    filled = 0
    for row in rows:
        key = (row.get('item'), row.get('account_name'))
        mst_specs = mst_data.get(key)
        if not mst_specs:
            continue
        for col in fill_cols:
            v = row.get(col)
            if isinstance(v, str):
                v = v.strip()
            if v in (None, ''):
                mst_v = mst_specs.get(col)
                if isinstance(mst_v, str):
                    mst_v = mst_v.strip() or None
                if mst_v not in (None, ''):
                    row[col] = mst_v
                    filled += 1
    return filled
