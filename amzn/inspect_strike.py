"""
Amazon detail snapshot 에서 strike-through (M.R.P.) 가격 영역 dom 추출 진단.

OSP 결함 진단용 — selector 가 어떤 영역에서 가격 잡는지 확인.
- data-a-strike="true" 매치 위치 + 컨텍스트
- 특정 가격 (예: ₹17,999) 매치 위치 + 컨텍스트
- corePriceDisplay / centerCol scope 검증

사용:
  py -3 amzn\inspect_strike.py amzn\logs\siel_amazon_hhp_detail_*_post.html ₹17,999
"""
from __future__ import annotations
import re
import sys
from pathlib import Path

if len(sys.argv) < 2:
    print('usage: py -3 amzn/inspect_strike.py <detail_post_html> [keyword]')
    sys.exit(2)

path = Path(sys.argv[1])
keyword = sys.argv[2] if len(sys.argv) >= 3 else None

if not path.exists():
    print(f'file not found: {path}')
    sys.exit(2)

h = path.read_text(encoding='utf-8', errors='replace')
print(f'file: {path.name}  ({len(h):,} chars)\n')

PATTERNS = [
    ('data-a-strike="true"', 'strike-through 가격 wrapper'),
    ('a-text-strike',        'strike-through CSS class'),
    ('apex-pricetopay-accessibility-label', 'apex 가격 영역 (FSP)'),
    ('id="corePriceDisplay_desktop_feature_div"', 'corePriceDisplay scope'),
    ('id="centerCol"',       'centerCol scope'),
    ('a-price-symbol',       'a-price-symbol (₹)'),
]

if keyword:
    PATTERNS.append((keyword, f'사용자 검색 키워드'))

for kw, label in PATTERNS:
    ms = list(re.finditer(re.escape(kw), h))
    print(f'=== {label}: {kw!r}  ({len(ms)} hits) ===')
    for m in ms[:5]:
        s = max(0, m.start() - 200)
        e = min(len(h), m.end() + 300)
        ctx = h[s:e].replace('\n', ' ').replace('\r', ' ')
        ctx = re.sub(r'\s+', ' ', ctx)
        print(f'  @{m.start()}: ...{ctx}...')
    print()
