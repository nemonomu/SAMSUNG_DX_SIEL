"""saved spec html 의 'General' 섹션 안에서 모든 label/value 쌍 추출.
사용: python inspect_general_labels.py <html_path>
"""
import re, sys
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

path = sys.argv[1]
h = open(path, encoding='utf-8').read()

# General 섹션 위치
m = re.search(r'>General<', h)
if not m:
    print('  no General section header found')
    sys.exit(0)
print(f'General header pos: {m.start()}')

# General 다음 4만자 안에서 모든 <div>LABEL</div><div>VALUE</div> 추출
section = h[m.end(): m.end() + 40000]
pat = re.compile(r'<div[^>]*>([A-Z][A-Za-z0-9 \-/&()]{1,40})</div>\s*<div[^>]*>([^<]{1,200})</div>')
seen = set()
for label, value in pat.findall(section):
    key = (label.strip(), value.strip())
    if key in seen:
        continue
    seen.add(key)
    print(f'  {label!r:<40}  →  {value!r}')
