"""TV detail HTML 의 spec 영역 모든 라벨 dump.
modern Flipkart pattern: <div ...>LABEL:</div><div ...>VALUE</div>
"""
import os
import re
import glob

cands = sorted(glob.glob('fpkt/logs/siel_flipkart_tv_detail_*.html'))
cands = [c for c in cands if '_review' not in c]
HTML = cands[-1] if cands else 'fpkt/logs/siel_flipkart_tv_detail.html'
print(f'== {HTML} ==\n')

with open(HTML, encoding='utf-8') as f:
    html = f.read()

# 1) 모든 <div ...>LABEL:</div> 라벨 dump
labels = []
for m in re.finditer(r'<div[^>]*?>([A-Z][^<>]{2,80}?):</div>', html):
    labels.append(m.group(1))
seen = set()
uniq = []
for l in labels:
    if l not in seen:
        seen.add(l)
        uniq.append(l)
print(f'== div label "X:" total={len(labels)} unique={len(uniq)} ==')
for l in uniq:
    print(f'  {l!r}')

# 2) 전기/연도/사이즈 키워드 sweep — 어디 라벨에 들어가는지
print('\n== keyword sweep (label 외 모든 element) ==')
KW = ['Power', 'Watt', 'Energy', 'Consumption', 'Year', 'Launch',
      'Model', 'Display', 'Screen', 'Size', 'Wattage']
for k in KW:
    print(f'\n=== {k!r} ===')
    n = 0
    for m in re.finditer(k, html):
        idx = m.start()
        ctx = html[max(0, idx - 60):idx + 200]
        ctx = re.sub(r'\s+', ' ', ctx)
        print(f'  [{idx}] ...{ctx}...')
        n += 1
        if n >= 4:
            break
    if n == 0:
        print('  NOT FOUND')
