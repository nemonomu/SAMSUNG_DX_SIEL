"""
Amazon.In 통합 크롤러 (SIEL).
listing → detail 한 프로세스 안 (driver 1 회 시작/종료).
listing 단계에서 추출된 product_url 을 모아서 detail 단계 입력으로 사용.

사용:
  python amzn/run.py --product hhp --stages main detail
  python amzn/run.py --product tv  --stages bsr detail --max-detail 50
  python amzn/run.py --product ref --stages main           # listing 만
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import traceback

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from amzn import listing as L
from amzn import detail as D


def run_listing_capture(driver, product: str, stage: str,
                        max_rank: int, max_pages: int) -> list:
    """listing 실행 + product_url 캡처. emit monkey-patch."""
    captured: list = []
    original_emit = L.emit

    def capturing(rec):
        original_emit(rec)
        u = rec.get('product_url')
        if u:
            captured.append(u)

    L.emit = capturing
    try:
        sels = L.load_selectors(L.SITE_ACCOUNT, stage, product)
        if not sels:
            L.emit({'_error': 'no selectors loaded',
                    'site': L.SITE_ACCOUNT, 'stage': stage, 'product': product})
            return captured
        batch_id = L.make_batch_id(stage, product)
        if stage == 'main':
            L.crawl_main(driver, product, sels, batch_id,
                         max_rank=max_rank, max_pages=max_pages)
        else:
            L.crawl_bsr(driver, product, sels, batch_id)
    finally:
        L.emit = original_emit
    return captured


def run_detail(driver, product: str, urls: list, sleep_s: float) -> int:
    sels = D.load_selectors(D.SITE_ACCOUNT, D.STAGE, product)
    batch_id = D.make_batch_id(product)
    if not sels:
        D.emit({'_error': 'no selectors loaded',
                'site': D.SITE_ACCOUNT, 'stage': D.STAGE,
                'product': product, 'batch_id': batch_id})
        return 0
    n = 0
    for u in urls:
        rec = D.crawl_detail(driver, product, u, sels, batch_id)
        D.emit(rec)
        n += 1
        if sleep_s > 0:
            time.sleep(sleep_s)
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description='Amazon.In 통합 크롤러')
    ap.add_argument('--product', required=True, choices=['hhp', 'tv', 'ref', 'ldy'])
    ap.add_argument('--stages', nargs='+', required=True,
                    choices=['main', 'bsr', 'detail'])
    ap.add_argument('--max-rank', type=int, default=300)
    ap.add_argument('--max-pages', type=int, default=30)
    ap.add_argument('--max-detail', type=int, default=None,
                    help='detail 단계 처리 URL 수 제한 (default 무제한)')
    ap.add_argument('--detail-sleep', type=float, default=2.0)
    ap.add_argument('--headless', action='store_true')
    args = ap.parse_args()

    driver = L.make_driver(headless=args.headless)
    captured: list = []
    try:
        for stage in args.stages:
            if stage in ('main', 'bsr'):
                urls = run_listing_capture(driver, args.product, stage,
                                           args.max_rank, args.max_pages)
                captured.extend(urls)
            else:  # detail
                use_urls = captured if args.max_detail is None else captured[:args.max_detail]
                if not use_urls:
                    D.emit({'_warn': 'no product_urls captured for detail',
                            'product': args.product})
                    continue
                run_detail(driver, args.product, use_urls, args.detail_sleep)
        return 0
    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        return 1
    finally:
        try:
            driver.quit()
        except Exception:
            pass


if __name__ == '__main__':
    sys.exit(main())
