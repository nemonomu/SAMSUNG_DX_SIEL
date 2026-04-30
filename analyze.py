"""run 결과 jsonl 분석. 사용: python analyze.py <jsonl_path>"""
import json, sys

r = [json.loads(l) for l in open(sys.argv[1], encoding='utf-8') if l.strip()]
d = [x for x in r if x.get('stage') == 'detail']
g = lambda f: sum(1 for x in d if not x.get(f))
pi = lambda v: int(str(v).replace(',', '')) if v and str(v).replace(',', '').isdigit() else 0
regr = [x for x in d if not x.get('detailed_review_content') and pi(x.get('count_of_star_ratings')) > 100]

print(f"total {len(r)}  main {sum(1 for x in r if x.get('stage') == 'main')}  bsr {sum(1 for x in r if x.get('stage') == 'bsr')}  detail {len(d)}")
print(f"null  detailed {g('detailed_review_content')}  summarized {g('summarized_review_content')}  similar {g('retailer_sku_name_similar')}  trade_in {g('trade_in')}  star {g('star_rating')}  sku {g('sku')}")
print(f"regression suspect (ratings>100, detailed null): {len(regr)}")
for x in regr[:20]:
    print(f"  {x.get('asin')}  ratings={x.get('count_of_star_ratings')}")
