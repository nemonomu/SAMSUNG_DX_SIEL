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
import sys
import time
import traceback
from datetime import datetime, timezone, timedelta

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
        'https://www.amazon.in/gp/bestsellers/kitchen/1380365031/ref=zg_bs_nav_kitchen_2_1380263031',
        'https://www.amazon.in/gp/bestsellers/kitchen/1380365031/ref=zg_bs_pg_2_kitchen?ie=UTF8&pg=2',
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


def emit(rec: dict) -> None:
    sys.stdout.write(json.dumps(rec, ensure_ascii=False) + '\n')
    sys.stdout.flush()
    if _logger is not None:
        siel_log.warn_price_logic(_logger, rec)
        siel_log.log_record_summary(_logger, rec)


def make_batch_id(stage: str, product: str) -> str:
    ts = datetime.now(IST).strftime('%Y%m%d%H%M%S')
    return f"{ts}_{ACCOUNT_NAME}_{product}_{stage}"


def now_ist_iso() -> str:
    return datetime.now(IST).isoformat(timespec='seconds')


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
    try:
        rec['asin'] = card.get_attribute('data-asin')
    except WebDriverException:
        rec['asin'] = None
    for field, sel in selectors.items():
        if field == 'base_container':
            continue
        xpath = sel.get('xpath')
        if not xpath:
            continue
        if field == 'product_url':
            rec[field] = safe_attr(card, xpath, 'href')
        else:
            rec[field] = safe_text(card, xpath)
    return rec


def crawl_main(driver, product: str, selectors: dict, batch_id: str,
               max_rank: int, max_pages: int) -> int:
    container_xpath = (selectors.get('base_container') or {}).get('xpath')
    if not container_xpath:
        emit({'_error': 'base_container selector missing',
              'product': product, 'stage': 'main', 'batch_id': batch_id})
        return 0
    template = MAIN_URL_TEMPLATES[product]
    rank = 0
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
            _logger.info('page=%d cards=%d', page, len(cards))
        if not cards:
            break
        for card in cards:
            if rank >= max_rank:
                break
            rank += 1
            rec = extract_card(card, selectors)
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
                'crawl_datetime': now_ist_iso(),
            })
            emit(rec)
    return rank


def crawl_bsr(driver, product: str, selectors: dict, batch_id: str) -> int:
    container_xpath = (selectors.get('base_container') or {}).get('xpath')
    if not container_xpath:
        emit({'_error': 'base_container selector missing',
              'product': product, 'stage': 'bsr', 'batch_id': batch_id})
        return 0
    rank = 0
    for page_no, url in enumerate(BSR_URL_TEMPLATES[product], start=1):
        if _logger:
            _logger.info('page=%d url=%s', page_no, url)
        driver.get(url)
        time.sleep(3)
        scroll_to_bottom(driver, pause=1.5, max_scrolls=30)
        if page_no == 1:
            maybe_save_html(driver)
        cards = driver.find_elements(By.XPATH, container_xpath)
        if _logger:
            _logger.info('page=%d cards=%d', page_no, len(cards))
        for card in cards:
            rank += 1
            rec = extract_card(card, selectors)
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
                'crawl_datetime': now_ist_iso(),
            })
            emit(rec)
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
