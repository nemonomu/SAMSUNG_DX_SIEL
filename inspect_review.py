"""saved HTML 의 review section 분석. 사용: python inspect_review.py <html_path>"""
import re, sys

h = open(sys.argv[1], encoding='utf-8').read()
keys = [
    'review-body', 'reviewsMedley', 'customer_review-', 'review-collapsed',
    'reviews-medley-widget', 'aspect-summary',
    'review-text-content', 'data-hook="review-body"',
    'data-hook="review-text-content"', 'cm_cr-review_list',
]
for k in keys:
    print(k, h.count(k))

print('--- first customer_review- div (1500 chars) ---')
m = re.search(r'<div[^>]*id=.customer_review-.{0,1500}', h, re.DOTALL)
print(m.group(0)[:1500] if m else 'no match')
