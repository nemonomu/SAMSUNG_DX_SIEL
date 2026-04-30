import os
import re

import sys
import glob

# 최신 _spec.html 우선, 없으면 일반 detail html
_logs = sorted(glob.glob('fpkt/logs/siel_flipkart_ref_detail_*_spec.html'))
if not _logs:
    _logs = sorted(glob.glob('fpkt/logs/siel_flipkart_ref_detail_*.html'))
    _logs = [p for p in _logs if not p.endswith('_review.html') and not p.endswith('_spec.html')]
if not _logs:
    print('no html snapshot found in fpkt/logs/')
    sys.exit(1)
HTML = _logs[-1]

with open(HTML, encoding='utf-8') as f:
    html = f.read()

print(f'== {HTML} size={len(html)} ==\n')

KW = [
    'Specifications', 'Refrigerator Type', 'Type', 'Capacity',
    'Total Capacity', 'Refrigerator Capacity', 'Litre', 'L)',
    'Side-by-Side', 'Single Door', 'Double Door',
]

for kw in KW:
    print(f'=== {kw!r} ===')
    n = 0
    for m in re.finditer(re.escape(kw), html):
        idx = m.start()
        ctx = html[max(0, idx - 120):idx + 300]
        ctx = re.sub(r'\s+', ' ', ctx)
        print(f'  [{idx}] ...{ctx}...')
        n += 1
        if n >= 3:
            break
    if n == 0:
        print('  NOT FOUND')
    print()
