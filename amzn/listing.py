"""
Amazon.In listing crawler (SIEL).
- undetected_chromedriver
- xpath: DB 로드 (dx_siel_xpath_selectors), 하드코딩 X
- 4 제품군 (HHP/TV/REF/LDY) 공유 — --product 인자
- stdout JSONL (account_name, batch_id, crawl_datetime 필수 컬럼 포함)
- amzn/logs/ 에 로그 + 첫 페이지 HTML snapshot 저장

사용:
  python amzn/listing.py --product hhp --stage main --max-rank 300
  python amzn/listing.py --product tv  --stage bsr
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import traceback
from datetime import datetime, timezone, timedelta
from urllib.parse import unquote

_ASIN_RE = re.compile(r'/(?:dp|gp/product)/([A-Z0-9]{10})')
_ASIN_TOKEN_RE = re.compile(r'(?:/|%2F)(?:dp|gp/product)(?:/|%2F)([A-Z0-9]{10})',
                            re.IGNORECASE)
_CANONICAL_HOST = 'https://www.amazon.in'

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import psycopg2
import psycopg2.extras
import undetected_chromedriver as uc
from selenium.common.exceptions import NoSuchElementException, WebDriverException
from selenium.webdriver.common.by import By

import config
import siel_log
from siel_batch import next_batch_id

# uc.Chrome.__del__ 가 GC 시점에 quit() 한 번 더 시도 → Windows OSError [WinError 6].
# finally 에서 driver.quit() 명시 호출하므로 __del__ 은 불필요.
uc.Chrome.__del__ = lambda self: None

SITE_ACCOUNT = 'Amazon'
ACCOUNT_NAME = 'amazon'
COMPANY = 'sea'
DIVISION = 'dx'
IST = timezone(timedelta(hours=5, minutes=30))

# URL 템플릿 (TARGETS.md / ERD v1.3 정리본)
MAIN_URL_TEMPLATES = {
    'hhp': 'https://www.amazon.in/s?k=smartphone&i=electronics&page={page}',
    'tv':  'https://www.amazon.in/s?k=tv&i=electronics&page={page}',
    'ref': 'https://www.amazon.in/s?k=refrigerator&page={page}',
    'ldy': 'https://www.amazon.in/s?k=washing+machine&page={page}',
}

BSR_URL_TEMPLATES = {
    'hhp': [
        'https://www.amazon.in/gp/bestsellers/electronics/1805560031/ref=zg_bs_nav_electronics_3_1389432031',
        'https://www.amazon.in/gp/bestsellers/electronics/1805560031/ref=zg_bs_pg_2_electronics?ie=UTF8&pg=2',
    ],
    'tv': [
        'https://www.amazon.in/gp/bestsellers/electronics/1389396031/ref=zg_bs_nav_electronics_2_1389375031',
        'https://www.amazon.in/gp/bestsellers/electronics/1389396031/ref=zg_bs_pg_2_electronics?ie=UTF8&pg=2',
    ],
    'ref': [
        'https://www.amazon.in/gp/bestsellers/kitchen/219651163031/ref=zg_bs_nav_kitchen_2_1380263031',
        'https://www.amazon.in/gp/bestsellers/kitchen/219651163031/ref=zg_bs_pg_2_kitchen?ie=UTF8&pg=2',
    ],
    'ldy': [
        'https://www.amazon.in/gp/bestsellers/kitchen/1380373031/ref=zg_bs_nav_kitchen_3_1380369031',
        'https://www.amazon.in/gp/bestsellers/kitchen/1380373031/ref=zg_bs_pg_2_kitchen?ie=UTF8&pg=2',
    ],
}

# logging globals (init_logging 으로 세팅)
_logger = None
_html_path = None
_html_saved = False

# emit() 가 갱신, 매 record progress + 누적 elapsed
_progress = {'total': 0, 'done': 0, 'start': 0.0,
             'fills': {'asin': 0, 'product_url': 0,
                       'final_sku_price': 0, 'retailer_sku_name': 0}}


def init_progress(total: int) -> None:
    global _progress
    _progress = {'total': total, 'done': 0, 'start': time.time(),
                 'fills': {'asin': 0, 'product_url': 0,
                           'final_sku_price': 0, 'retailer_sku_name': 0}}


def _format_elapsed(sec: float) -> str:
    s = int(sec)
    h, r = divmod(s, 3600)
    m, ss = divmod(r, 60)
    if h:
        return f"{h}h{m}m{ss}s"
    if m:
        return f"{m}m{ss}s"
    return f"{ss}s"


def _update_progress(rec: dict) -> None:
    if _logger is None:
        return
    p = _progress
    if p['total'] == 0:
        return  # init_progress 미호출 — listing.py 단독 실행 시 skip
    p['done'] += 1
    for f in p['fills']:
        v = rec.get(f)
        if v not in (None, '', []):
            p['fills'][f] += 1
    n, t = p['done'], p['total']
    elapsed = _format_elapsed(time.time() - p['start'])
    pct = n * 100 // t if t else 0
    f = p['fills']
    stage_tag = rec.get('stage') or 'listing'
    _logger.info('[%s] %d/%d (%d%%) %s | asin=%d url=%d price=%d name=%d',
                 stage_tag, n, t, pct, elapsed, f['asin'], f['product_url'],
                 f['final_sku_price'], f['retailer_sku_name'])


def db_connect():
    cfg = dict(config.DB_CONFIG)
    cfg.setdefault('database', 'postgres')
    return psycopg2.connect(**cfg)


def load_selectors(site_account: str, page_type: str, domain: str) -> dict:
    sql = """
        SELECT data_field, xpath_primary, fallback_xpath
          FROM dx_siel_xpath_selectors
         WHERE site_account = %s
           AND page_type    = %s
           AND domain       = %s
           AND is_active    = TRUE
    """
    conn = db_connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(sql, (site_account, page_type, domain))
            rows = cur.fetchall()
    finally:
        conn.close()
    return {r['data_field']: {'xpath': r['xpath_primary'],
                              'fallback': r['fallback_xpath']} for r in rows}


def make_driver(headless: bool = False) -> uc.Chrome:
    opts = uc.ChromeOptions()
    if headless:
        opts.add_argument('--headless=new')
    opts.add_argument('--no-sandbox')
    opts.add_argument('--disable-dev-shm-usage')
    opts.add_argument('--window-size=1920,1080')
    opts.add_argument('--lang=en-IN')
    kwargs = {'options': opts}
    major = siel_log.detect_chrome_major()
    if major:
        kwargs['version_main'] = major
    return uc.Chrome(**kwargs)


def scroll_to_bottom(driver, pause: float = 1.5, max_scrolls: int = 30) -> None:
    last_h = driver.execute_script('return document.body.scrollHeight')
    for _ in range(max_scrolls):
        driver.execute_script('window.scrollTo(0, document.body.scrollHeight);')
        time.sleep(pause)
        new_h = driver.execute_script('return document.body.scrollHeight')
        if new_h == last_h:
            break
        last_h = new_h


def safe_text(card, xpath: str):
    try:
        el = card.find_element(By.XPATH, xpath)
        return (el.text or el.get_attribute('textContent') or '').strip() or None
    except (NoSuchElementException, WebDriverException):
        return None


def safe_attr(card, xpath: str, attr: str):
    try:
        el = card.find_element(By.XPATH, xpath)
        return el.get_attribute(attr)
    except (NoSuchElementException, WebDriverException):
        return None


def first_text(card, primary: str, fallback: str = None):
    for xpath in (primary, fallback):
        if not xpath:
            continue
        val = safe_text(card, xpath)
        if val:
            return val
    return None


def first_attr(card, primary: str, fallback: str = None, attr: str = 'href'):
    for xpath in (primary, fallback):
        if not xpath:
            continue
        val = safe_attr(card, xpath, attr)
        if val:
            return val
    return None


def asin_from_text(value: str):
    if not value:
        return None
    for candidate in (value, unquote(value)):
        m = _ASIN_TOKEN_RE.search(candidate)
        if m:
            return m.group(1).upper()
    return None


def canonical_product_url(asin: str):
    return f'{_CANONICAL_HOST}/dp/{asin}' if asin else None


def scan_card_asin(card):
    try:
        links = card.find_elements(By.XPATH, './/a[@href]')
    except WebDriverException:
        return None
    for link in links:
        try:
            href = link.get_attribute('href') or ''
        except WebDriverException:
            continue
        asin = asin_from_text(href)
        if asin:
            return asin
    return None


def _safe_find_elements(driver, xpath: str):
    if not xpath:
        return []
    try:
        return driver.find_elements(By.XPATH, xpath)
    except WebDriverException:
        return []


def _page_height(driver) -> int:
    try:
        return int(driver.execute_script(
            'return Math.max(document.body.scrollHeight, document.documentElement.scrollHeight);'
        ) or 0)
    except WebDriverException:
        return 0


def _viewport_height(driver) -> int:
    try:
        return int(driver.execute_script(
            'return window.innerHeight || document.documentElement.clientHeight || 900;'
        ) or 900)
    except WebDriverException:
        return 900


def _scroll_to(driver, y: int) -> None:
    try:
        driver.execute_script('window.scrollTo(0, arguments[0]);', max(0, int(y)))
    except WebDriverException:
        pass

def emit(rec: dict) -> None:
    sys.stdout.write(json.dumps(rec, ensure_ascii=False) + '\n')
    sys.stdout.flush()
    if _logger is not None:
        siel_log.warn_price_logic(_logger, rec)
        siel_log.log_record_summary(_logger, rec)
    _update_progress(rec)


def make_batch_id(stage: str, product: str) -> str:
    return next_batch_id('a', _ROOT, datetime.now())


def now_server_ts() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def init_logging(product: str, stage: str):
    global _logger, _html_path, _html_saved
    _logger, _html_path = siel_log.setup(ACCOUNT_NAME, product, stage, _HERE)
    _html_saved = False


def maybe_save_html(driver) -> None:
    global _html_saved
    if _html_saved or _html_path is None:
        return
    if siel_log.save_html(driver, _html_path) and _logger is not None:
        _logger.info('HTML snapshot saved: %s', _html_path)
    _html_saved = True


def extract_card(card, selectors: dict) -> dict:
    rec: dict = {}
    # 1) data-asin attr (Main 카드만 있음. BSR 카드엔 없음)
    try:
        asin = card.get_attribute('data-asin')
        if asin:
            rec['asin'] = asin
    except WebDriverException:
        pass
    for field, sel in selectors.items():
        if field == 'base_container':
            continue
        xpath = sel.get('xpath')
        fallback = sel.get('fallback')
        if not xpath and not fallback:
            continue
        if field == 'product_url':
            rec[field] = first_attr(card, xpath, fallback, 'href')
        elif field in ('final_sku_price', 'original_sku_price'):
            rec[field] = siel_log.parse_amzn_apex_price(first_text(card, xpath, fallback))
        else:
            rec[field] = first_text(card, xpath, fallback)
    # 2) asin URL fallback — /dp/{ASIN}/ 패턴 (data-asin 없을 때, 예: BSR)
    if 'asin' not in rec and rec.get('product_url'):
        asin = asin_from_text(rec['product_url'])
        if asin:
            rec['asin'] = asin
    if 'asin' not in rec:
        asin = scan_card_asin(card)
        if asin:
            rec['asin'] = asin
    # 3) Sponsored 카드 fallback — selector 가 ad redirect URL 못 잡으면
    # data-asin 으로 canonical /dp/<ASIN> URL 생성 (retail_com 분석 일관성)
    if rec.get('asin'):
        rec['product_url'] = canonical_product_url(rec['asin'])
    return rec


def listing_record_key(rec: dict):
    asin = rec.get('asin') or asin_from_text(rec.get('product_url') or '')
    if asin:
        rec['asin'] = asin
        rec['product_url'] = canonical_product_url(asin)
        return asin
    url = rec.get('product_url') or ''
    return url.split('?', 1)[0].rstrip('/') or None


def crawl_main(driver, product: str, selectors: dict, batch_id: str,
               max_rank: int, max_pages: int) -> int:
    container_xpath = (selectors.get('base_container') or {}).get('xpath')
    if not container_xpath:
        emit({'_error': 'base_container selector missing',
              'product': product, 'stage': 'main', 'batch_id': batch_id})
        return 0
    template = MAIN_URL_TEMPLATES[product]
    rank = 0
    seen_keys = set()
    duplicate_count = 0
    for page in range(1, max_pages + 1):
        if rank >= max_rank:
            break
        url = template.format(page=page)
        if _logger:
            _logger.info('page=%d url=%s', page, url)
        driver.get(url)
        time.sleep(3)
        scroll_to_bottom(driver, pause=1.0, max_scrolls=8)
        if page == 1:
            maybe_save_html(driver)
        cards = driver.find_elements(By.XPATH, container_xpath)
        if _logger:
            _logger.info('page=%d cards=%d (initial)', page, len(cards))
        # cards=0 → refresh retry 3회 (anti-bot detection / timing 회복).
        # bsr 의 refresh 패턴 차용 + 3회 확장 (사용자 5/8 spec).
        if not cards:
            for attempt in range(1, 4):
                if _logger:
                    _logger.info('page=%d cards=0 → refresh attempt %d/3', page, attempt)
                try:
                    driver.refresh()
                    time.sleep(3)
                    scroll_to_bottom(driver, pause=1.0, max_scrolls=8)
                    cards = driver.find_elements(By.XPATH, container_xpath)
                    if _logger:
                        _logger.info('page=%d cards=%d (after refresh %d)', page, len(cards), attempt)
                    if cards:
                        break
                except WebDriverException as e:
                    if _logger:
                        _logger.warning('page=%d refresh %d failed: %s', page, attempt, e)
            if not cards:
                break
        for raw_pos, card in enumerate(cards, start=1):
            if rank >= max_rank:
                break
            rec = extract_card(card, selectors)
            key = listing_record_key(rec) or f'__no_key_main_{page}_{raw_pos}'
            if key in seen_keys:
                duplicate_count += 1
                continue
            seen_keys.add(key)
            rank += 1
            rec.update({
                'account_name':   ACCOUNT_NAME,
                'product':        product,
                'stage':          'main',
                'page_no':        page,
                'main_rank':      rank,
                'company':        COMPANY,
                'division':       DIVISION,
                'source_url':     url,
                'batch_id':       batch_id,
                'crawl_datetime': now_server_ts(),
            })
            emit(rec)
    if duplicate_count and _logger:
        _logger.info('[main] duplicate cards skipped=%d unique_emitted=%d', duplicate_count, rank)
    return rank


def _load_bsr_cards(driver, container_xpath: str, expected_count: int = 50,
                    max_rounds: int = 4):
    best_cards = []
    stable_rounds = 0
    for _ in range(max_rounds):
        height = max(_page_height(driver), 1)
        viewport = max(_viewport_height(driver), 600)
        step = max(int(viewport * 0.7), 420)
        positions = list(range(0, height + step, step))
        if positions[-1] != height:
            positions.append(height)

        for y in positions:
            _scroll_to(driver, y)
            time.sleep(0.35)
            cards = _safe_find_elements(driver, container_xpath)
            if len(cards) > len(best_cards):
                best_cards = cards
                stable_rounds = 0
            if len(cards) >= expected_count:
                return cards

        time.sleep(1.0)
        cards = _safe_find_elements(driver, container_xpath)
        if len(cards) > len(best_cards):
            best_cards = cards
            stable_rounds = 0
        elif len(cards) == len(best_cards):
            stable_rounds += 1
        if len(best_cards) >= expected_count or stable_rounds >= 2:
            break
    return best_cards or _safe_find_elements(driver, container_xpath)


def crawl_bsr(driver, product: str, selectors: dict, batch_id: str,
              max_rank: int = 0) -> int:
    """max_rank=0 (default) -> unlimited, >0 caps output."""
    container_xpath = (selectors.get('base_container') or {}).get('xpath')
    if not container_xpath:
        emit({'_error': 'base_container selector missing',
              'product': product, 'stage': 'bsr', 'batch_id': batch_id})
        return 0
    rank = 0
    seen_keys = set()
    duplicate_count = 0
    for page_no, url in enumerate(BSR_URL_TEMPLATES[product], start=1):
        if max_rank and rank >= max_rank:
            break
        if _logger:
            _logger.info('page=%d url=%s', page_no, url)
        driver.get(url)
        time.sleep(3)
        cards = _load_bsr_cards(driver, container_xpath, expected_count=50)
        if page_no == 1:
            maybe_save_html(driver)
        if _logger:
            _logger.info('page=%d cards=%d (loaded primary gridItemRoot)', page_no, len(cards))
        if not cards:
            if _logger:
                _logger.info('page=%d cards=0 -> refresh', page_no)
            try:
                driver.refresh()
                time.sleep(3)
                cards = _load_bsr_cards(driver, container_xpath, expected_count=50)
                if _logger:
                    _logger.info('page=%d cards=%d (after refresh primary gridItemRoot)',
                                 page_no, len(cards))
            except WebDriverException as e:
                if _logger:
                    _logger.warning('page=%d refresh failed: %s', page_no, e)
        elif len(cards) < 50:
            if _logger:
                _logger.info('page=%d cards=%d<50 -> second primary-grid pass', page_no, len(cards))
            cards = _load_bsr_cards(driver, container_xpath, expected_count=50)
            if _logger:
                _logger.info('page=%d cards=%d (after second primary-grid pass)',
                             page_no, len(cards))
        for raw_pos, card in enumerate(cards, start=1):
            if max_rank and rank >= max_rank:
                break
            rec = extract_card(card, selectors)
            key = listing_record_key(rec) or f'__no_key_bsr_{page_no}_{raw_pos}'
            if key in seen_keys:
                duplicate_count += 1
                continue
            seen_keys.add(key)
            rank += 1
            rec.update({
                'account_name':   ACCOUNT_NAME,
                'product':        product,
                'stage':          'bsr',
                'page_no':        page_no,
                'bsr_rank':       rank,
                'company':        COMPANY,
                'division':       DIVISION,
                'source_url':     url,
                'batch_id':       batch_id,
                'crawl_datetime': now_server_ts(),
            })
            emit(rec)
    if duplicate_count and _logger:
        _logger.info('[bsr] duplicate cards skipped=%d unique_emitted=%d', duplicate_count, rank)
    return rank

def main() -> int:
    ap = argparse.ArgumentParser(description='Amazon.In listing crawler')
    ap.add_argument('--product', required=True, choices=['hhp', 'tv', 'ref', 'ldy'])
    ap.add_argument('--stage', required=True, choices=['main', 'bsr'])
    ap.add_argument('--max-rank', type=int, default=300)
    ap.add_argument('--max-pages', type=int, default=30)
    ap.add_argument('--headless', action='store_true')
    args = ap.parse_args()

    init_logging(args.product, args.stage)
    batch_id = make_batch_id(args.stage, args.product)
    if _logger:
        _logger.info('batch_id=%s', batch_id)

    selectors = load_selectors(SITE_ACCOUNT, args.stage, args.product)
    if not selectors:
        if _logger:
            _logger.error('no selectors loaded for site=%s stage=%s product=%s',
                          SITE_ACCOUNT, args.stage, args.product)
        print(json.dumps({'_error': 'no selectors loaded',
                          'site': SITE_ACCOUNT, 'stage': args.stage,
                          'product': args.product, 'batch_id': batch_id}),
              file=sys.stderr)
        return 2
    if _logger:
        siel_log.log_selectors(_logger, selectors)

    driver = make_driver(headless=args.headless)
    try:
        if args.stage == 'main':
            n = crawl_main(driver, args.product, selectors, batch_id,
                           max_rank=args.max_rank, max_pages=args.max_pages)
        else:
            n = crawl_bsr(driver, args.product, selectors, batch_id)
        if _logger:
            _logger.info('=== done: records=%d batch_id=%s ===', n, batch_id)
        print(json.dumps({'_summary': 'ok', 'records': n,
                          'product': args.product, 'stage': args.stage,
                          'batch_id': batch_id}),
              file=sys.stderr)
        return 0
    except Exception as e:
        if _logger:
            _logger.exception('crawl failed: %s', e)
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({'_error': str(e), 'product': args.product,
                          'stage': args.stage, 'batch_id': batch_id}),
              file=sys.stderr)
        return 1
    finally:
        try:
            driver.quit()
        except Exception:
            pass


if __name__ == '__main__':
    sys.exit(main())
