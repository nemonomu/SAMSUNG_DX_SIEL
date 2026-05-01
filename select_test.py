"""dx_siel_test_retail_com 의 최근 N 행 select. 사용: python select_test.py [N=10]"""
import os, sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import psycopg2
import config

n = int(sys.argv[1]) if len(sys.argv) > 1 else 10
cols = ['product', 'item', 'sku', 'retailer_sku_name', 'star_rating',
        'count_of_star_ratings', 'main_rank', 'hhp_storage',
        'number_of_units_purchased_past_month', 'detailed_review_content']
cfg = dict(config.DB_CONFIG)
cfg.setdefault('database', 'postgres')
conn = psycopg2.connect(**cfg)
with conn.cursor() as cur:
    cur.execute(f"SELECT {', '.join(cols)} FROM dx_siel_test_retail_com ORDER BY id DESC LIMIT {n}")
    rows = cur.fetchall()
conn.close()

print(f'rows: {len(rows)}')
for r in rows:
    d = dict(zip(cols, r))
    drc = d.get('detailed_review_content')
    drc_short = (drc[:60] + '...') if drc and len(drc) > 60 else drc
    print()
    print(f"  product={d['product']} item={d['item']} sku={d['sku']} main_rank={d['main_rank']}")
    print(f"  name={(d['retailer_sku_name'] or '')[:80]}")
    print(f"  star={d['star_rating']} ratings={d['count_of_star_ratings']} units={d['number_of_units_purchased_past_month']}")
    print(f"  storage={d['hhp_storage']} review={drc_short!r}")
