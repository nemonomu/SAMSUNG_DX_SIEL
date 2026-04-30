"""
Flipkart product detail crawler (SIEL).
- undetected_chromedriver
- xpath: DB 로드 (dx_siel_xpath_selectors), 하드코딩 X
- 4 제품군 (HHP/TV/REF/LDY) 공유
- count_of_reviews >= 1 일 때만 detailed_review_content 추출 (max 20)
- stdout JSONL + fpkt/logs/ 에 .log + 첫 URL .html

특수 selector data_field:
  base_container             : (옵션, 보통 detail 에 없음)
  expand_specifications      : Specifications 클릭 (실패 무시)
  click_show_all_reviews     : Show all reviews → review page (Buy now 회피)
  detailed_review_content    : review page 다중 element. 'review{n} - text ||| ...' 합침 (max 20)
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
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By

import config
import siel_log

# uc.Chrome.__del__ 가 GC 시점에 quit() 한 번 더 시도 → Windows OSError [WinError 6].
# finally 에서 driver.quit() 명시 호출하므로 __del__ 은 불필요.
uc.Chrome.__del__ = lambda self: None

SITE_ACCOUNT = 'Flipkart'
ACCOUNT_NAME = 'flipkart'
COMPANY = 'sea'
DIVISION = 'dx'
STAGE = 'detail'
IST = timezone(timedelta(hours=5, minutes=30))

REVIEW_MAX = 20

EXPAND_FIELDS = {'expand_specifications'}
NAVIGATE_FIELDS = {'click_show_all_reviews'}
CONTROL_FIELDS = EXPAND_FIELDS | NAVIGATE_FIELDS | {'base_container'}

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


def scroll_to_bottom(driver, pause: float = 1.0, max_scrolls: int = 20) -> None:
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


def fsn_from_url(url: str):
    m = re.search(r'[?&]pid=([A-Z0-9]+)', url)
    if m:
        return m.group(1)
    m = re.search(r'/itm([a-z0-9]+)', url, re.IGNORECASE)
    return m.group(1) if m else None


def robust_click(driver, xpath: str) -> bool:
    """Flipkart React click 좌표 이슈 회피."""
    try:
        el = driver.find_element(By.XPATH, xpath)
    except (NoSuchElementException, WebDriverException):
        return False
    try:
        driver.execute_script('arguments[0].scrollIntoView({block: "center"});', el)
        time.sleep(0.3)
    except WebDriverException:
        pass
    try:
        el.click()
        return True
    except WebDriverException:
        pass
    try:
        driver.execute_script('arguments[0].click();', el)
        return True
    except WebDriverException:
        pass
    try:
        ActionChains(driver).move_to_element(el).pause(0.3).click(el).perform()
        return True
    except WebDriverException:
        return False


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


def crawl_detail(driver, product: str, url: str, selectors: dict, batch_id: str) -> dict:
    rec: dict = {
        'account_name':   ACCOUNT_NAME,
        'product':        product,
        'stage':          STAGE,
        'company':        COMPANY,
        'division':       DIVISION,
        'source_url':     url,
        'fsn':            fsn_from_url(url),
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

    # Specifications 클릭 (robust)
    spec_sel = selectors.get('expand_specifications')
    if spec_sel and spec_sel.get('xpath'):
        ok = robust_click(driver, spec_sel['xpath'])
        if _logger:
            _logger.info('expand_specifications clicked=%s', ok)
        time.sleep(1.0)

    scroll_to_bottom(driver, pause=1.0, max_scrolls=10)

    # product page 의 spec / 일반 컬럼 추출 (review 는 보류)
    review_xpath = None
    for field, sel in selectors.items():
        if field in CONTROL_FIELDS:
            continue
        xpath = sel.get('xpath')
        if not xpath:
            rec[field] = None
            continue
        if field == 'detailed_review_content':
            review_xpath = xpath
            continue
        if field == 'retailer_sku_name_similar':
            parts = _extract_multi_raw(driver, xpath)
            rec[field] = siel_log.format_similar_names(parts)
        elif field == 'product_url':
            rec[field] = extract_attr(driver, xpath, 'href')
        elif field == 'star_rating':
            rec[field] = siel_log.parse_star_rating(extract_single(driver, xpath))
        elif field == 'count_of_star_ratings':
            rec[field] = siel_log.parse_count_of_ratings(extract_single(driver, xpath))
        else:
            rec[field] = extract_single(driver, xpath)

    # count_of_reviews 정책:
    #   숫자 >=1: 명시적으로 리뷰 있음 → 추출
    #   0:        명시적으로 리뷰 없음 → skip
    #   None:     count 표기 자체가 페이지에 없음 (modern Flipkart) → click_show_all_reviews
    #             가 매치되면 best-effort 시도, 매치 안 되면 skip
    count_reviews = siel_log.parse_int_field(rec.get('count_of_reviews'))
    rev_btn = selectors.get('click_show_all_reviews')
    rev_btn_xpath = rev_btn.get('xpath') if rev_btn else None

    should_try_reviews = False
    if review_xpath:
        if count_reviews is not None and count_reviews >= 1:
            should_try_reviews = True
        elif count_reviews is None and rev_btn_xpath:
            # count 표기 없음. show_all_reviews 버튼 존재 여부로 판단
            try:
                if driver.find_elements(By.XPATH, rev_btn_xpath):
                    should_try_reviews = True
            except WebDriverException:
                pass

    if should_try_reviews:
        if rev_btn_xpath:
            # click 대신 anchor href 직접 추출 + driver.get() — 새 탭 / JS interception 회피
            # aspect 필터 없는 generic 리뷰 link 우선 (&an=Camera 같은 aspect-specific 제외)
            rev_href = None
            try:
                anchors = driver.find_elements(By.XPATH, rev_btn_xpath)
            except WebDriverException:
                anchors = []
            # 1) aspect 없는 href 우선
            for a in anchors:
                try:
                    href = a.get_attribute('href') or ''
                except WebDriverException:
                    continue
                if '/product-reviews/' in href and '&an=' not in href:
                    rev_href = href
                    break
            # 2) fallback: 첫 매치
            if not rev_href:
                for a in anchors:
                    try:
                        href = a.get_attribute('href')
                    except WebDriverException:
                        continue
                    if href:
                        rev_href = href
                        break
            if rev_href:
                if _logger:
                    _logger.info('navigating to review page: %s', rev_href)
                try:
                    driver.get(rev_href)
                    time.sleep(3)
                    scroll_to_bottom(driver, pause=1.2, max_scrolls=15)
                except WebDriverException as e:
                    if _logger:
                        _logger.warning('review page navigation failed: %s', e)
                # review page 진입 후 두 번째 HTML snapshot — review xpath 디버깅용
                if _html_path:
                    review_html = _html_path.replace('.html', '_review.html')
                    if siel_log.save_html(driver, review_html) and _logger:
                        _logger.info('review page HTML saved: %s', review_html)
            elif _logger:
                _logger.info('review anchor href not found')
        parts = _extract_multi_raw(driver, review_xpath, max_n=REVIEW_MAX)
        rec['detailed_review_content'] = siel_log.format_review_content(parts)
    else:
        rec['detailed_review_content'] = None
        if review_xpath and _logger:
            _logger.info('skip review extraction: count_of_reviews=%s rev_btn=%s',
                         count_reviews, bool(rev_btn_xpath))
    return rec


def read_urls(args) -> list:
    if args.url:
        return [args.url]
    if args.urls_file:
        with open(args.urls_file, 'r', encoding='utf-8') as f:
            return [ln.strip() for ln in f if ln.strip()]
    return [ln.strip() for ln in sys.stdin if ln.strip()]


def main() -> int:
    ap = argparse.ArgumentParser(description='Flipkart product detail crawler')
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
