"""
Amazon xpath validator — main / bsr / detail page 의 selector 검증 도구.

driver 가 visible 창 띄움. 사용자가 page 직접 보면서 cmd 결과 비교.

사용:
  py -3 amzn\validate.py --product hhp --stage main
  py -3 amzn\validate.py --product tv  --stage bsr
  py -3 amzn\validate.py --product hhp --stage detail --url https://www.amazon.in/dp/B0XXXXXXXX

옵션:
  --card-rank N   main/bsr 의 카드 #N 검사 (default 1 = 첫 카드)
  --headless      browser 안 띄움

명령 (interactive prompt):
  <Enter> 또는 q  → 종료 + driver close
  i               → custom xpath 입력 → 매치 결과 출력
  r               → driver refresh
  s               → screenshot 저장 (amzn/logs/)
"""
from __future__ import annotations
import argparse
import os
import sys
import time
from datetime import datetime, timezone, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import psycopg2
import psycopg2.extras
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.common.exceptions import WebDriverException

import config
import siel_log

uc.Chrome.__del__ = lambda self: None

SITE_ACCOUNT = 'Amazon'
IST = timezone(timedelta(hours=5, minutes=30))

MAIN_URL_TEMPLATES = {
    'hhp': 'https://www.amazon.in/s?k=smartphone&i=electronics&page=1',
    'tv':  'https://www.amazon.in/s?k=tv&i=electronics&page=1',
    'ref': 'https://www.amazon.in/s?k=refrigerator&page=1',
    'ldy': 'https://www.amazon.in/s?k=washing+machine&page=1',
}

BSR_URL_TEMPLATES = {
    'hhp': 'https://www.amazon.in/gp/bestsellers/electronics/1805560031/',
    'tv':  'https://www.amazon.in/gp/bestsellers/electronics/1389396031/',
    'ref': 'https://www.amazon.in/gp/bestsellers/kitchen/1380365031/',
    'ldy': 'https://www.amazon.in/gp/bestsellers/kitchen/1380373031/',
}


def db_connect():
    cfg = dict(config.DB_CONFIG)
    cfg.setdefault('database', 'postgres')
    return psycopg2.connect(**cfg)


def load_selectors(product: str, stage: str) -> list:
    sql = """
        SELECT data_field, xpath_primary, fallback_xpath, notes
          FROM dx_siel_xpath_selectors
         WHERE site_account = %s
           AND page_type    = %s
           AND domain       = %s
           AND is_active    = TRUE
         ORDER BY data_field
    """
    conn = db_connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(sql, (SITE_ACCOUNT, stage, product))
            return cur.fetchall()
    finally:
        conn.close()


def make_driver(headless=False):
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


def scroll_bottom(driver, pause=1.0, max_scrolls=8):
    last_h = driver.execute_script('return document.body.scrollHeight')
    for _ in range(max_scrolls):
        driver.execute_script('window.scrollTo(0, document.body.scrollHeight);')
        time.sleep(pause)
        new_h = driver.execute_script('return document.body.scrollHeight')
        if new_h == last_h:
            break
        last_h = new_h


def match_xpath(scope, xpath: str, attr: str = None):
    """scope 에서 xpath 매치 → (count, sample). attr 지정 시 attr 값, 아니면 text."""
    try:
        els = scope.find_elements(By.XPATH, xpath)
    except WebDriverException as e:
        return 0, f'<error: {type(e).__name__}: {str(e)[:80]}>'
    n = len(els)
    if n == 0:
        return 0, None
    el = els[0]
    if attr:
        try:
            val = el.get_attribute(attr)
        except Exception:
            val = None
        return n, val
    try:
        text = (el.text or el.get_attribute('textContent') or '').strip()
    except Exception:
        text = None
    return n, text


def print_result(field: str, xpath: str, found: int, sample, label: str = ''):
    label_str = f' [{label}]' if label else ''
    if found == 0:
        xp_short = xpath[:90] + ('...' if len(xpath) > 90 else '')
        print(f'  ❌ {field}{label_str}: 0 매치')
        print(f'      xpath: {xp_short}')
    else:
        sample_short = (str(sample) if sample is not None else '<empty>')[:140].replace('\n', ' ')
        print(f'  ✅ {field}{label_str}: {found} 매치 → "{sample_short}"')


def validate(scope, selectors: list, scope_label: str):
    print(f'\n=== {scope_label} 매치 결과 ===')
    for sel in selectors:
        field = sel['data_field']
        if field == 'base_container':
            continue
        primary = sel['xpath_primary']
        fallback = sel['fallback_xpath']
        attr = 'href' if field == 'product_url' else None
        f_p, s_p = match_xpath(scope, primary, attr=attr)
        print_result(field, primary, f_p, s_p, label='primary')
        if fallback:
            f_f, s_f = match_xpath(scope, fallback, attr=attr)
            print_result(field, fallback, f_f, s_f, label='fallback')


def interactive_loop(driver, scope, scope_label: str):
    print(f'\n--- {scope_label} interactive (q=quit, i=xpath input, r=refresh, s=screenshot) ---')
    while True:
        try:
            cmd = input('> ').strip().lower()
        except (EOFError, KeyboardInterrupt):
            break
        if cmd in ('q', 'quit', ''):
            break
        if cmd == 'r':
            driver.refresh()
            time.sleep(2)
            print('refreshed')
        elif cmd == 's':
            ts = datetime.now(IST).strftime('%y%m%d%H%M%S')
            logs = os.path.join(_HERE, 'logs')
            os.makedirs(logs, exist_ok=True)
            path = os.path.join(logs, f'validate_{scope_label}_{ts}.png')
            try:
                driver.save_screenshot(path)
                print(f'screenshot: {path}')
            except Exception as e:
                print(f'screenshot failed: {e}')
        elif cmd == 'i':
            try:
                xp = input('  xpath: ').strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not xp:
                continue
            try:
                attr_in = input('  attr (Enter=text, href, data-asin 등): ').strip()
            except (EOFError, KeyboardInterrupt):
                break
            attr_use = attr_in if attr_in else None
            f, s = match_xpath(scope, xp, attr=attr_use)
            sample_short = (str(s) if s is not None else '<empty>')[:200].replace('\n', ' ')
            print(f'  → {f} 매치, sample: "{sample_short}"')
        else:
            print('  명령: q | i | r | s')


def main():
    ap = argparse.ArgumentParser(description='Amazon xpath validator')
    ap.add_argument('--product', required=True, choices=['hhp', 'tv', 'ref', 'ldy'])
    ap.add_argument('--stage', required=True, choices=['main', 'bsr', 'detail'])
    ap.add_argument('--url', help='detail stage 시 product URL 필수')
    ap.add_argument('--card-rank', type=int, default=1, help='main/bsr 의 카드 #N 검사 (default 1)')
    ap.add_argument('--headless', action='store_true')
    args = ap.parse_args()

    if args.stage == 'detail' and not args.url:
        print('error: --stage detail 시 --url 필수', file=sys.stderr)
        return 2

    if args.stage == 'main':
        url = MAIN_URL_TEMPLATES[args.product]
    elif args.stage == 'bsr':
        url = BSR_URL_TEMPLATES[args.product]
    else:
        url = args.url

    print(f'[validate] product={args.product} stage={args.stage}')
    print(f'           url={url}')

    selectors = load_selectors(args.product, args.stage)
    print(f'[validate] DB selectors loaded: {len(selectors)}')

    driver = make_driver(headless=args.headless)
    try:
        driver.get(url)
        time.sleep(3)
        if args.stage in ('main', 'bsr'):
            scroll_bottom(driver)

        base_sel = next((s for s in selectors if s['data_field'] == 'base_container'), None)
        if base_sel and args.stage in ('main', 'bsr'):
            base_xpath = base_sel['xpath_primary']
            cards = driver.find_elements(By.XPATH, base_xpath)
            print(f'\n[base_container] {len(cards)} cards 매치')
            if not cards:
                print('  ❌ base_container 0 매치 — page 자체 결함 또는 anti-bot. selectors 검사 skip')
                interactive_loop(driver, driver, 'page')
                return 1
            idx = max(0, min(args.card_rank - 1, len(cards) - 1))
            scope = cards[idx]
            scope_label = f'card #{args.card_rank}/{len(cards)}'
            try:
                asin = scope.get_attribute('data-asin')
                if asin:
                    print(f'  카드 ASIN: {asin}  (page 에서 직접 비교)')
            except Exception:
                pass
        else:
            scope = driver
            scope_label = args.stage

        validate(scope, selectors, scope_label)
        interactive_loop(driver, scope, scope_label)
    finally:
        try:
            driver.quit()
        except Exception:
            pass

    return 0


if __name__ == '__main__':
    sys.exit(main())
