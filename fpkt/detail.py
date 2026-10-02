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
  open_reviews_panel         : Top rating link opens a client-side review panel
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
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import psycopg2
import psycopg2.extras
import undetected_chromedriver as uc
from selenium.common.exceptions import (NoSuchElementException, StaleElementReferenceException,
                                         TimeoutException, WebDriverException)
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from urllib3.exceptions import ReadTimeoutError as _Urllib3RT

import config
import siel_log
from siel_batch import next_batch_id
from fpkt.review_fields import FIELDS as REVIEW_FIELDS, aggregate_fields, direct_review_url, fill_missing, product_pid, same_review_url

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
REVIEW_SUMMARY_XPATH = (
    '(//div[contains(@class,"css-146c3p1") '
    'and contains(translate(normalize-space(.),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz"),"ratings") '
    'and contains(translate(normalize-space(.),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz"),"reviews")])[1]'
)

EXPAND_FIELDS = {'expand_specifications', 'expand_see_more'}
NAVIGATE_FIELDS = {'click_show_all_reviews', 'open_reviews_panel'}
CONTROL_FIELDS = EXPAND_FIELDS | NAVIGATE_FIELDS | {'base_container'}

_logger = None
_html_path = None
_html_saved = False
_review_violation_saved = False  # batch 별 첫 violation (count_of_reviews>=1 + body=NULL) 만 saved


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
    return next_batch_id('f', _ROOT, datetime.now())


def now_server_ts() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


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


def _pid_from_url(url: str):
    m = re.search(r'[?&]pid=([A-Z0-9]+)', url)
    return m.group(1) if m else None


def _item_id_from_url(url: str):
    m = re.search(r'/itm([a-z0-9]+)', url, re.IGNORECASE)
    return m.group(1).lower() if m else None


def robust_click(driver, xpath: str, wait_s: float = 10.0) -> bool:
    """Flipkart React click 좌표 이슈 회피 + element 등장까지 wait + stale retry.

    chain: js → native → actions. JS click 우선 — Selenium native el.click() 은
    좌표 click (W3C WebDriver) 이라 lazy load 시 image overlay 가 spec div 위로
    잠깐 떠오르는 timing 에 wrong target. JS arguments[0].click() 는 element
    direct (HTMLElement.click() native) — viewport overlay 무관.

    StaleElementReferenceException 시 element 재 lookup 후 1회 retry — React
    re-render 가 click chain 진행 중 발생하는 케이스 대응.
    """
    for attempt in range(2):  # stale 시 1회 retry
        try:
            el = WebDriverWait(driver, wait_s, poll_frequency=0.3).until(
                lambda d: d.find_element(By.XPATH, xpath))
        except (TimeoutException, WebDriverException) as e:
            if _logger:
                _logger.info('robust_click wait_timeout %s xpath=%.80s',
                             type(e).__name__, xpath)
            return False
        try:
            driver.execute_script('arguments[0].scrollIntoView({block: "center"});', el)
            time.sleep(0.3)
        except WebDriverException:
            pass
        chain = [
            ('js',      lambda: driver.execute_script('arguments[0].click();', el)),
            ('native',  lambda: el.click()),
            ('actions', lambda: ActionChains(driver).move_to_element(el).pause(0.3).click(el).perform()),
        ]
        stale_retry = False
        for method, fn in chain:
            try:
                fn()
                return True
            except StaleElementReferenceException:
                if _logger:
                    _logger.info('robust_click %s_stale attempt=%d: re-lookup',
                                 method, attempt + 1)
                stale_retry = True
                break
            except WebDriverException as e:
                if _logger:
                    _logger.info('robust_click %s_fail %s: %s',
                                 method, type(e).__name__, str(e)[:100])
                continue
        if not stale_retry:
            break  # stale 아닌 chain 전체 fail — retry 무의미
    return False


def extract_single(driver, xpath: str):
    try:
        el = driver.find_element(By.XPATH, xpath)
        return (el.text or el.get_attribute('textContent') or '').strip() or None
    except (NoSuchElementException, WebDriverException):
        return None


def _fill_review_summary_counts(driver, rec: dict):
    if not same_review_url(rec.get('source_url'), driver.current_url):
        return
    raw = extract_single(driver, REVIEW_SUMMARY_XPATH)
    if not raw:
        return
    ratings = siel_log.parse_count_of_ratings(raw)
    reviews = siel_log.parse_count_of_reviews(raw)
    changed = []
    if ratings and not rec.get('count_of_star_ratings'):
        rec['count_of_star_ratings'] = ratings
        changed.append(f'count_of_star_ratings={ratings}')
    if reviews and not rec.get('count_of_reviews'):
        rec['count_of_reviews'] = reviews
        changed.append(f'count_of_reviews={reviews}')
    if changed and _logger:
        _logger.info('review summary counts recovered: raw=%r %s',
                     raw, ' '.join(changed))


def _fill_product_rating_data(driver, rec):
    """JSON-LD is a fallback only; require the target product's exact SKU."""
    if product_pid(driver.current_url) != product_pid(rec['source_url']):
        return
    try:
        scripts = [el.get_attribute('textContent') for el in
                   driver.find_elements(By.CSS_SELECTOR, 'script[type="application/ld+json"]')]
        changed = fill_missing(rec, aggregate_fields(scripts, product_pid(rec['source_url'])))
        if changed and _logger:
            _logger.info('product rating JSON-LD recovered: %s', ', '.join(changed))
    except WebDriverException:
        pass


def _find_review_href(driver, url, selectors):
    sel = selectors.get('click_show_all_reviews') or {}
    # Preserve main's inline-link search before trying a synthesized URL or panel.
    for xpath in (sel.get('xpath'), sel.get('fallback'),
                  '//a[contains(@href,"/product-reviews/") and not(contains(@href,"buynow"))]'):
        if not xpath:
            continue
        try:
            for anchor in driver.find_elements(By.XPATH, xpath):
                href = anchor.get_attribute('href') or ''
                if same_review_url(url, href):
                    return href
        except WebDriverException:
            continue
    return None


def _prepare_review_href(driver, url, selectors, *, force_panel=False):
    # Legacy pages may already contain a usable link. Modern pages mount it in a panel.
    href = None if force_panel else _find_review_href(driver, url, selectors)
    if href:
        return href
    panel = selectors.get('open_reviews_panel') or {}
    xpath = panel.get('xpath')
    if not xpath:
        if _logger:
            _logger.warning('review_panel_selector_missing: apply review panel SQL migration')
        return None
    try:
        # Never click another product's rating badge.
        anchors = WebDriverWait(driver, 8, poll_frequency=0.3).until(
            lambda d: [a for a in d.find_elements(By.XPATH, xpath)
                       if product_pid(a.get_attribute('href') or '') == product_pid(url)])
        driver.execute_script('arguments[0].scrollIntoView({block: "center"});', anchors[0])
        driver.execute_script('arguments[0].click();', anchors[0])
        href = WebDriverWait(driver, 15, poll_frequency=0.3).until(
            lambda d: _find_review_href(d, url, selectors))
        if _logger:
            _logger.info('review_panel_ready: same-product unfiltered link found')
        return href
    except TimeoutException:
        if _logger:
            _logger.warning('review_panel_or_link_missing: pid=%s', product_pid(url))
    except WebDriverException as e:
        if _logger:
            _logger.warning('review_panel_open_failed: %s', type(e).__name__)
    return None


def crawl_review_fields(driver, product, url, selectors, batch_id, *, need_body=True):
    """Recovery entry point: no prices, specs, recommendations or listing collection."""
    rec = dict(account_name=ACCOUNT_NAME, product=product, stage=STAGE,
               source_url=url, fsn=fsn_from_url(url), batch_id=batch_id,
               crawl_datetime=now_server_ts())
    rec.update({field: None for field in REVIEW_FIELDS})
    driver.get(url)
    WebDriverWait(driver, 15).until(
        lambda d: d.execute_script('return document.readyState') == 'complete')
    # Wait for a top product badge or its structured data, not recommendation ratings.
    time.sleep(2)
    if product_pid(driver.current_url) != product_pid(url):
        rec['_error'] = 'product_redirect_mismatch'
        return rec
    for field, parser in [('star_rating', siel_log.parse_star_rating),
                          ('count_of_star_ratings', siel_log.parse_count_of_ratings),
                          ('count_of_reviews', siel_log.parse_count_of_reviews)]:
        xpath = (selectors.get(field) or {}).get('xpath')
        if xpath:
            rec[field] = parser(extract_single(driver, xpath))
    return _collect_reviews(driver, url, selectors, rec, need_body=need_body)


def _is_same_product_review_href(source_url: str, href: str) -> bool:
    if not href or '/product-reviews/' not in href or 'buynow' in href:
        return False
    src_pid = _pid_from_url(source_url)
    href_pid = _pid_from_url(href)
    if src_pid and href_pid and src_pid != href_pid:
        return False
    src_item = _item_id_from_url(source_url)
    href_item = _item_id_from_url(href)
    if src_item and href_item and src_item != href_item:
        return False
    return True


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
        'crawl_datetime': now_server_ts(),
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

    # spec section lazy mount trigger — Flipkart React 의 일부 component (Specifications)
    # 가 viewport scroll 없이 mount 안 되는 카드 발생. scroll_to_bottom 으로 lazy
    # render 강제. 대부분 카드는 이미 mount → height 변화 없어 first iteration 후 break.
    # 사례: 2026-05-06 vivo T5x 5G (MOBHH69NRE6PHFBH) — spec dom 30s 안에 미등장
    # (WebDriverWait + sku wait 둘 다 timeout). 사용자 console 에선 spec 정상.
    scroll_to_bottom(driver, pause=0.8, max_scrolls=5)

    # Specifications 클릭 (robust). wait_s 20s — stochastic page load 대비 (이전 10s 부족 사례).
    spec_sel = selectors.get('expand_specifications')
    if spec_sel and spec_sel.get('xpath'):
        ok = robust_click(driver, spec_sel['xpath'], wait_s=20.0)
        if _logger:
            _logger.info('expand_specifications clicked=%s', ok)
        if ok:
            # spec click 성공 시 React 비동기 expand animation 완료 + deep contents
            # (See more 버튼) lazy mount trigger. 본 wait 부재 시 see_more 10s timeout
            # 5건 발생 (2026-05-06 30 sample). 사용자 page 진단: spec click → 펼쳐지고
            # See more 정상 등장. driver-only timing 결함이라 명시.
            time.sleep(1.0)
            scroll_to_bottom(driver, pause=0.5, max_scrolls=3)
        else:
            time.sleep(0.8)

    # See more 클릭 — deep spec lazy load. wait_s 20s — 위 사례 대응.
    seemore_sel = selectors.get('expand_see_more')
    sku_sel = selectors.get('sku')
    sku_value_xpath = (sku_sel or {}).get('xpath') or '//div[normalize-space(text())="Model Name"]/following-sibling::div[1]'
    if seemore_sel and seemore_sel.get('xpath'):
        ok = robust_click(driver, seemore_sel['xpath'], wait_s=20.0)
        if _logger:
            _logger.info('expand_see_more clicked=%s', ok)
        # WebDriverWait + custom condition — sku value 등장 시 즉시 break, timeout 시 exception.
        # 빠른 server: ~1초도 안 걸림. 느린 server: max 30초까지 대기. polling 0.3초 자동.
        def _sku_ready(d):
            try:
                els = d.find_elements(By.XPATH, sku_value_xpath)
                if not els:
                    return False
                txt = (els[0].text or els[0].get_attribute('textContent') or '').strip()
                return bool(txt)
            except WebDriverException:
                return False
        try:
            WebDriverWait(driver, 30, poll_frequency=0.3).until(_sku_ready)
            if _logger:
                _logger.info('sku value ready (WebDriverWait)')
        except TimeoutException:
            if _logger:
                _logger.warning('sku value 30초 내 미등장 — spec 자체 없는 product 가능성, 진행')

    scroll_to_bottom(driver, pause=1.0, max_scrolls=10)

    # spec 영역 디버깅용 — expand_specifications 클릭 후 HTML snapshot (마지막 URL 가 덮어씀)
    if _html_path:
        spec_html = _html_path.replace('.html', '_spec.html')
        if siel_log.save_html(driver, spec_html) and _logger:
            _logger.info('spec section HTML saved: %s', spec_html)

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
        elif field == 'count_of_reviews':
            rec[field] = siel_log.parse_count_of_reviews(extract_single(driver, xpath))
        elif field == 'savings':
            rec[field] = siel_log.parse_savings(extract_single(driver, xpath))
        elif field == 'discount_type':
            # cls "HZ0E6r Rm9_cy" deal badge innermost div 매치 (main 과 동일 cls — 사용자 5/9 console 검증).
            # Bank Offer 제외. Exchange offer 영역 ("Upto" / "₹X" / "on Exchange") 제외. 길이 < 50.
            matched, seen = [], set()
            try:
                els = driver.find_elements(By.XPATH, xpath)
            except WebDriverException:
                els = []
            for e in els:
                try:
                    txt = (e.text or '').strip()
                except WebDriverException:
                    continue
                if not txt or len(txt) > 80:
                    continue
                if txt == 'Upto' or txt.startswith('₹') or 'on Exchange' in txt:
                    continue
                if 'Bank Offer' in txt or 'Bank offer' in txt:
                    continue
                if 'Only' in txt and 'left' in txt:
                    # 재고 표지 — discount_type 아님
                    continue
                if txt not in seen:
                    seen.add(txt)
                    matched.append(txt)
            rec[field] = ', '.join(matched) if matched else None
        elif field == 'final_sku_price':
            rec[field] = siel_log.parse_price_value(extract_single(driver, xpath))
        elif field == 'original_sku_price':
            # detail page strikethrough div text = ₹ 없는 숫자만 ("10,999") — ₹ prefix 추가.
            # main page original 은 ₹ 포함 ("₹39,900") — startswith 검사로 호환.
            _t = extract_single(driver, xpath)
            if _t and not _t.startswith('₹'):
                _t = '₹' + _t
            rec[field] = siel_log.parse_price_value(_t)
        elif field == 'hhp_storage':
            rec[field] = siel_log.parse_hhp_storage(extract_single(driver, xpath))
        elif field == 'delivery_availability':
            rec[field] = siel_log.parse_delivery(extract_single(driver, xpath))
        elif field == 'ldy_loading_type':
            rec[field] = siel_log.parse_ldy_loading_type(extract_single(driver, xpath))
        elif field == 'ldy_capacity':
            rec[field] = siel_log.parse_ldy_capacity(extract_single(driver, xpath))
        else:
            rec[field] = extract_single(driver, xpath)

    return _collect_reviews(driver, url, selectors, rec)


def _wait_review_ready(driver, url, rec, review_xpath, *, need_body=True):
    """Wait for required data, rather than sleeping after every navigation."""
    if not same_review_url(url, driver.current_url):
        return False
    if any(rec.get(field) is None for field in ('count_of_reviews', 'count_of_star_ratings')):
        try:
            WebDriverWait(driver, 12, poll_frequency=0.3).until(
                lambda d: extract_single(d, REVIEW_SUMMARY_XPATH))
        except TimeoutException:
            if _logger:
                _logger.warning('review_summary_missing')
    _fill_review_summary_counts(driver, rec)
    if not need_body or siel_log.parse_int_field(rec.get('count_of_reviews')) == 0:
        return True
    try:
        WebDriverWait(driver, 12, poll_frequency=0.3).until(
            lambda d: _extract_multi_raw(d, review_xpath, max_n=None))
        return True
    except TimeoutException:
        return False


def _scroll_review_bodies(driver, review_xpath, target):
    """Wait for a page's nonempty bodies, including delayed React mounting."""
    def ready(d):
        count = len(_extract_multi_raw(d, review_xpath, max_n=None))
        if count >= target:
            return count
        d.execute_script('window.scrollTo(0, document.body.scrollHeight);')
        return False

    try:
        return WebDriverWait(driver, 15, poll_frequency=0.5).until(ready)
    except TimeoutException:
        # A page can legitimately have nine usable bodies. Keep these and paginate.
        return len(_extract_multi_raw(driver, review_xpath, max_n=None))


def _review_page_url(href, page):
    parts = urlsplit(href)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
             if key != 'page']
    if page > 1:
        query.append(('page', str(page)))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ''))


def _review_target(rec):
    count = siel_log.parse_int_field(rec.get('count_of_reviews'))
    return min(count, REVIEW_MAX) if count is not None else REVIEW_MAX


def _merge_review_parts(all_parts, parts, target):
    for part in parts:
        if len(all_parts) >= target:
            break
        text = re.sub(r'\s+', ' ', part).strip()
        if text and text not in all_parts:
            all_parts.append(text)


def _read_review_pages(driver, url, href, review_xpath, rec, all_parts, *, need_body=True):
    """One bounded pass; keep partial results for the next fallback."""
    for page in range(1, 4):
        count = siel_log.parse_int_field(rec.get('count_of_reviews'))
        if count is not None and (page - 1) * 10 >= count:
            break
        try:
            driver.get(_review_page_url(href, page))
            if not _wait_review_ready(driver, url, rec, review_xpath, need_body=False):
                return
            if not need_body or _review_target(rec) == 0:
                return
            count = siel_log.parse_int_field(rec.get('count_of_reviews'))
            expected = min(10, max(0, count - (page - 1) * 10)) if count is not None else 10
            if expected == 0:
                return
            _scroll_review_bodies(driver, review_xpath, expected)
            # Recheck after asynchronous loading before accepting any body.
            if not same_review_url(url, driver.current_url):
                return
            _merge_review_parts(all_parts, _extract_multi_raw(driver, review_xpath, max_n=None),
                                _review_target(rec))
            if _logger:
                _logger.info('review page %d: collected=%d/%d', page,
                             len(all_parts), _review_target(rec))
            if page == 1 and _html_path:
                siel_log.save_html(driver, _html_path.replace('.html', '_review.html'))
            if len(all_parts) >= _review_target(rec):
                return
        except (WebDriverException, _Urllib3RT) as exc:
            if _logger:
                _logger.warning('review page %d failed: %s', page, type(exc).__name__)
            return


def _collect_reviews(driver, url, selectors, rec, *, need_body=True):
    _fill_product_rating_data(driver, rec)
    rec.setdefault('count_of_reviews', None)
    review_xpath = (selectors.get('detailed_review_content') or {}).get('xpath')
    if _review_target(rec) == 0:
        rec.setdefault('detailed_review_content', None)
        rec['_review_status'] = 'zero_reviews'
        return rec
    if not need_body and siel_log.parse_int_field(rec.get('count_of_reviews')) is not None:
        return rec
    if need_body and not review_xpath:
        rec.setdefault('detailed_review_content', None)
        rec['_review_status'] = 'review_selector_missing'
        return rec

    existing = rec.get('detailed_review_content') or ''
    all_parts = []
    _merge_review_parts(all_parts, re.split(r'(?:^| \|\|\| )review\d+ - ', existing),
                        _review_target(rec))

    def complete():
        if not need_body:
            return siel_log.parse_int_field(rec.get('count_of_reviews')) is not None
        return len(all_parts) >= _review_target(rec)

    # Main's existing inline review link is the first choice.
    inline = _find_review_href(driver, url, selectors)
    # Main also reads embedded bodies when there is a positive count but no link.
    if (need_body and not inline and not complete()
            and siel_log.parse_int_field(rec.get('count_of_reviews')) is not None
            and product_pid(driver.current_url) == product_pid(url)):
        _merge_review_parts(all_parts, _extract_multi_raw(driver, review_xpath, max_n=None),
                            _review_target(rec))
    direct = direct_review_url(url)
    tried = set()
    for method, href in (('inline', inline), ('direct', direct)):
        if complete():
            break
        if not href:
            continue
        first_page = _review_page_url(href, 1)
        if first_page in tried:
            continue
        tried.add(first_page)
        if _logger:
            _logger.info('review route=%s pid=%s', method, product_pid(url))
        _read_review_pages(driver, url, first_page, review_xpath, rec, all_parts,
                           need_body=need_body)

    # The panel is only needed after inline/direct collection is missing or short.
    if not complete():
        try:
            if driver.current_url != url:
                driver.get(url)
                WebDriverWait(driver, 15, poll_frequency=0.3).until(
                    lambda d: d.execute_script('return document.readyState') == 'complete')
            if product_pid(driver.current_url) == product_pid(url):
                _fill_product_rating_data(driver, rec)
                href = _prepare_review_href(driver, url, selectors, force_panel=True)
                if href and not complete():
                    if _logger:
                        _logger.info('review route=panel pid=%s', product_pid(url))
                    _read_review_pages(driver, url, href, review_xpath, rec, all_parts,
                                       need_body=need_body)
        except (WebDriverException, _Urllib3RT) as exc:
            if _logger:
                _logger.warning('review panel fallback failed: %s', type(exc).__name__)

    if not need_body:
        return rec
    target = _review_target(rec)
    # Never discard an already collected body when a later fallback fails.
    rec['detailed_review_content'] = (siel_log.format_review_content(all_parts[:target])
                                      if target else existing or None)
    rec['_review_status'] = ('zero_reviews' if target == 0 else
                             'collected' if len(all_parts) >= target else
                             'review_body_partial' if all_parts else 'review_body_missing')
    if _logger and rec['_review_status'] in ('review_body_partial', 'review_body_missing'):
        _logger.warning('%s: pid=%s collected=%d target=%d',
                        rec['_review_status'], product_pid(url), len(all_parts), target)
    global _review_violation_saved
    count_reviews = siel_log.parse_int_field(rec.get('count_of_reviews'))
    if not _review_violation_saved and count_reviews and count_reviews >= 1 and not all_parts:
        if _html_path and siel_log.save_html(driver, _html_path.replace('.html', '_review_violation.html')):
            _review_violation_saved = True
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
