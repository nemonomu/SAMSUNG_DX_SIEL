"""
Amazon.In product detail crawler (SIEL).
- undetected_chromedriver
- xpath: DB 로드 (dx_siel_xpath_selectors), 하드코딩 X
- 4 제품군 (HHP/TV/REF/LDY) 공유
- stdout JSONL + amzn/logs/ 에 .log + 첫 URL .html

특수 selector data_field:
  base_container             : (옵션, detail 에선 보통 없음)
  expand_additional_details  : 클릭 (실패 무시)
  expand_item_details        : 클릭 (실패 무시)
  detailed_review_content    : 다중 element. 'review{n} - text ||| ...' 형식 합침
  retailer_sku_name_similar  : 다중 element. ', ' 합침
  product_url                : href attr
  그 외                       : text()
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

# uc.Chrome.__del__ 가 GC 시점에 quit() 한 번 더 시도 → Windows OSError [WinError 6].
# finally 에서 driver.quit() 명시 호출하므로 __del__ 은 불필요.
uc.Chrome.__del__ = lambda self: None

SITE_ACCOUNT = 'Amazon'
ACCOUNT_NAME = 'amazon'
COMPANY = 'sea'
DIVISION = 'dx'
STAGE = 'detail'
IST = timezone(timedelta(hours=5, minutes=30))

EXPAND_FIELDS = {'expand_additional_details', 'expand_item_details'}

_logger = None
_html_path = None
_html_saved = False


def db_connect():
    cfg = dict(config.DB_CONFIG)
    cfg.setdefault('database', 'postgres')
    return psycopg2.connect(**cfg)


def load_selectors(site_account: str, stage: str, domain: str) -> dict:
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
            cur.execute(sql, (site_account, stage, domain))
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


def scroll_to_bottom(driver, pause: float = 1.0, max_scrolls: int = 15) -> None:
    last_h = driver.execute_script('return document.body.scrollHeight')
    for _ in range(max_scrolls):
        driver.execute_script('window.scrollTo(0, document.body.scrollHeight);')
        time.sleep(pause)
        new_h = driver.execute_script('return document.body.scrollHeight')
        if new_h == last_h:
            break
        last_h = new_h


def emit(rec: dict) -> None:
    sys.stdout.write(json.dumps(rec, ensure_ascii=False) + '\n')
    sys.stdout.flush()
    if _logger is not None:
        siel_log.warn_price_logic(_logger, rec)
        siel_log.log_record_summary(_logger, rec)


def make_batch_id(product: str) -> str:
    ts = datetime.now(IST).strftime('%Y%m%d%H%M%S')
    return f"{ts}_{ACCOUNT_NAME}_{product}_{STAGE}"


def now_ist_iso() -> str:
    return datetime.now(IST).isoformat(timespec='seconds')


def init_logging(product: str):
    global _logger, _html_path, _html_saved
    _logger, _html_path = siel_log.setup(ACCOUNT_NAME, product, STAGE, _HERE)
    _html_saved = False


def maybe_save_html(driver) -> None:
    global _html_saved
    if _html_saved or _html_path is None:
        return
    if siel_log.save_html(driver, _html_path) and _logger is not None:
        _logger.info('HTML snapshot saved: %s', _html_path)
    _html_saved = True


def asin_from_url(url: str):
    m = re.search(r'/(?:dp|gp/product)/([A-Z0-9]{10})', url)
    return m.group(1) if m else None


def try_click_expand(driver, xpath: str) -> None:
    try:
        btn = driver.find_element(By.XPATH, xpath)
        btn.click()
        time.sleep(0.5)
    except (NoSuchElementException, WebDriverException):
        pass


def extract_single(driver, xpath: str):
    try:
        el = driver.find_element(By.XPATH, xpath)
        return (el.text or el.get_attribute('textContent') or '').strip() or None
    except (NoSuchElementException, WebDriverException):
        return None


def _extract_multi_raw(driver, xpath: str, max_n=None) -> list:
    try:
        els = driver.find_elements(By.XPATH, xpath)
    except WebDriverException:
        return []
    if max_n is not None:
        els = els[:max_n]
    parts = []
    for e in els:
        try:
            t = (e.text or e.get_attribute('textContent') or '').strip()
            if t:
                parts.append(t)
        except WebDriverException:
            continue
    return parts


def extract_attr(driver, xpath: str, attr: str):
    try:
        el = driver.find_element(By.XPATH, xpath)
        return el.get_attribute(attr)
    except (NoSuchElementException, WebDriverException):
        return None


def extract_text_or_value(driver, xpath: str):
    """element 가 input/option 이면 value, 아니면 visible text. 빈문자열 → None."""
    try:
        el = driver.find_element(By.XPATH, xpath)
    except (NoSuchElementException, WebDriverException):
        return None
    try:
        if el.tag_name in ('input', 'option'):
            v = el.get_attribute('value')
        else:
            v = el.text or el.get_attribute('textContent')
    except WebDriverException:
        return None
    return (v or '').strip() or None


def crawl_detail(driver, product: str, url: str, selectors: dict, batch_id: str) -> dict:
    rec: dict = {
        'account_name':   ACCOUNT_NAME,
        'product':        product,
        'stage':          STAGE,
        'company':        COMPANY,
        'division':       DIVISION,
        'source_url':     url,
        'asin':           asin_from_url(url),
        'batch_id':       batch_id,
        'crawl_datetime': now_ist_iso(),
    }
    if _logger:
        _logger.info('detail url=%s', url)
    try:
        driver.get(url)
        time.sleep(3)
    except WebDriverException as e:
        rec['_error'] = f'goto_exception: {type(e).__name__}: {str(e)[:200]}'
        if _logger:
            _logger.warning('goto failed: %s', rec['_error'])
        return rec

    maybe_save_html(driver)

    for trigger_field in EXPAND_FIELDS:
        sel = selectors.get(trigger_field)
        if sel and sel.get('xpath'):
            try_click_expand(driver, sel['xpath'])

    scroll_to_bottom(driver, pause=1.0, max_scrolls=15)

    for field, sel in selectors.items():
        if field in EXPAND_FIELDS or field == 'base_container':
            continue
        xpath = sel.get('xpath')
        if not xpath:
            rec[field] = None
            continue
        if field == 'detailed_review_content':
            parts = _extract_multi_raw(driver, xpath)
            rec[field] = siel_log.format_review_content(parts)
        elif field == 'retailer_sku_name_similar':
            parts = _extract_multi_raw(driver, xpath)
            rec[field] = siel_log.format_similar_names(parts)
        elif field == 'product_url':
            rec[field] = extract_attr(driver, xpath, 'href')
        elif field == 'star_rating':
            rec[field] = siel_log.parse_star_rating(extract_single(driver, xpath))
        elif field == 'count_of_star_ratings':
            rec[field] = siel_log.parse_count_of_ratings(extract_single(driver, xpath))
        elif field == 'sku':
            rec[field] = extract_text_or_value(driver, xpath)
        else:
            rec[field] = extract_single(driver, xpath)
    return rec


def read_urls(args) -> list:
    if args.url:
        return [args.url]
    if args.urls_file:
        with open(args.urls_file, 'r', encoding='utf-8') as f:
            return [ln.strip() for ln in f if ln.strip()]
    return [ln.strip() for ln in sys.stdin if ln.strip()]


def main() -> int:
    ap = argparse.ArgumentParser(description='Amazon.In product detail crawler')
    ap.add_argument('--product', required=True, choices=['hhp', 'tv', 'ref', 'ldy'])
    ap.add_argument('--url', help='single URL')
    ap.add_argument('--urls-file', help='URL list file (한 줄 = 한 URL)')
    ap.add_argument('--sleep', type=float, default=2.0, help='URL 사이 sleep (s)')
    ap.add_argument('--headless', action='store_true')
    args = ap.parse_args()

    urls = read_urls(args)
    if not urls:
        print(json.dumps({'_error': 'no urls'}), file=sys.stderr)
        return 2

    init_logging(args.product)
    batch_id = make_batch_id(args.product)
    if _logger:
        _logger.info('batch_id=%s urls=%d', batch_id, len(urls))

    selectors = load_selectors(SITE_ACCOUNT, STAGE, args.product)
    if not selectors:
        if _logger:
            _logger.error('no selectors loaded')
        print(json.dumps({'_error': 'no selectors loaded',
                          'site': SITE_ACCOUNT, 'stage': STAGE,
                          'product': args.product, 'batch_id': batch_id}),
              file=sys.stderr)
        return 2
    if _logger:
        siel_log.log_selectors(_logger, selectors)

    driver = make_driver(headless=args.headless)
    try:
        n = 0
        for url in urls:
            rec = crawl_detail(driver, args.product, url, selectors, batch_id)
            emit(rec)
            n += 1
            if args.sleep > 0:
                time.sleep(args.sleep)
        if _logger:
            _logger.info('=== done: records=%d batch_id=%s ===', n, batch_id)
        print(json.dumps({'_summary': 'ok', 'records': n,
                          'product': args.product, 'stage': STAGE,
                          'batch_id': batch_id}),
              file=sys.stderr)
        return 0
    except Exception as e:
        if _logger:
            _logger.exception('crawl failed: %s', e)
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({'_error': str(e), 'product': args.product,
                          'stage': STAGE, 'batch_id': batch_id}),
              file=sys.stderr)
        return 1
    finally:
        try:
            driver.quit()
        except Exception:
            pass


if __name__ == '__main__':
    sys.exit(main())
