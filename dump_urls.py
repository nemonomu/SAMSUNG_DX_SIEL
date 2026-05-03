"""jsonl 의 detail stage record 에서 product_url 추출. selector fix 검증용 sample URL 파일 생성.

사용:
  python dump_urls.py amzn/logs/siel_amazon_tv_run_20260503163628.jsonl --max 5 > urls_tv.txt
  python amzn/detail.py --product tv --urls-file urls_tv.txt > test_tv_detail.jsonl
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
            url = r.get('product_url')
            if not url or url in seen:
                continue
            seen.add(url)
            print(url)
            n += 1
            if args.max and n >= args.max:
                break
    return 0


if __name__ == '__main__':
    sys.exit(main())
