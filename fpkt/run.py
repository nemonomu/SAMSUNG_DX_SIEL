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
import re
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_IST = timezone(timedelta(hours=5, minutes=30))
_FPKT_PID_RE = re.compile(r'[?&]pid=([A-Z0-9]+)')
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


def _fsn_from_url(url: str) -> str:
    m = _FPKT_PID_RE.search(url or '')
    return m.group(1) if m else ''


def _listing_key_from_url(url: str) -> str:
    return _fsn_from_url(url) or _url_key(url)


def _listing_key_from_rec(rec: dict) -> str:
    url = rec.get('product_url') or rec.get('source_url') or ''
    return rec.get('fsn') or _listing_key_from_url(url)


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
        columns_by_product = getattr(ITR, 'COLUMNS_BY_PRODUCT_FPKT', ITR.COLUMNS_BY_PRODUCT)
        for p in ITR.PRODUCT_LOWERS:
            cols_p = columns_by_product[p]
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
    key = _listing_key_from_rec(detail_rec)
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
        key = _listing_key_from_rec(rec)
        if not key:
            return
        if stage == 'main':
            _main_cache.setdefault(key, rec)
        elif stage == 'bsr':
            _bsr_cache.setdefault(key, rec)
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
    seen_keys: set = set()
    stats = {'emitted': 0, 'with_id': 0, 'with_url': 0}
    missing_url_ranks: list = []
    duplicate_ranks: list = []
    rank_field = 'bsr_rank' if stage == 'bsr' else 'main_rank'
    original_emit = L.emit

    def capturing(rec):
        original_emit(rec)
        stats['emitted'] += 1
        rank = rec.get(rank_field)
        u = rec.get('product_url')
        key = _listing_key_from_rec(rec)
        if rec.get('fsn') or _fsn_from_url(u or ''):
            stats['with_id'] += 1
        if u:
            stats['with_url'] += 1
            captured.append(u)
        else:
            missing_url_ranks.append(rank)
        if key:
            if key in seen_keys:
                duplicate_ranks.append(rank)
            else:
                seen_keys.add(key)

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
        print(
            f'[run] product={product} stage={stage} raw_summary '
            f'emitted={stats["emitted"]} fsn={stats["with_id"]} '
            f'url={stats["with_url"]} unique_keys={len(seen_keys)} '
            f'missing_url_ranks={missing_url_ranks[:20]} '
            f'duplicate_ranks={duplicate_ranks[:20]}',
            file=sys.stderr)
    return captured


def _list_chrome_pids() -> set:
    """현재 OS 의 chrome.exe PID set. tasklist CSV 출력 파싱."""
    try:
        out = subprocess.run(
            ['tasklist', '/FI', 'IMAGENAME eq chrome.exe', '/FO', 'CSV', '/NH'],
            capture_output=True, text=True, timeout=10)
        pids = set()
        for line in out.stdout.splitlines():
            parts = [p.strip('"') for p in line.split('","')]
            if len(parts) >= 2:
                pid_s = parts[1].strip('"').strip()
                if pid_s.isdigit():
                    pids.add(int(pid_s))
        return pids
    except Exception:
        return set()


def _make_driver_tracked(headless: bool):
    """L.make_driver 래퍼 — make_driver 전후 의 chrome.exe PID 스냅샷 diff 로 본 driver
    의 chrome PID 들 추적. driver._fpkt_chrome_pids 에 저장 — _hard_kill_driver 가 사용."""
    before = _list_chrome_pids()
    drv = L.make_driver(headless=headless)
    # chrome 부팅 (browser + gpu + utility + crashpad) 안정화 대기
    time.sleep(3)
    after = _list_chrome_pids()
    drv._fpkt_chrome_pids = after - before
    print(f'[run] new driver chrome.exe PIDs tracked={sorted(drv._fpkt_chrome_pids)}',
          file=sys.stderr)
    return drv


def _hard_kill_driver(driver) -> None:
    """driver.quit() + process tree 강제 정리.
    5/12 사용자 evidence #1 (commit f2bf00e) — driver.quit() 만 으로 chrome 자식 안 죽는
    케이스 다수. PID 트리 kill 추가. 그러나 evidence #2 (5/12 실측) — chromedriver service
    PID 의 /T 트리 kill 이 chrome 안 잡음. undetected_chromedriver 는 chrome 을
    chromedriver 자식 이 아닌 sibling 으로 분리 띄움 → service.process /T 가 안 닿음.

    fix — make_driver 호출 전/후 의 chrome.exe PID 스냅샷 diff 로 정확한 chrome PID 추적
    (driver._fpkt_chrome_pids). 병렬 4 도메인 케이스 에서도 다른 도메인 chrome 안 건드림
    (diff 가 우리 새 chrome 만 잡음)."""
    pids = set()
    # 우리 의 chrome.exe PIDs (snapshot diff 로 추적 됨)
    try:
        pids.update(getattr(driver, '_fpkt_chrome_pids', set()) or set())
    except Exception:
        pass
    # chromedriver service process — chrome sibling 이지만 어쨌든 같이 정리
    try:
        sp = getattr(getattr(driver, 'service', None), 'process', None)
        if sp is not None and hasattr(sp, 'pid'):
            pids.add(sp.pid)
    except Exception:
        pass
    # uc browser_pid (혹시 노출 되면)
    try:
        bpid = getattr(driver, 'browser_pid', None)
        if bpid is not None:
            pids.add(bpid)
    except Exception:
        pass
    try:
        driver.quit()
    except Exception:
        pass
    for pid in pids:
        try:
            subprocess.run(['taskkill', '/F', '/PID', str(pid), '/T'],
                           capture_output=True, timeout=5)
        except Exception:
            pass


def run_detail(driver, product: str, urls: list, sleep_s: float,
               headless: bool, restart_every: int = 30):
    """detail loop — restart_every 카드 마다 driver quit + recreate (chrome 누적 메모리 release).
    5/11 사용자 evidence — long-run 중 chrome page "Out of memory" 노출. driver 자체 reset
    없이 driver.get N번 누적 시 chrome renderer 메모리 GB 단위 누적 → OOM. 30 카드 마다
    restart 가 가장 단순 대응. 반환: (record 수, 마지막 driver 인스턴스) — caller 가 새 driver
    참조 받아서 후속 호출 에 사용."""
    D.init_logging(product)
    sels = D.load_selectors(D.SITE_ACCOUNT, D.STAGE, product)
    batch_id = D.make_batch_id(product)
    if not sels:
        D.emit({'_error': 'no selectors loaded',
                'site': D.SITE_ACCOUNT, 'stage': D.STAGE,
                'product': product, 'batch_id': batch_id})
        return 0, driver
    n = 0
    for i, u in enumerate(urls):
        if i > 0 and i % restart_every == 0:
            print(f'[run] product={product} driver 재시작 at card {i}/{len(urls)} (메모리 release)',
                  file=sys.stderr)
            _hard_kill_driver(driver)
            driver = _make_driver_tracked(headless)
        # per-card try/except — driver 가 죽었을 때 (urllib3 ReadTimeout 등 uncaught) 한 카드
        # skip 하고 새 driver 로 다음 카드 계속. single-product run 도 self-heal.
        # 5/12 사용자 evidence — REF 단독 run 4 카드 직후 driver.get silent crash → 다음
        # product 없어서 batch 종료. per-card recovery 없으면 단독 run = 1 카드 fail = batch 죽음.
        try:
            rec = D.crawl_detail(driver, product, u, sels, batch_id)
        except Exception as e:
            err = f'{type(e).__name__}: {str(e)[:200]}'
            print(f'[run] product={product} crawl_detail uncaught at card {i+1}/{len(urls)} — driver 재생성: {err}',
                  file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            D.emit({'_error': f'crawl_detail_uncaught: {err}',
                    'product_url': u, 'product': product,
                    'stage': D.STAGE, 'batch_id': batch_id})
            _hard_kill_driver(driver)
            driver = _make_driver_tracked(headless)
            n += 1
            if sleep_s > 0:
                time.sleep(sleep_s)
            continue
        D.emit(rec)
        n += 1
        if sleep_s > 0:
            time.sleep(sleep_s)
    return n, driver


def _run_one_product(driver, product: str, args):
    """단일 product 의 main/bsr/detail 처리 — driver 공유, cache reset, jsonl/INSERT 별개.
    detail loop 가 주기 적 driver 재시작 시 새 driver 인스턴스 반환 — main 이 받아 다음 product 에 사용."""
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
                    key = _listing_key_from_url(u)
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
                _n, driver = run_detail(driver, product, use_urls, args.detail_sleep,
                                        args.headless, restart_every=30)
    finally:
        _close_results()
    return driver


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

    driver = _make_driver_tracked(args.headless)
    try:
        for product in args.product:
            print(f'\n=== [run] starting product={product} ===\n', file=sys.stderr)
            try:
                driver = _run_one_product(driver, product, args)
            except Exception as e:
                traceback.print_exc(file=sys.stderr)
                # 5/11 — driver 가 죽었을 가능성 (urllib3 ReadTimeoutError 류 uncaught). quit 후
                # 새 driver 재생성 — 같은 driver 로 다음 product 진행 시 첫 카드부터 cascade 사망.
                # evidence: 2026-05-11 TV --product tv 단독 run 시 75 카드에서 driver.get
                # urllib3 ReadTimeout (120s) → 다음 product 없어서 batch 종료. multi-product
                # run 시엔 이 driver 그대로 dead 상태로 다음 product 진입 → 즉시 fail.
                print(f'[run] product={product} failed — driver 재시작 + 다음 product 진행', file=sys.stderr)
                _hard_kill_driver(driver)
                driver = _make_driver_tracked(args.headless)
        return 0
    finally:
        _hard_kill_driver(driver)
        _close_results()
        _close_db()


if __name__ == '__main__':
    sys.exit(main())
