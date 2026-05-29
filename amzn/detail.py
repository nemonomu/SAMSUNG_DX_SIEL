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
from selenium.common.exceptions import NoSuchElementException, TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

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
STAGE = 'detail'
IST = timezone(timedelta(hours=5, minutes=30))

EXPAND_FIELDS = {'expand_additional_details', 'expand_item_details'}

_logger = None
_html_path = None
_html_saved = False
_html_saved_post = False

# emit() 가 갱신, PROGRESS_INTERVAL 마다 + 완료 시 1줄 출력
_progress = {'total': 0, 'done': 0, 'start': 0.0,
             'fills': {'inventory_status': 0, 'detailed_review_content': 0,
                       'sku': 0, 'star_rating': 0},
             'errors': 0}
PROGRESS_INTERVAL = 1  # 매 record 마다 progress + 누적 elapsed 출력


def init_progress(total: int) -> None:
    global _progress
    _progress = {'total': total, 'done': 0, 'start': time.time(),
                 'fills': {'inventory_status': 0, 'detailed_review_content': 0,
                           'sku': 0, 'star_rating': 0},
                 'errors': 0}


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
    p['done'] += 1
    if rec.get('_error'):
        p['errors'] += 1
    else:
        for f in p['fills']:
            v = rec.get(f)
            if v not in (None, '', []):
                p['fills'][f] += 1
    n, t = p['done'], p['total']
    is_final = (t > 0 and n == t)
    if n % PROGRESS_INTERVAL == 0 or is_final:
        elapsed = _format_elapsed(time.time() - p['start'])
        f = p['fills']
        if is_final:
            _logger.info('[done] %d records in %s | rev=%d sku=%d inv=%d star=%d errors=%d',
                         n, elapsed, f['detailed_review_content'], f['sku'],
                         f['inventory_status'], f['star_rating'], p['errors'])
        else:
            pct = n * 100 // t if t else 0
            _logger.info('[progress] %d/%d (%d%%) %s | rev=%d sku=%d inv=%d star=%d',
                         n, t, pct, elapsed, f['detailed_review_content'], f['sku'],
                         f['inventory_status'], f['star_rating'])


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
    _update_progress(rec)


def make_batch_id(product: str) -> str:
    return next_batch_id('a', _ROOT, datetime.now(IST))


def now_server_ts() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def init_logging(product: str):
    global _logger, _html_path, _html_saved, _html_saved_post
    _logger, _html_path = siel_log.setup(ACCOUNT_NAME, product, STAGE, _HERE)
    _html_saved = False
    _html_saved_post = False


def maybe_save_html(driver) -> None:
    global _html_saved
    if _html_saved or _html_path is None:
        return
    if siel_log.save_html(driver, _html_path) and _logger is not None:
        _logger.info('HTML snapshot saved: %s', _html_path)
    _html_saved = True


def maybe_save_html_post(driver) -> None:
    """post-scroll snapshot — review/lazy-load 영역 진단용. 첫 URL 만, _post.html 별도 파일."""
    global _html_saved_post
    if _html_saved_post or _html_path is None:
        return
    root, _ext = os.path.splitext(_html_path)
    post_path = root + '_post.html'
    if siel_log.save_html(driver, post_path) and _logger is not None:
        _logger.info('HTML snapshot (post-scroll) saved: %s', post_path)
    _html_saved_post = True


def asin_from_url(url: str):
    m = re.search(r'/(?:dp|gp/product)/([A-Z0-9]{10})', url or '', re.IGNORECASE)
    return m.group(1).upper() if m else None


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
        if _logger is not None:
            _logger.info('multi_raw xpath=%s WebDriverException', xpath[:80])
        return []
    if max_n is not None:
        els = els[:max_n]
    parts = []
    raw_samples = []
    for e in els:
        try:
            visible = (e.text or '').strip()
            tc = (e.get_attribute('textContent') or '').strip()
            t = visible or tc
            if len(raw_samples) < 2:
                raw_samples.append((len(visible), len(tc), tc[:100]))
            # media-only review (image/video 만, 본문 텍스트 0) — Amazon a11y collapsed-state wrapper text
            # LDY 5/10 batch 8/205 (3.9%) 검증. HHP/TV/REF 는 다른 selector 라 noise 0
            if t.startswith('Brief content visible, double tap to read full content'):
                parts.append('[media-only review]')
                continue
            if t:
                parts.append(t)
        except WebDriverException:
            continue
    nonempty = len(parts)
    parts = list(dict.fromkeys(parts))  # 순서 보존 dedup — review-collapsed/body 같은 hidden state 의 같은 text 중복 차단 (textContent fallback 으로 둘 다 잡혀 DB 중복 사례 방지)
    if _logger is not None:
        _logger.info('multi_raw xpath=%s els=%d nonempty=%d unique=%d samples=%r',
                     xpath[:120], len(els), nonempty, len(parts), raw_samples)
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
    asin = asin_from_url(url)
    rec: dict = {
        'account_name':   ACCOUNT_NAME,
        'product':        product,
        'stage':          STAGE,
        'company':        COMPANY,
        'division':       DIVISION,
        'source_url':     url,
        'asin':           asin,
        'item':           asin,
        'redirect':       False,
        'batch_id':       batch_id,
        'crawl_datetime': now_server_ts(),
    }
    # HHP sku = url 의 ASIN (stable). input[@id="ASIN"] 은 default variant 따라 dynamic 이라 unsafe.
    # TV/REF/LDY 는 SQL selector (Manufacturer Part Number) 사용 — 분기 X.
    if product == 'hhp':
        rec['sku'] = asin
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

    try:
        landing_url = driver.current_url or ''
    except WebDriverException:
        landing_url = ''
    landing_asin = asin_from_url(landing_url)
    if asin:
        if landing_asin == asin:
            rec['redirect'] = False
        else:
            rec.update({
                '_detail_skip': 'asin_mismatch',
                'redirect': True,
                'landing_url': landing_url,
                'landing_asin': landing_asin,
            })
            if _logger:
                _logger.warning(
                    'detail skip: listed ASIN != landing ASIN listed=%s landing=%s landing_url=%s',
                    asin, landing_asin, landing_url)
            maybe_save_html(driver)
            return rec

    maybe_save_html(driver)

    for trigger_field in EXPAND_FIELDS:
        sel = selectors.get(trigger_field)
        if sel and sel.get('xpath'):
            try_click_expand(driver, sel['xpath'])

    scroll_to_bottom(driver, pause=1.0, max_scrolls=15)
    if not _html_saved_post:
        time.sleep(1)  # 첫 URL post-scroll snapshot 진단용 settle (lazy-load fetch 안정화)
    maybe_save_html_post(driver)

    for field, sel in selectors.items():
        if field in EXPAND_FIELDS or field == 'base_container':
            continue
        if field == 'sku_assurance' and product == 'tv':
            continue
        xpath = sel.get('xpath')
        if not xpath:
            rec[field] = None
            continue
        if field == 'detailed_review_content':
            parts = _extract_multi_raw(driver, xpath)
            if not parts:
                # review section lazy-load — explicit scroll + sleep 3-tier retry
                # 같은 product 가 timing 따라 fill/null 변동 (els=8 / els=0). full-run 0/400 회귀 방지
                try:
                    driver.execute_script(
                        "var e = document.getElementById('reviewsMedley');"
                        " if (e) e.scrollIntoView({block: 'center', behavior: 'instant'});"
                    )
                except WebDriverException:
                    pass
                time.sleep(3)
                scroll_to_bottom(driver, pause=0.7, max_scrolls=5)
                parts = _extract_multi_raw(driver, xpath)
                if not parts:
                    time.sleep(3)
                    parts = _extract_multi_raw(driver, xpath)
            rec[field] = siel_log.format_review_content(parts)
        elif field == 'retailer_sku_name_similar':
            # sims carousel lazy load — 7번째 SKU 가 lazy. carousel viewport center scrollIntoView + 짧은 wait
            try:
                driver.execute_script(
                    "var c = document.querySelector('div[aria-labelledby=\"Customers who viewed this item also viewed\"]');"
                    " if (c) c.scrollIntoView({block: 'center', behavior: 'instant'});"
                )
            except WebDriverException:
                pass
            time.sleep(1.5)
            parts = _extract_multi_raw(driver, xpath)
            # race retry — LDY 인기 product (csr have row sim NULL 51.9% — TV 0% 대비 race 확정)
            # carousel placeholder DOM 미추가 함정: querySelector null 시 scrollIntoView JS no-op → 단순 sleep 만
            # 3 단계 보강: (1) scroll_to_bottom 재호출 lazy trigger, (2) scrollIntoView center,
            # (3) WebDriverWait until 3+ 카드 carousel anchor present (6s timeout) — race 회복 시 즉시, sparse 시 timeout
            if not parts:
                scroll_to_bottom(driver, pause=0.7, max_scrolls=5)
                try:
                    driver.execute_script(
                        "var c = document.querySelector('div[aria-labelledby=\"Customers who viewed this item also viewed\"]');"
                        " if (c) c.scrollIntoView({block: 'center', behavior: 'instant'});"
                    )
                except WebDriverException:
                    pass
                try:
                    WebDriverWait(driver, 6).until(
                        lambda d: len(d.find_elements(
                            By.XPATH,
                            "//div[@aria-labelledby='Customers who viewed this item also viewed']"
                            "//li[not(contains(@class,'a-carousel-card-empty'))]"
                            "//a[contains(@href,'/dp/')]"
                        )) >= 3
                    )
                except TimeoutException:
                    pass
                parts = _extract_multi_raw(driver, xpath)
            if product == 'ref':
                parts = siel_log.filter_similar_noise_ref(parts)
            elif product == 'ldy':
                parts = siel_log.filter_similar_noise_ldy(parts)
            else:
                # HHP / TV — 검증된 path 그대로 (메모 feedback_domain_branching_pattern.md)
                parts = siel_log.filter_similar_noise(parts)
            rec[field] = siel_log.format_similar_names(parts)
        elif field == 'product_url':
            rec[field] = extract_attr(driver, xpath, 'href')
        elif field == 'star_rating':
            rec[field] = siel_log.parse_star_rating(extract_single(driver, xpath))
        elif field == 'count_of_star_ratings':
            rec[field] = siel_log.parse_count_of_ratings(extract_single(driver, xpath))
        elif field == 'sku':
            # primary: Manufacturer Part Number. fallback union: Model Number /
            # Part Number / Item Model Number — 5/8 진단 발견 (REF Midea "Part Number",
            # TV SANSUI "Model Number", Warranty plan technical details "Item Model Number")
            v = extract_text_or_value(driver, xpath)
            if v is None and sel.get('fallback'):
                v = extract_text_or_value(driver, sel['fallback'])
            rec[field] = v
        elif field == 'hhp_memory_ram':
            v = extract_single(driver, xpath)
            if v is None and sel.get('fallback'):
                v = extract_single(driver, sel['fallback'])
            rec[field] = v
        elif field == 'delivery_availability':
            rec[field] = siel_log.parse_delivery_availability(extract_single(driver, xpath))
        elif field == 'fastest_delivery':
            rec[field] = siel_log.parse_fastest_delivery(extract_single(driver, xpath))
        elif field == 'sku_assurance':
            v = extract_single(driver, xpath)
            if v is None and sel.get('fallback'):
                v = extract_single(driver, sel['fallback'])
            rec[field] = siel_log.parse_sku_assurance(v)
        elif field == 'trade_in':
            rec[field] = siel_log.parse_trade_in(extract_single(driver, xpath))
        elif field == 'ldy_loading_type':
            rec[field] = siel_log.parse_ldy_loading_type(extract_single(driver, xpath))
        elif field == 'ldy_capacity':
            rec[field] = siel_log.parse_ldy_capacity(extract_single(driver, xpath))
        elif field == 'final_sku_price':
            # 신 layout (apex-pricetopay-accessibility-label) primary, 구 layout (a-offscreen) fallback.
            # 위치 동일 (corePriceDisplay/centerCol 안 가격 영역). 다른 field 의 fallback 은 활성 X.
            v = extract_single(driver, xpath)
            if v is None and sel.get('fallback'):
                v = extract_single(driver, sel['fallback'])
            rec[field] = siel_log.parse_amzn_apex_price(v)
        else:
            rec[field] = extract_single(driver, xpath)
    # post-process: "No customer reviews" 감지 — star_rating + count_of_star_ratings
    # 둘 다 추출 실패 + review 섹션 텍스트에 sentinel → 의도적 0건임을 명시.
    # 검색 범위는 #cr-top-reviews / #reviewsMedley 한정 (FAQ/추천 위젯 false positive 회피).
    # 실제 0건 sentinel "There are 0 customer reviews." 는 #cr-top-reviews 안.
    if not rec.get('star_rating') and not rec.get('count_of_star_ratings'):
        page_text = ''
        for review_id in ('cr-top-reviews', 'reviewsMedley'):
            try:
                section_text = driver.find_element(By.ID, review_id).text.lower()
            except (WebDriverException, NoSuchElementException):
                continue
            if section_text:
                page_text = section_text
                break
        if 'no customer reviews' in page_text or 'there are 0 customer reviews' in page_text:
            rec['star_rating'] = 'No customer reviews'
            rec['count_of_star_ratings'] = '0'
            if _logger:
                _logger.info('detail no_customer_reviews: url=%s', url)
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
