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
from selenium.common.exceptions import (NoSuchElementException, StaleElementReferenceException,
                                         TimeoutException, WebDriverException)
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from urllib3.exceptions import ReadTimeoutError as _Urllib3RT

import config
import siel_log
from siel_batch import next_batch_id

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

EXPAND_FIELDS = {'expand_specifications', 'expand_see_more'}
NAVIGATE_FIELDS = {'click_show_all_reviews'}
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
    return next_batch_id('f', _ROOT, datetime.now(IST))


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
        # target: count_of_reviews 만큼 (최대 REVIEW_MAX). count None 이면 best-effort = REVIEW_MAX.
        if count_reviews is not None and count_reviews >= 1:
            target = min(count_reviews, REVIEW_MAX)
        else:
            target = REVIEW_MAX
        rev_href = None
        if rev_btn_xpath:
            # click 대신 anchor href 직접 추출 + driver.get() — 새 탭 / JS interception 회피
            # source_url 의 fsn (pid) 과 review URL fsn 일치 확인 → 다른 product review 링크 차단
            # aspect 필터 없는 generic link 우선 (&an=Camera 같은 aspect-specific 후순위)
            src_fsn = fsn_from_url(url)
            try:
                anchors = driver.find_elements(By.XPATH, rev_btn_xpath)
            except WebDriverException:
                anchors = []
            # 1차: same product (fsn 매치) + aspect 없음
            for a in anchors:
                try:
                    href = a.get_attribute('href') or ''
                except WebDriverException:
                    continue
                if '/product-reviews/' not in href:
                    continue
                href_fsn = fsn_from_url(href)
                if src_fsn and href_fsn and src_fsn != href_fsn:
                    continue  # 다른 product 의 review link → skip
                if '&an=' not in href:
                    rev_href = href
                    break
            # 2차 fallback: same product (aspect 있어도 OK)
            if not rev_href:
                for a in anchors:
                    try:
                        href = a.get_attribute('href') or ''
                    except WebDriverException:
                        continue
                    if '/product-reviews/' not in href:
                        continue
                    href_fsn = fsn_from_url(href)
                    if src_fsn and href_fsn and src_fsn != href_fsn:
                        continue
                    rev_href = href
                    break
            # 3차 fallback: anchor 없거나 매치 0 → source URL 의 /p/ → /product-reviews/ 변환
            #               URL 자체에 fsn 포함되어 100% 같은 product 보장
            if not rev_href and '/p/' in url:
                rev_href = url.replace('/p/', '/product-reviews/', 1)
                if _logger:
                    _logger.info('anchor 매치 없음 — source URL 변환으로 review URL 생성: %s',
                                 rev_href)
            if rev_href:
                if _logger:
                    _logger.info('navigating to review page: %s (target=%d)', rev_href, target)
                # A+C: 1회 retry (driver hang stochastic 대응) + 그래도 fail 시 부분 review
                # 수용. urllib3 ReadTimeoutError 는 WebDriverException 자식 아니라 별도 catch.
                # 5/10 #8 사용자 진단 — production 의 expand_specifications + expand_see_more
                # click 흐름 후 review URL navigate 시 driver state 가 review page 의 lazy
                # 미발동 trigger (count >= 1 단 review body 매치 0 결함 — Haier M80 evidence).
                # 처치: review URL navigate 전 detail URL re-navigate (driver state refresh).
                # 본 도구 의 review URL detection 흐름 (review URL 직접 검증 시 매치 OK)
                # 와 같은 driver state.
                try:
                    _detail_url_refresh = rev_href.replace('/product-reviews/', '/p/', 1)
                    driver.get(_detail_url_refresh)
                    time.sleep(1.5)
                except (WebDriverException, _Urllib3RT) as e:
                    if _logger:
                        _logger.info('detail URL re-navigate fail (전 review): %s',
                                     type(e).__name__)
                try:
                    driver.get(rev_href)
                    time.sleep(3)
                    # review body count 기반 scroll loop — height-based 정지 조건 결함 회피
                    # (Flipkart React virtual scroll 시 첫 iteration height 안 변하 break 결함).
                    # target = min(count_of_reviews, REVIEW_MAX) review body element 등장
                    # 또는 추가 scroll 무효 (3회 stuck) 시 stop. 5/10 #4 사용자 evidence —
                    # 첫 navigate 직후 14-17건 (lazy 일부) → scroll 후 44-47건 (page 의 모든
                    # review body) 가 검증.
                    _scroll_target = min(target, REVIEW_MAX)
                    _last_n, _stuck = -1, 0
                    for _ in range(40):
                        try:
                            _now_n = len(driver.find_elements(By.XPATH, review_xpath))
                        except WebDriverException:
                            _now_n = 0
                        if _now_n >= _scroll_target:
                            break
                        if _now_n == _last_n:
                            _stuck += 1
                            if _stuck >= 3:
                                break
                        else:
                            _stuck = 0
                        _last_n = _now_n
                        try:
                            driver.execute_script(
                                'window.scrollTo(0, document.body.scrollHeight);')
                        except WebDriverException:
                            break
                        time.sleep(1.2)
                    if _logger:
                        _logger.info('review body count-based scroll: target=%d collected=%d',
                                     _scroll_target, _last_n if _last_n >= 0 else 0)
                except (WebDriverException, _Urllib3RT) as e:
                    if _logger:
                        _logger.warning('review page navigate fail: %s — retry',
                                        type(e).__name__)
                    try:
                        time.sleep(2)
                        driver.get(rev_href)
                        time.sleep(3)
                        scroll_to_bottom(driver, pause=1.2, max_scrolls=15)
                    except (WebDriverException, _Urllib3RT) as e2:
                        if _logger:
                            _logger.warning('review page navigate fail (after retry): %s',
                                            type(e2).__name__)
                # review page 진입 후 두 번째 HTML snapshot — review xpath 디버깅용
                if _html_path:
                    review_html = _html_path.replace('.html', '_review.html')
                    if siel_log.save_html(driver, review_html) and _logger:
                        _logger.info('review page HTML saved: %s', review_html)
            elif _logger:
                _logger.info('review anchor href not found')
        # 첫 페이지 + 부족 시 &page=N navigate. 페이지당 ~10. REVIEW_MAX=20 → 최대 page 2~3 까지.
        all_parts = []
        seen = set()
        for p in _extract_multi_raw(driver, review_xpath, max_n=None):
            if p not in seen:
                seen.add(p)
                all_parts.append(p)
                if len(all_parts) >= target:
                    break
        page = 2
        while len(all_parts) < target and rev_href and page <= 3:
            sep = '&' if '?' in rev_href else '?'
            page_url = f'{rev_href}{sep}page={page}'
            if _logger:
                _logger.info('review page %d: %s (collected=%d/%d)',
                             page, page_url, len(all_parts), target)
            # A+C: 1회 retry + 그래도 fail 시 collected so far 사용 + break
            try:
                driver.get(page_url)
                time.sleep(3)
                scroll_to_bottom(driver, pause=1.0, max_scrolls=10)
            except (WebDriverException, _Urllib3RT) as e:
                if _logger:
                    _logger.warning('review page %d navigate fail: %s — retry',
                                    page, type(e).__name__)
                try:
                    time.sleep(2)
                    driver.get(page_url)
                    time.sleep(3)
                    scroll_to_bottom(driver, pause=1.0, max_scrolls=10)
                except (WebDriverException, _Urllib3RT) as e2:
                    if _logger:
                        _logger.warning('review page %d navigate fail (after retry): %s',
                                        page, type(e2).__name__)
                    break
            new_count = 0
            for p in _extract_multi_raw(driver, review_xpath, max_n=None):
                if p not in seen:
                    seen.add(p)
                    all_parts.append(p)
                    new_count += 1
                    if len(all_parts) >= target:
                        break
            if new_count == 0:
                if _logger:
                    _logger.info('review page %d: no new parts — stop pagination', page)
                break
            page += 1
        rec['detailed_review_content'] = siel_log.format_review_content(all_parts)
        # logic violation 검사 — count_of_reviews>=1 인데 review body 0 → batch 첫 1건 만 saved
        global _review_violation_saved
        if not _review_violation_saved and count_reviews and count_reviews >= 1 and not all_parts:
            if _html_path:
                violation_html = _html_path.replace('.html', '_review_violation.html')
                if siel_log.save_html(driver, violation_html):
                    _review_violation_saved = True
                    if _logger:
                        _logger.warning('logic violation saved (count=%s, body=0): %s',
                                        count_reviews, violation_html)
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
