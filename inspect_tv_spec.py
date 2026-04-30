import os
import re

HTML = 'fpkt/logs/siel_flipkart_tv_detail_2604301504.html'

with open(HTML, encoding='utf-8') as f:
    html = f.read()

print(f'== {HTML} size={len(html)} ==\n')

KW = [
    'Specifications', 'Display Size', 'Screen Size', 'Launch Year',
    'Year of Launch', 'Year', 'Power Consumption', 'Annual Energy',
    '% off', 'Energy', 'Display', 'inch',
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
