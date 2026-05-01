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
import json
import os
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_IST = timezone(timedelta(hours=5, minutes=30))

# emit dual write — stdout JSONL (사용자 redirect) + 자동 results file (auto INSERT 용)
_results_path = None
_results_file = None


def _setup_results(product: str) -> None:
    global _results_path, _results_file
    ts = datetime.now(_IST).strftime('%Y%m%d%H%M%S')
    logs_dir = os.path.join(_ROOT, 'amzn', 'logs')
    os.makedirs(logs_dir, exist_ok=True)
    _results_path = os.path.join(logs_dir, f'siel_amazon_{product}_run_{ts}.jsonl')
    _results_file = open(_results_path, 'w', encoding='utf-8')


def _write_results(rec: dict) -> None:
    if _results_file is None:
        return
    try:
        _results_file.write(json.dumps(rec, ensure_ascii=False) + '\n')
        _results_file.flush()
    except Exception:
        pass


def _close_results() -> None:
    global _results_file
    if _results_file is not None:
        try:
            _results_file.close()
        except Exception:
            pass
        _results_file = None


def _auto_insert() -> None:
    """run 끝난 후 results jsonl 의 모든 row 를 dx_siel_test_retail_com 에 INSERT."""
    if not (_results_path and os.path.exists(_results_path)):
        return
    insert_script = os.path.join(_ROOT, 'insert_test_retail_com.py')
    if not os.path.exists(insert_script):
        return
    print(f'[run.py] auto INSERT to dx_siel_test_retail_com from {_results_path}',
          file=sys.stderr)
    try:
        subprocess.run([sys.executable, insert_script, _results_path, '999999'],
                       check=False, timeout=600)
    except Exception as e:
        print(f'[run.py] auto INSERT failed: {type(e).__name__}: {e}', file=sys.stderr)


def _auto_apply_sql():
    """run 시작 전 SQL latest 자동 적용 — post-merge hook 미설치 환경 안전장치.
    DROP+CREATE+INSERT idempotent 라 매 run OK. 실패 시 (DB down 등) skip — crawler 는 진행."""
    sql_path = os.path.join(_ROOT, 'sql', 'dx_siel_xpath_selectors.sql')
    apply_script = os.path.join(_ROOT, 'apply_sql.py')
    if not (os.path.exists(sql_path) and os.path.exists(apply_script)):
        return
    try:
        subprocess.run([sys.executable, apply_script, sql_path],
                       check=False, timeout=60)
    except Exception as e:
        print(f'[run.py] auto apply_sql skip: {type(e).__name__}: {e}', file=sys.stderr)


_auto_apply_sql()

from amzn import listing as L
from amzn import detail as D


def _sync_logs_to_retail_com() -> None:
    """logs 폴더를 retail_com 으로 sync (폴더 있으면). silent best-effort, 회귀 위험 0."""
    try:
        import shutil
        src = os.path.join(_ROOT, 'amzn', 'logs')
        dst = os.path.expanduser(
            r'~\Documents\퀵오일\삼성전자\samsung_dx_retail_com\siel\logs')
        if not os.path.isdir(src) or not os.path.isdir(dst):
            return
        for fname in os.listdir(src):
            sp = os.path.join(src, fname)
            if os.path.isfile(sp):
                shutil.copy2(sp, os.path.join(dst, fname))
    except Exception:
        pass


def run_listing_capture(driver, product: str, stage: str,
                        max_rank: int, max_pages: int) -> list:
    """listing 실행 + product_url 캡처. emit monkey-patch."""
    L.init_logging(product, stage)
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
    D.init_logging(product)
    D.init_progress(len(urls))
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

    # results jsonl + emit dual write (stdout 그대로 + 자동 file → finally 에 INSERT)
    _setup_results(args.product)
    _orig_L_emit = L.emit
    _orig_D_emit = D.emit

    def _L_dual(rec):
        _orig_L_emit(rec)
        _write_results(rec)

    def _D_dual(rec):
        _orig_D_emit(rec)
        _write_results(rec)

    L.emit = _L_dual
    D.emit = _D_dual

    driver = L.make_driver(headless=args.headless)
    captured: list = []
    seen = set()  # url path dedupe — main+bsr 중복 제거 (Amazon 은 ASIN 으로 dedupe)
    try:
        for stage in args.stages:
            if stage in ('main', 'bsr'):
                urls = run_listing_capture(driver, args.product, stage,
                                           args.max_rank, args.max_pages)
                added = 0
                for u in urls:
                    # Amazon 은 ASIN 만 비교 (URL query/path 변동 무관)
                    import re as _re
                    m = _re.search(r'/(?:dp|gp/product)/([A-Z0-9]{10})', u or '')
                    key = m.group(1) if m else (u or '').split('?', 1)[0].rstrip('/')
                    if not key or key in seen:
                        continue
                    seen.add(key)
                    captured.append(u)
                    added += 1
                print(f'[run] stage={stage} captured={len(urls)} unique_added={added} total_unique={len(captured)}',
                      file=sys.stderr)
            else:  # detail
                use_urls = captured if args.max_detail is None else captured[:args.max_detail]
                if not use_urls:
                    D.emit({'_warn': 'no product_urls captured for detail',
                            'product': args.product})
                    continue
                print(f'[run] stage=detail processing={len(use_urls)} (dedupe 후)',
                      file=sys.stderr)
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
        _close_results()
        _sync_logs_to_retail_com()
        _auto_insert()


if __name__ == '__main__':
    sys.exit(main())
