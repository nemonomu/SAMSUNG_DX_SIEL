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
from selenium.webdriver import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

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
BSR_POST_GET_WAIT = float(os.environ.get('AMZN_BSR_POST_GET_WAIT', '8'))

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


def make_driver(headless: bool = False, page_load_strategy: str | None = None) -> uc.Chrome:
    opts = uc.ChromeOptions()
    if page_load_strategy:
        opts.set_capability('pageLoadStrategy', page_load_strategy)
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
    driver = uc.Chrome(**kwargs)
    try:
        driver.set_page_load_timeout(60)
        driver.set_script_timeout(30)
    except WebDriverException:
        pass
    return driver


def load_page(driver, url: str, stage: str, product: str, page_no: int,
              batch_id: str, max_attempts: int = 2) -> bool:
    for attempt in range(1, max_attempts + 1):
        try:
            if _logger and stage == 'bsr':
                _logger.info('%s page=%d driver.get start attempt=%d/%d',
                             stage, page_no, attempt, max_attempts)
            driver.get(url)
            if _logger and stage == 'bsr':
                _logger.info('%s page=%d driver.get ok attempt=%d/%d',
                             stage, page_no, attempt, max_attempts)
            return True
        except WebDriverException as e:
            message = f'{type(e).__name__}: {str(e)[:240]}'
            if _logger:
                _logger.warning(
                    '%s page=%d load failed attempt %d/%d: %s',
                    stage, page_no, attempt, max_attempts, message,
                )
            try:
                driver.execute_script('window.stop();')
            except WebDriverException:
                pass
            time.sleep(2)
    emit({
        '_error': 'listing page load failed',
        'stage': f'{stage}_error',
        'error_stage': stage,
        'product': product,
        'page_no': page_no,
        'source_url': url,
        'batch_id': batch_id,
        'message': message,
        'crawl_datetime': now_server_ts(),
    })
    return False


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
    except Exception:
        return None
    for link in links:
        try:
            href = link.get_attribute('href') or ''
        except Exception:
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
    except Exception as e:
        if _logger:
            _logger.warning('safe_find_elements failed: %s: %s',
                            type(e).__name__, str(e)[:160])
        return []


def _js_bsr_records(driver) -> list[dict]:
    """Extract BSR cards inside Chrome to avoid Selenium WebElement round trips."""
    try:
        rows = driver.execute_script(
            """
            const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
            const text = (root, selectors) => {
              for (const sel of selectors) {
                const el = root.querySelector(sel);
                if (!el) continue;
                const val = clean(el.textContent || el.getAttribute('aria-label'));
                if (val) return val;
              }
              return null;
            };
            const attr = (root, selectors, name) => {
              for (const sel of selectors) {
                const el = root.querySelector(sel);
                if (!el) continue;
                const val = clean(el.getAttribute(name));
                if (val) return val;
              }
              return null;
            };
            const cards = Array.from(document.querySelectorAll(
              '#gridItemRoot, .zg-grid-general-faceout'
            ));
            const asinFromHref = (href) => {
              const match = (href || '').match(/\\/(?:dp|gp\\/product)\\/([A-Z0-9]{10})/);
              return match ? match[1] : null;
            };
            const seen = new Set();
            const rows = [];
            for (const card of cards) {
              const href = attr(card, [
                'a[href*="/dp/"]',
                'a[href*="/gp/product/"]'
              ], 'href');
              if (!href) continue;
              const asin = asinFromHref(href);
              const key = asin || href.split('?')[0];
              if (!key || seen.has(key)) continue;
              seen.add(key);
              const imgAlt = attr(card, ['img[alt]'], 'alt');
              rows.push({
                product_url: href,
                retailer_sku_name: text(card, [
                  '.p13n-sc-css-line-clamp',
                  '.p13n-sc-truncate',
                  'a[href*="/dp/"] span',
                  'a[href*="/gp/product/"] span',
                  'a[href*="/dp/"] div',
                  'a[href*="/gp/product/"] div'
                ]) || imgAlt,
                final_sku_price: text(card, [
                  '.p13n-sc-price',
                  '.a-price .a-offscreen',
                  '.a-color-price'
                ]),
                star_rating: attr(card, [
                  '[aria-label*="out of 5 stars"]',
                  'i.a-icon-star span'
                ], 'aria-label') || text(card, [
                  'i.a-icon-star span',
                  '[aria-label*="out of 5 stars"]'
                ]),
                count_of_star_ratings: text(card, [
                  'a[href*="customerReviews"] span',
                  'a[href*="product-reviews"] span',
                  '.a-size-small'
                ])
              });
            }
            return rows;
            """
        )
        return rows if isinstance(rows, list) else []
    except Exception as e:
        if _logger:
            _logger.warning('bsr js extract failed: %s: %s',
                            type(e).__name__, str(e)[:220])
        return []


def _page_height(driver) -> int:
    try:
        return int(driver.execute_script(
            'return Math.max(document.body.scrollHeight, document.documentElement.scrollHeight);'
        ) or 0)
    except Exception:
        return 0


def _viewport_height(driver) -> int:
    try:
        return int(driver.execute_script(
            'return window.innerHeight || document.documentElement.clientHeight || 900;'
        ) or 900)
    except Exception:
        return 900


def _scroll_to(driver, y: int) -> None:
    try:
        driver.execute_script('window.scrollTo(0, arguments[0]);', max(0, int(y)))
    except Exception:
        pass


def _scroll_element_into_view(driver, css_selector: str) -> None:
    try:
        driver.execute_script(
            """
            const el = document.querySelector(arguments[0]);
            if (el) {
                el.scrollIntoView({block: 'center', inline: 'nearest'});
            }
            """,
            css_selector,
        )
    except Exception:
        pass


def _dispatch_wheel(driver, delta_y: int) -> None:
    try:
        driver.execute_script(
            """
            const dy = arguments[0];
            const ev = new WheelEvent('wheel', {
              deltaY: dy,
              deltaMode: 0,
              bubbles: true,
              cancelable: true,
              view: window
            });
            (document.scrollingElement || document.documentElement).dispatchEvent(ev);
            window.dispatchEvent(ev);
            window.scrollBy(0, dy);
            """,
            delta_y,
        )
    except Exception:
        pass


def _selenium_wheel(driver, amount: int) -> None:
    try:
        ActionChains(driver).scroll_by_amount(0, amount).perform()
    except Exception:
        _dispatch_wheel(driver, amount)


def _key_scroll(driver, key: str, times: int = 1, pause: float = 0.25) -> None:
    try:
        body = driver.find_element(By.TAG_NAME, 'body')
        for _ in range(times):
            body.send_keys(key)
            time.sleep(pause)
    except Exception:
        pass


def _focus_bsr_grid(driver) -> None:
    try:
        driver.execute_script(
            """
            const first = document.querySelector('#gridItemRoot a, #gridItemRoot');
            if (first) {
              first.scrollIntoView({block: 'center'});
              if (first.focus) first.focus();
            }
            """
        )
    except Exception:
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
    try:
        html = driver.execute_script('return document.documentElement.outerHTML;')
        if html:
            with open(_html_path, 'w', encoding='utf-8') as f:
                f.write('<!doctype html>\n')
                f.write(html)
            if _logger is not None:
                _logger.info('HTML snapshot saved via js: %s', _html_path)
            _html_saved = True
            return
    except Exception as e:
        if _logger is not None:
            _logger.warning('HTML snapshot js save failed: %s: %s',
                            type(e).__name__, str(e)[:160])
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
    except Exception:
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
        if not load_page(driver, url, 'main', product, page, batch_id):
            break
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
                emit({
                    '_error': 'listing page has no cards',
                    'stage': 'main_error',
                    'error_stage': 'main',
                    'product': product,
                    'page_no': page,
                    'source_url': url,
                    'batch_id': batch_id,
                    'message': 'cards=0 after refresh retries',
                    'crawl_datetime': now_server_ts(),
                })
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
                    max_rounds: int = 12):
    best_cards = []

    def remember_cards():
        nonlocal best_cards
        cards = _safe_find_elements(driver, container_xpath)
        if len(cards) > len(best_cards):
            best_cards = cards
        return cards

    cards = remember_cards()
    if _logger:
        _logger.info('bsr render initial cards=%d', len(cards))
    if len(cards) >= expected_count:
        return cards

    def maybe_done(label: str):
        cards_now = remember_cards()
        if _logger:
            _logger.info('bsr render %s cards=%d best=%d',
                         label, len(cards_now), len(best_cards))
        return cards_now if len(cards_now) >= expected_count else None

    # Amazon BSR desktop cards lazy-render reliably when the page is moved
    # through fixed scroll positions with real waits between browser commands.
    pause = float(os.environ.get('AMZN_BSR_SCROLL_PAUSE', '2'))
    try:
        last_height = _page_height(driver)
        if _logger:
            _logger.info('bsr render retail scroll start target=%d height=%d pause=%.1fs',
                         expected_count, last_height, pause)

        for pct in (20, 40, 60, 80, 100):
            driver.execute_script(
                'window.scrollTo(0, document.body.scrollHeight * arguments[0]);',
                pct / 100,
            )
            time.sleep(pause)
            cards = remember_cards()
            if _logger:
                _logger.info('bsr render scroll pct=%d cards=%d best=%d',
                             pct, len(cards), len(best_cards))
            if len(cards) >= expected_count:
                return cards

        for attempt in range(1, 4):
            driver.execute_script('window.scrollTo(0, document.body.scrollHeight);')
            time.sleep(pause)
            cards = remember_cards()
            new_height = _page_height(driver)
            if _logger:
                _logger.info('bsr render bottom attempt=%d cards=%d best=%d height=%d',
                             attempt, len(cards), len(best_cards), new_height)
            if len(cards) >= expected_count:
                return cards
            if new_height == last_height:
                break
            last_height = new_height

        driver.execute_script('window.scrollTo(0, 0);')
        time.sleep(1)
        cards = remember_cards()
        if _logger:
            _logger.info('bsr render top reset cards=%d best=%d',
                         len(cards), len(best_cards))

        driver.execute_script('window.scrollTo(0, document.body.scrollHeight);')
        time.sleep(pause)
    except Exception as e:
        if _logger:
            _logger.warning('bsr render retail scroll failed: %s: %s',
                            type(e).__name__, str(e)[:220])

    cards = maybe_done('after percent scroll')
    if cards:
        return cards

    # Amazon BSR can keep the final cards unrendered unless it receives
    # real wheel/key-like events. Restore that fallback while keeping the
    # faster percent-scroll path above for environments where it works.
    try:
        if _logger:
            _logger.info('bsr render wheel fallback start target=%d', expected_count)
        for amount in (700, 900, 1100, 1300, 1500, 1800):
            _dispatch_wheel(driver, amount)
            time.sleep(0.45)
            cards = maybe_done(f'wheel dy={amount}')
            if cards:
                return cards
        _selenium_wheel(driver, 1800)
        time.sleep(0.7)
    except Exception as e:
        if _logger:
            _logger.warning('bsr render wheel fallback failed: %s: %s',
                            type(e).__name__, str(e)[:220])

    cards = maybe_done('after wheel fallback')
    if cards:
        return cards

    try:
        if _logger:
            _logger.info('bsr render key fallback start target=%d', expected_count)
        _key_scroll(driver, Keys.PAGE_DOWN, 8, pause=0.35)
        cards = maybe_done('after page_down')
        if cards:
            return cards
        _key_scroll(driver, Keys.END, 1, pause=0.5)
        time.sleep(1.0)
    except Exception as e:
        if _logger:
            _logger.warning('bsr render key fallback failed: %s: %s',
                            type(e).__name__, str(e)[:220])

    cards = remember_cards()
    if _logger:
        _logger.info('bsr render final cards=%d best=%d',
                     len(cards), len(best_cards))
    return best_cards or _safe_find_elements(driver, container_xpath)


def _normalize_bsr_record(raw: dict) -> dict:
    rec = dict(raw or {})
    if rec.get('final_sku_price'):
        rec['final_sku_price'] = siel_log.parse_amzn_apex_price(
            rec.get('final_sku_price'))
    rating_label = rec.get('star_rating') or ''
    rating_count = rec.get('count_of_star_ratings') or ''
    combined_rating = None
    for value in (rating_label, rating_count):
        if value and 'out of 5 stars' in value and 'rating' in value.lower():
            combined_rating = value
            break
    if combined_rating:
        star_part, _, count_part = combined_rating.partition(',')
        rec['star_rating'] = star_part.strip() or None
        rec['count_of_star_ratings'] = (
            siel_log.parse_count_of_ratings(count_part) if count_part else None
        )
    else:
        if rating_label:
            rec['star_rating'] = rating_label.split(',', 1)[0].strip() or None
        if rating_count:
            if 'out of 5 stars' in rating_count:
                rec['count_of_star_ratings'] = None
            else:
                rec['count_of_star_ratings'] = siel_log.parse_count_of_ratings(
                    rating_count)
    if rec.get('product_url'):
        asin = asin_from_text(rec['product_url'])
        if asin:
            rec['asin'] = asin
            rec['product_url'] = canonical_product_url(asin)
    return rec


def _load_bsr_records(driver, container_xpath: str, selectors: dict,
                      expected_count: int = 50):
    best_records: list[dict] = []

    def remember_records():
        nonlocal best_records
        records = [_normalize_bsr_record(r) for r in _js_bsr_records(driver)]
        if len(records) > len(best_records):
            best_records = records
        return records

    records = remember_records()
    if _logger:
        _logger.info('bsr js render initial records=%d', len(records))
    if len(records) >= expected_count:
        return records

    pause = float(os.environ.get('AMZN_BSR_SCROLL_PAUSE', '2'))
    plateau_count = 0
    last_count = len(records)
    try:
        last_height = _page_height(driver)
        if _logger:
            _logger.info('bsr js render scroll start target=%d height=%d pause=%.1fs',
                         expected_count, last_height, pause)
        for pct in (20, 40, 60, 80, 100):
            driver.execute_script(
                'window.scrollTo(0, document.body.scrollHeight * arguments[0]);',
                pct / 100,
            )
            time.sleep(pause)
            records = remember_records()
            if len(records) == last_count:
                plateau_count += 1
            else:
                plateau_count = 0
            last_count = len(records)
            if _logger:
                _logger.info('bsr js render scroll pct=%d records=%d best=%d plateau=%d',
                             pct, len(records), len(best_records), plateau_count)
            if len(records) >= expected_count:
                return records
            if len(records) >= 30 and plateau_count >= 3:
                break

        for amount in (700, 900, 1100, 1300, 1500, 1800, 2200, 2600):
            _dispatch_wheel(driver, amount)
            time.sleep(0.45)
            records = remember_records()
            if _logger:
                _logger.info('bsr js render wheel dy=%d records=%d best=%d',
                             amount, len(records), len(best_records))
            if len(records) >= expected_count:
                return records

        driver.execute_script('window.scrollTo(0, document.body.scrollHeight);')
        time.sleep(pause)
        new_height = _page_height(driver)
        records = remember_records()
        if _logger:
            _logger.info('bsr js render bottom records=%d best=%d height=%d',
                         len(records), len(best_records), new_height)
    except Exception as e:
        if _logger:
            _logger.warning('bsr js render failed: %s: %s',
                            type(e).__name__, str(e)[:220])

    if best_records:
        return best_records

    cards = _load_bsr_cards(driver, container_xpath, expected_count=expected_count)
    return [_normalize_bsr_record(extract_card(card, selectors)) for card in cards]


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
        if not load_page(driver, url, 'bsr', product, page_no, batch_id):
            continue
        if _logger:
            _logger.info('page=%d load_page ok; starting BSR lazy render', page_no)
            _logger.info('page=%d bsr post-get wait %.1fs before first DOM command',
                         page_no, BSR_POST_GET_WAIT)
        time.sleep(BSR_POST_GET_WAIT)
        records = _load_bsr_records(driver, container_xpath, selectors, expected_count=50)
        if page_no == 1:
            maybe_save_html(driver)
        if _logger:
            _logger.info('page=%d records=%d (loaded primary gridItemRoot)',
                         page_no, len(records))
        if not records:
            if _logger:
                _logger.info('page=%d records=0 -> refresh', page_no)
            try:
                driver.refresh()
                time.sleep(3)
                records = _load_bsr_records(driver, container_xpath, selectors, expected_count=50)
                if _logger:
                    _logger.info('page=%d records=%d (after refresh primary gridItemRoot)',
                                 page_no, len(records))
            except WebDriverException as e:
                if _logger:
                    _logger.warning('page=%d refresh failed: %s', page_no, e)
            if not records:
                emit({
                    '_error': 'listing page has no cards',
                    'stage': 'bsr_error',
                    'error_stage': 'bsr',
                    'product': product,
                    'page_no': page_no,
                    'source_url': url,
                    'batch_id': batch_id,
                    'message': 'cards=0 after refresh retry',
                    'crawl_datetime': now_server_ts(),
                })
        elif len(records) < 50:
            if _logger:
                _logger.info('page=%d records=%d<50 -> refresh/retry primary-grid pass',
                             page_no, len(records))
            try:
                driver.refresh()
                time.sleep(3)
                retry_records = _load_bsr_records(driver, container_xpath, selectors, expected_count=50)
                if len(retry_records) > len(records):
                    records = retry_records
            except WebDriverException as e:
                if _logger:
                    _logger.warning('page=%d refresh retry failed: %s', page_no, e)
            if _logger:
                _logger.info('page=%d records=%d (after refresh/retry primary-grid pass)',
                             page_no, len(records))
        for raw_pos, rec in enumerate(records, start=1):
            if max_rank and rank >= max_rank:
                break
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
