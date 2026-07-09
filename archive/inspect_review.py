"""saved HTML 의 review section 분석. 사용: python inspect_review.py <html_path>"""
import re, sys

h = open(sys.argv[1], encoding='utf-8').read()
keys = [
    'review-body', 'review-collapsed', 'review-text-content', 'review-text',
    'reviewsMedley', 'customer_review-', 'data-hook="review"', 'aspect-summary',
    'cr-top-reviews', '_cr-top-reviews_style',
]
for k in keys:
    print(k, h.count(k))

print()
print('--- first customer_review- div (5000 chars) ---')
m = re.search(r'<div[^>]*id=.customer_review-.{0,5000}', h, re.DOTALL)
print(m.group(0)[:5000] if m else 'no match')
