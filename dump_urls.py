"""jsonl 의 stage record 에서 URL 추출. selector fix 검증용 sample URL 파일 생성.

사용:
  python dump_urls.py amzn/logs/siel_amazon_tv_run_20260503163628.jsonl --max 5 > urls_tv.txt
  # main 단계의 discount_type 채워진 product 만 (dealBadge 검증용):
  python dump_urls.py amzn/logs/siel_amazon_tv_run_20260503112635.jsonl \\
      --stage main --has discount_type --max 2 > urls_tv_deal.txt
  python amzn/detail.py --product tv --urls-file urls_tv_deal.txt > test_tv_deal_detail.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('jsonl', help='소스 jsonl (run 결과)')
    ap.add_argument('--stage', default='detail', help='추출 대상 stage (default: detail)')
    ap.add_argument('--max', type=int, default=0, help='최대 N 개 (0 = 무제한)')
    ap.add_argument('--has', action='append', default=[],
                    help='해당 field 가 None/빈 아닌 record 만 (반복 가능, 다중 AND)')
    args = ap.parse_args()

    n = 0
    seen = set()
    with open(args.jsonl, encoding='utf-8') as f:
        for line in f:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get('stage') != args.stage:
                continue
            # detail stage record 는 source_url 에 URL, product_url=None.
            # main/bsr stage record 는 product_url 에 URL, source_url=상위 페이지 URL.
            url = r.get('source_url') if args.stage == 'detail' else r.get('product_url')
            if not url or url in seen:
                continue
            if any(not r.get(f) for f in args.has):
                continue
            seen.add(url)
            print(url)
            n += 1
            if args.max and n >= args.max:
                break
    return 0


if __name__ == '__main__':
    sys.exit(main())
