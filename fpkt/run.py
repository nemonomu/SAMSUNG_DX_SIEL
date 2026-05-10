"""
Flipkart 통합 크롤러 (SIEL).
listing → detail 한 프로세스 안 (driver 1 회 시작/종료).
listing 단계의 product_url 캡처 → detail 단계 입력.

사용:
  python fpkt/run.py --product hhp --stages main detail
  python fpkt/run.py --product tv  --stages bsr detail --max-detail 50
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
_results_path = None
_results_file = None

# streaming insert state — main/bsr cache + DB connection
_main_cache: dict = {}
_bsr_cache: dict = {}
_db_conn = None
_db_cursor = None
_streaming_enabled = False
# 8 운영 테이블 SQL (5/10 사용자 룰) — product 별 retail_com + product_list
_retail_sqls: dict = {}
_list_sqls: dict = {}


def _url_key(url: str) -> str:
    return (url or '').split('?', 1)[0].rstrip('/')


def _setup_db():
    """run 시작 시 DB connection long-lived. detail emit 마다 INSERT.
    5/10 사용자 룰: 본 운영 8 테이블 (4 retail_com + 4 product_list) 만 INSERT, test 제거."""
    global _db_conn, _db_cursor, _streaming_enabled, _retail_sqls, _list_sqls
    try:
        import psycopg2
        sys.path.insert(0, _ROOT)
        import config
        import insert_test_retail_com as ITR
        cfg = dict(config.DB_CONFIG)
        cfg.setdefault('database', 'postgres')
        cfg.setdefault('client_encoding', 'utf8')
        _db_conn = psycopg2.connect(**cfg)
        _db_cursor = _db_conn.cursor()
        # retail_com — product 별 컬럼 분기 (5/10 사용자 룰)
        _retail_sqls = {}
        for p in ITR.PRODUCT_LOWERS:
            cols_p = ITR.COLUMNS_BY_PRODUCT[p]
            cols_join = ', '.join(cols_p)
            placeholders_join = ', '.join(f'%({c})s' for c in cols_p)
            _retail_sqls[p] = f'INSERT INTO dx_siel_{p}_retail_com ({cols_join}) VALUES ({placeholders_join})'
        # product_list — 4 product 동일 컬럼
        cols_list = ', '.join(ITR.COLUMNS_LIST)
        placeholders_list = ', '.join(f'%({c})s' for c in ITR.COLUMNS_LIST)
        _list_sqls = {p: f'INSERT INTO dx_siel_{p}_product_list ({cols_list}) VALUES ({placeholders_list})'
                      for p in ITR.PRODUCT_LOWERS}
        _streaming_enabled = True
        print('[run.py] streaming INSERT enabled — 8 운영 테이블 (4 retail_com + 4 product_list)',
              file=sys.stderr)
    except Exception as e:
        print(f'[run.py] streaming INSERT setup failed: {type(e).__name__}: {e}', file=sys.stderr)
        _streaming_enabled = False


def _close_db():
    global _db_conn, _db_cursor, _streaming_enabled
    if _db_cursor is not None:
        try:
            _db_cursor.close()
        except Exception:
            pass
        _db_cursor = None
    if _db_conn is not None:
        try:
            _db_conn.close()
        except Exception:
            pass
        _db_conn = None
    _streaming_enabled = False


def _stream_insert(detail_rec: dict) -> None:
    """detail record 도착 즉시 main/bsr cache 와 merge → 8 운영 테이블 INSERT.
    product 별 retail_com (full columns) + product_list (detail 제외 columns) 둘 다."""
    if not _streaming_enabled or _db_cursor is None:
        return
    try:
        import insert_test_retail_com as ITR
    except Exception:
        return
    src = detail_rec.get('source_url') or detail_rec.get('product_url') or ''
    key = _url_key(src)
    main_rec = _main_cache.get(key)
    bsr_rec = _bsr_cache.get(key)
    # retail_com — full merge (main + bsr + detail)
    row_full = ITR.make_row(main_rec, bsr_rec, detail_rec)
    # product_list — main + bsr only (사용자 룰 5/10: detail 출처 컬럼 NULL)
    row_listing = ITR.make_row_listing(main_rec, bsr_rec)
    if not row_full and not row_listing:
        return
    prod_lower = (row_full or row_listing or {}).get('product', '').lower()
    if prod_lower not in _retail_sqls:
        return
    try:
        if row_full:
            _db_cursor.execute(_retail_sqls[prod_lower], row_full)
        if row_listing:
            _db_cursor.execute(_list_sqls[prod_lower], row_listing)
        _db_conn.commit()
    except Exception as e:
        try:
            _db_conn.rollback()
        except Exception:
            pass
        print(f'[run.py] streaming INSERT row failed: {type(e).__name__}: {e}', file=sys.stderr)


def _setup_results(product: str) -> None:
    """fpkt/logs/siel_flipkart_{product}_run_{ts}.jsonl 자동 생성. dual emit 으로 stdout + file 동시."""
    global _results_path, _results_file
    ts = datetime.now(_IST).strftime('%Y%m%d%H%M%S')
    logs_dir = os.path.join(_ROOT, 'fpkt', 'logs')
    os.makedirs(logs_dir, exist_ok=True)
    _results_path = os.path.join(logs_dir, f'siel_flipkart_{product}_run_{ts}.jsonl')
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


def _make_dual(orig_emit):
    """emit monkey-patch — original (stdout) + jsonl write + streaming INSERT (detail 마다)."""
    def dual(rec):
        orig_emit(rec)
        _write_results(rec)
        # streaming: main/bsr → cache, detail → 즉시 INSERT
        if not _streaming_enabled:
            return
        stage = rec.get('stage')
        url = rec.get('product_url') or rec.get('source_url') or ''
        key = _url_key(url)
        if not key:
            return
        if stage == 'main':
            _main_cache[key] = rec
        elif stage == 'bsr':
            _bsr_cache[key] = rec
        elif stage == 'detail':
            _stream_insert(rec)
    return dual


def _reset_caches() -> None:
    """product 변경 시 in-memory cache reset (다중 product run 사이)."""
    _main_cache.clear()
    _bsr_cache.clear()


def _auto_insert() -> None:
    """run 끝난 후 jsonl → dx_siel_test_retail_com INSERT."""
    insert_script = os.path.join(_ROOT, 'insert_test_retail_com.py')
    if not (_results_path and os.path.exists(_results_path)
            and os.path.exists(insert_script)):
        return
    print(f'[run.py] auto INSERT from {_results_path}', file=sys.stderr)
    try:
        subprocess.run([sys.executable, insert_script, _results_path, '999999'],
                       check=False, timeout=600)
    except Exception as e:
        print(f'[run.py] auto INSERT failed: {type(e).__name__}: {e}', file=sys.stderr)


def _auto_apply_sql():
    """run 시작 전 SQL latest 자동 적용 — post-merge hook 미설치 환경 안전장치.
    sql/*.sql idempotent (DROP 없음, IF NOT EXISTS / ON CONFLICT). sql/manual/ 은 자동 적용 X."""
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

from fpkt import listing as L
from fpkt import detail as D


def run_listing_capture(driver, product: str, stage: str,
                        max_rank: int, max_pages: int) -> list:
    captured: list = []
    original_emit = L.emit

    def capturing(rec):
        original_emit(rec)
        u = rec.get('product_url')
        if u:
            captured.append(u)

    L.emit = capturing
    try:
        if stage == 'main':
            base_url = L.MAIN_URL_TEMPLATES[product]
            rank_field = 'main_rank'
            mr = max_rank if max_rank is not None else 300
        else:
            base_url = L.BSR_URL_TEMPLATES[product]
            rank_field = 'bsr_rank'
            mr = max_rank if max_rank is not None else 100

        L.init_logging(product, stage)
        sels = L.load_selectors(L.SITE_ACCOUNT, stage, product)
        if not sels:
            L.emit({'_error': 'no selectors loaded',
                    'site': L.SITE_ACCOUNT, 'stage': stage, 'product': product})
            return captured
        batch_id = L.make_batch_id(stage, product)
        L.crawl_paged(driver, product, stage, base_url, sels, batch_id,
                      mr, max_pages, rank_field)
    finally:
        L.emit = original_emit
    return captured


def run_detail(driver, product: str, urls: list, sleep_s: float) -> int:
    D.init_logging(product)
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


def _run_one_product(driver, product: str, args) -> None:
    """단일 product 의 main/bsr/detail 처리 — driver 공유, cache reset, jsonl/INSERT 별개."""
    _reset_caches()
    _setup_results(product)
    captured: list = []
    seen = set()
    try:
        for stage in args.stages:
            if stage in ('main', 'bsr'):
                if stage == 'main':
                    mr = args.max_rank_main if args.max_rank_main is not None else args.max_rank
                else:
                    mr = args.max_rank_bsr if args.max_rank_bsr is not None else args.max_rank
                urls = run_listing_capture(driver, product, stage,
                                           mr, args.max_pages)
                added = 0
                for u in urls:
                    key = (u or '').split('?', 1)[0].rstrip('/')
                    if not key or key in seen:
                        continue
                    seen.add(key)
                    captured.append(u)
                    added += 1
                print(f'[run] product={product} stage={stage} captured={len(urls)} unique_added={added} total_unique={len(captured)}',
                      file=sys.stderr)
            else:  # detail
                use_urls = captured if args.max_detail is None else captured[:args.max_detail]
                if not use_urls:
                    D.emit({'_warn': 'no product_urls captured for detail',
                            'product': product})
                    continue
                print(f'[run] product={product} stage=detail processing={len(use_urls)} (dedupe 후)',
                      file=sys.stderr)
                run_detail(driver, product, use_urls, args.detail_sleep)
    finally:
        _close_results()


def main() -> int:
    ap = argparse.ArgumentParser(description='Flipkart 통합 크롤러')
    ap.add_argument('--product', nargs='+', required=True,
                    choices=['hhp', 'tv', 'ref', 'ldy'],
                    help='1개 이상 — 여러 개 주면 driver 공유하며 순차 처리')
    ap.add_argument('--stages', nargs='+', required=True,
                    choices=['main', 'bsr', 'detail'])
    ap.add_argument('--max-rank', type=int, default=None,
                    help='listing 단계 공통 max_rank (호환). default: main=300, bsr=100')
    ap.add_argument('--max-rank-main', type=int, default=None,
                    help='main 단계 max_rank. 있으면 --max-rank 보다 우선')
    ap.add_argument('--max-rank-bsr', type=int, default=None,
                    help='bsr 단계 max_rank. 있으면 --max-rank 보다 우선')
    ap.add_argument('--max-pages', type=int, default=30)
    ap.add_argument('--max-detail', type=int, default=None,
                    help='detail 단계 처리 URL 수 제한 (default 무제한)')
    ap.add_argument('--detail-sleep', type=float, default=2.0)
    ap.add_argument('--headless', action='store_true')
    ap.add_argument('--no-auto-insert', action='store_true',
                    help='streaming INSERT 비활성 (jsonl 만)')
    args = ap.parse_args()

    # dual emit (stdout + jsonl + streaming INSERT) — emit monkey-patch 1번만
    L.emit = _make_dual(L.emit)
    D.emit = _make_dual(D.emit)

    if not args.no_auto_insert:
        _setup_db()

    driver = L.make_driver(headless=args.headless)
    try:
        for product in args.product:
            print(f'\n=== [run] starting product={product} ===\n', file=sys.stderr)
            try:
                _run_one_product(driver, product, args)
            except Exception as e:
                traceback.print_exc(file=sys.stderr)
                print(f'[run] product={product} failed — 다음 product 진행', file=sys.stderr)
        return 0
    finally:
        try:
            driver.quit()
        except Exception:
            pass
        _close_results()
        _close_db()


if __name__ == '__main__':
    sys.exit(main())
