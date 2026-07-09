"""sku 추출 진단 — sku 가 retailer_sku_name 부분문자열인지 + URL 샘플 추출.

전제: sku 는 detail page 의 Specifications → General → Model Name 에서만 추출 (전 제품 공통).
listing 카드/시리즈명 추출 X.

사용: python inspect_sku_unique.py
"""
import os, sys, re
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import psycopg2
import config

cfg = dict(config.DB_CONFIG)
cfg.setdefault('database', 'postgres')
conn = psycopg2.connect(**cfg)

PRODUCTS = ('TV', 'HHP', 'REF', 'LDY')

with conn.cursor() as cur:
    print('=== sku NULL ratio (전체 row, account × product × page_type) ===')
    cur.execute("""
        SELECT account_name, product, page_type,
               COUNT(*) AS rows,
               COUNT(*) FILTER (WHERE sku IS NULL) AS null_sku
          FROM dx_siel_test_retail_com
         WHERE account_name IS NOT NULL
         GROUP BY account_name, product, page_type
         ORDER BY account_name, product, page_type
    """)
    for r in cur.fetchall():
        rows, nn = r[3], r[4]
        pct = (nn * 100 // rows) if rows else 0
        print(f'  {r[0]:<10} {r[1]:<4} {r[2] or "-":<6} rows={rows:>4}  null_sku={nn:>4}  ({pct}%)')

    print()
    print('=== sku 가 retailer_sku_name 부분문자열인 비율 (Flipkart main) ===')
    print('  → detail Model Name 추출 정상이면 sku 는 모델 코드 (이름 부분문자열 X).')
    print('  → fallback 으로 시리즈명이 들어왔다면 sku ⊂ retailer_sku_name.')
    cur.execute("""
        SELECT product,
               COUNT(*) AS rows,
               COUNT(*) FILTER (WHERE sku IS NOT NULL AND retailer_sku_name IS NOT NULL
                                  AND retailer_sku_name ILIKE '%' || sku || '%') AS substr_hit,
               COUNT(*) FILTER (WHERE sku IS NULL) AS null_sku
          FROM dx_siel_test_retail_com
         WHERE account_name = 'Flipkart' AND page_type = 'main'
         GROUP BY product
         ORDER BY product
    """)
    for r in cur.fetchall():
        prod, rows, hit, nn = r
        pct_hit = (hit * 100 // rows) if rows else 0
        pct_nn = (nn * 100 // rows) if rows else 0
        print(f'  {prod:<4} rows={rows:>4}  substr_hit={hit:>4} ({pct_hit}%)  null_sku={nn:>4} ({pct_nn}%)')

    # HHP dup sku group 들의 sku vs retailer_sku_name 첫 단어 매칭 패턴
    print()
    print('=== Flipkart HHP dup sku samples (8 group) ===')
    cur.execute("""
        WITH dup AS (
            SELECT sku FROM dx_siel_test_retail_com
             WHERE account_name = 'Flipkart' AND product = 'HHP' AND page_type = 'main'
               AND sku IS NOT NULL
             GROUP BY sku HAVING COUNT(*) > 1
             ORDER BY COUNT(*) DESC, sku
             LIMIT 8
        )
        SELECT t.sku, t.item, t.retailer_sku_name, t.product_url
          FROM dx_siel_test_retail_com t
          JOIN dup d ON t.sku = d.sku
         WHERE t.account_name = 'Flipkart' AND t.product = 'HHP' AND t.page_type = 'main'
         ORDER BY t.sku, t.main_rank NULLS LAST
    """)
    rows = cur.fetchall()
    by_sku = {}
    for sku, item, name, url in rows:
        by_sku.setdefault(sku, []).append((item, name, url))
    for sku, group in by_sku.items():
        print(f'\n  sku={sku!r}  (n={len(group)})')
        for item, name, url in group[:3]:
            print(f'    item={item}  name={(name or "")[:90]!r}')
            # URL 만 출력 (사용자가 직접 detail page 의 Model Name 확인용)
            print(f'      url={url}')

    # 비교용: 같은 product 의 dup 안 된 sku 샘플 — sku 가 어떤 모양인지
    print()
    print('=== Flipkart HHP unique sku samples (5) — 정상 sku 모양 비교용 ===')
    cur.execute("""
        WITH uniq AS (
            SELECT sku FROM dx_siel_test_retail_com
             WHERE account_name = 'Flipkart' AND product = 'HHP' AND page_type = 'main'
               AND sku IS NOT NULL
             GROUP BY sku HAVING COUNT(*) = 1
             ORDER BY sku LIMIT 5
        )
        SELECT t.sku, t.item, t.retailer_sku_name
          FROM dx_siel_test_retail_com t
          JOIN uniq u USING(sku)
         WHERE t.account_name = 'Flipkart' AND t.product = 'HHP' AND t.page_type = 'main'
    """)
    for sku, item, name in cur.fetchall():
        print(f'  sku={sku!r}  item={item}  name={(name or "")[:80]!r}')

conn.close()
