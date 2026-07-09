"""saved spec html 에서 'Model Name' / 'Model Number' label 주변 dom 추출.
사용: python inspect_model_label.py <html_path>
"""
import re, sys
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

path = sys.argv[1]
h = open(path, encoding='utf-8').read()

for label in ('Model Name', 'Model Number'):
    print(f'\n=== {label!r} ===')
    # label 텍스트 정확 매치 + 주변 200자
    for m in re.finditer(re.escape(label), h):
        start = max(0, m.start() - 200)
        end = min(len(h), m.end() + 400)
        snippet = h[start:end]
        # tag 들 readable 하게 (긴 attr 줄임)
        snippet = re.sub(r'\s+', ' ', snippet)
        print(f'\n  pos={m.start()}:')
        print(f'  ...{snippet[:600]}...')

    # xpath-likely 패턴: <div>{label}</div> 다음 sibling div
    pat = re.compile(
        r'<div[^>]*>' + re.escape(label) + r'</div>\s*<div[^>]*>([^<]{1,200})</div>'
    )
    hits = pat.findall(h)
    print(f'\n  pattern <div>{label}</div><div>VALUE</div> hits: {len(hits)}')
    for v in hits[:5]:
        print(f'    value = {v!r}')
