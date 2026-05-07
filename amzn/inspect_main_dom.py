"""
Amazon main snapshot 의 popularity / status 표지 dom 추출 진단.

키워드별 출현 횟수 + 첫 3개 매치 주변 컨텍스트 (-80 ~ +300 chars) 출력.
sku_popularity / sku_status xpath 가 0% fill 인 원인 분석용.

사용:
  py -3 amzn\inspect_main_dom.py amzn\logs\siel_amazon_hhp_main_2605070810.html
"""
from __future__ import annotations
import re
import sys
from pathlib import Path

if len(sys.argv) < 2:
    print('usage: py -3 amzn/inspect_main_dom.py <main_snapshot.html>')
    sys.exit(2)

path = Path(sys.argv[1])
if not path.exists():
    print(f'file not found: {path}')
    sys.exit(2)

h = path.read_text(encoding='utf-8', errors='replace')
print(f'file: {path.name}  ({len(h):,} chars)\n')

PATTERNS = [
    ('Best Seller',          'sku_popularity primary text'),
    ('Bestseller',           'sku_popularity 변형'),
    ("Amazon's Choice",      'sku_popularity primary aria (straight quote)'),
    ('Amazon’s Choice', 'sku_popularity primary aria (curly quote)'),
    ('amazons-choice-label', 'sku_popularity fallback id'),
    ('puis-sponsored-label', 'sku_status primary class'),
    ('>Sponsored<',          'sku_status primary text (raw)'),
    ('aria-label="Sponsored', 'sku_status fallback aria'),
    ('#1 Best Seller',       'BSR 가능 표지'),
    ('Top Rated',            'Top Rated 표지'),
]

for kw, label in PATTERNS:
    ms = list(re.finditer(re.escape(kw), h, flags=re.IGNORECASE))
    print(f'=== {label}: {kw!r}  ({len(ms)} hits) ===')
    for m in ms[:3]:
        s = max(0, m.start() - 80)
        e = min(len(h), m.end() + 300)
        ctx = h[s:e].replace('\n', ' ').replace('\r', ' ')
        ctx = re.sub(r'\s+', ' ', ctx)
        print(f'  @{m.start()}: ...{ctx}...')
    print()
