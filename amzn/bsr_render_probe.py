"""
Probe Amazon BSR lazy rendering for one product.

This script only opens Amazon BSR page 1 and page 2, tries several real
scroll/input strategies, and prints the rendered gridItemRoot card count plus
basic card fields. It does not read DB selectors and does not write DB rows.

Usage:
  python amzn\\bsr_render_probe.py --product ldy
  python amzn\\bsr_render_probe.py --product ldy --pause
  python amzn\\bsr_render_probe.py --product tv --page-load-strategy none
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from selenium.common.exceptions import WebDriverException
from selenium.webdriver import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

from amzn import listing as L


CARD_XPATH = '//div[@id="gridItemRoot"]'


def log(msg: str) -> None:
    print(f'{datetime.now():%H:%M:%S} {msg}', flush=True)


def js(driver, script: str, *args):
    try:
        return driver.execute_script(script, *args)
    except WebDriverException:
        return None


def page_metrics(driver) -> dict:
    data = js(
        driver,
        """
        return {
          y: Math.round(window.scrollY || 0),
          innerHeight: Math.round(window.innerHeight || 0),
          bodyHeight: Math.round(document.body.scrollHeight || 0),
          docHeight: Math.round(document.documentElement.scrollHeight || 0),
          readyState: document.readyState,
          gridItems: document.querySelectorAll('#gridItemRoot').length,
          dataAsins: document.querySelectorAll('[data-asin]').length,
          rankTokens: (document.documentElement.innerHTML.match(/render\\.zg\\.rank/g) || []).length,
          endOfList: !!document.querySelector('#endOfList'),
          pagination: !!document.querySelector('nav[aria-label="pagination"], .a-pagination')
        };
        """,
    )
    return data or {}


def cards(driver):
    try:
        return driver.find_elements(By.XPATH, CARD_XPATH)
    except WebDriverException:
        return []


def count_cards(driver) -> int:
    return len(cards(driver))


def show_count(driver, label: str) -> int:
    m = page_metrics(driver)
    n = int(m.get('gridItems') or 0)
    log(
        f'{label}: cards={n} y={m.get("y")} inner={m.get("innerHeight")} '
        f'height={max(int(m.get("bodyHeight") or 0), int(m.get("docHeight") or 0))} '
        f'rankTokens={m.get("rankTokens")} end={m.get("endOfList")} '
        f'pagination={m.get("pagination")} ready={m.get("readyState")}'
    )
    return n


def dispatch_wheel(driver, delta_y: int) -> None:
    js(
        driver,
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


def selenium_wheel(driver, amount: int) -> None:
    try:
        ActionChains(driver).scroll_by_amount(0, amount).perform()
    except Exception:
        dispatch_wheel(driver, amount)


def key_scroll(driver, key: str, times: int = 1) -> None:
    try:
        body = driver.find_element(By.TAG_NAME, 'body')
        for _ in range(times):
            body.send_keys(key)
            time.sleep(0.25)
    except WebDriverException:
        pass


def focus_grid(driver) -> None:
    js(
        driver,
        """
        const first = document.querySelector('#gridItemRoot a, #gridItemRoot');
        if (first) {
          first.scrollIntoView({block: 'center'});
          if (first.focus) first.focus();
        }
        """,
    )


def scroll_to_selector(driver, selector: str) -> None:
    js(
        driver,
        """
        const el = document.querySelector(arguments[0]);
        if (el) el.scrollIntoView({block: 'center', inline: 'nearest'});
        """,
        selector,
    )


def render_to_target(driver, target: int, max_rounds: int) -> int:
    show_count(driver, 'initial')
    focus_grid(driver)
    time.sleep(1.0)

    for round_no in range(1, max_rounds + 1):
        before = count_cards(driver)

        # Mix real user-like inputs with JS scroll. Amazon BSR sometimes reacts
        # to wheel/key events better than direct window.scrollTo only.
        selenium_wheel(driver, 900)
        time.sleep(0.5)
        dispatch_wheel(driver, 900)
        time.sleep(0.5)
        key_scroll(driver, Keys.PAGE_DOWN, 2)
        time.sleep(0.5)

        if round_no % 2 == 0:
            scroll_to_selector(driver, '#endOfList')
            time.sleep(0.8)
            key_scroll(driver, Keys.HOME, 1)
            time.sleep(0.5)
            key_scroll(driver, Keys.PAGE_DOWN, 5)
            time.sleep(0.5)

        if round_no % 3 == 0:
            scroll_to_selector(driver, 'nav[aria-label="pagination"], .a-pagination')
            time.sleep(0.8)
            key_scroll(driver, Keys.END, 1)
            time.sleep(0.8)

        after = show_count(driver, f'round {round_no} before={before}')
        if after >= target:
            return after

    return count_cards(driver)


def text_or_empty(root, xpath: str) -> str:
    try:
        el = root.find_element(By.XPATH, xpath)
        return (el.text or el.get_attribute('aria-label') or '').strip()
    except WebDriverException:
        return ''


def attr_or_empty(root, xpath: str, attr: str) -> str:
    try:
        return (root.find_element(By.XPATH, xpath).get_attribute(attr) or '').strip()
    except WebDriverException:
        return ''


def extract_card(card, rank: int) -> dict:
    href = attr_or_empty(card, './/a[contains(@href,"/dp/")]', 'href')
    asin = L.asin_from_text(href) or L.scan_card_asin(card) or ''
    return {
        'rank': rank,
        'asin': asin,
        'url': L.canonical_product_url(asin) if asin else href,
        'name': text_or_empty(
            card,
            './/div[contains(@class,"p13n-sc-css-line-clamp") and normalize-space(text())] '
            '| .//div[contains(@class,"p13n-sc-truncate") and normalize-space(text())] '
            '| .//a[contains(@href,"/dp/")]//*[self::span or self::div][normalize-space(text())][1]',
        ),
        'price': text_or_empty(
            card,
            './/span[contains(@class,"p13n-sc-price")] '
            '| .//span[contains(@class,"a-price")]//span[@class="a-offscreen"]',
        ),
        'star': text_or_empty(
            card,
            './/i[contains(@class,"a-icon-star")]//span '
            '| .//*[@aria-label and contains(@aria-label,"out of 5 stars")]',
        ),
        'rating_count': text_or_empty(
            card,
            './/a[contains(@href,"customerReviews")]//span[normalize-space(text())] '
            '| .//span[contains(@class,"a-size-small") and normalize-space(text())][1]',
        ),
    }


def save_snapshot(driver, out_dir: str, product: str, page_no: int) -> None:
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f'bsr_probe_{product}_p{page_no}_{datetime.now():%y%m%d%H%M%S}.html')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(driver.page_source)
    log(f'saved_html={path}')


def run_page(driver, product: str, page_no: int, url: str, args) -> None:
    log('=' * 90)
    log(f'page={page_no} url={url}')
    driver.get(url)
    time.sleep(args.initial_wait)
    try:
        driver.set_window_size(args.width, args.height)
    except WebDriverException:
        pass

    final_count = render_to_target(driver, args.target, args.rounds)
    log(f'page={page_no} final_count={final_count} target={args.target}')
    save_snapshot(driver, args.out_dir, product, page_no)

    rows = [extract_card(card, i) for i, card in enumerate(cards(driver), start=1)]
    for row in rows:
        name = (row['name'][:80] + '...') if len(row['name']) > 80 else row['name']
        log(
            f"card rank={row['rank']:02d} asin={row['asin']} "
            f"star={row['star']!r} ratings={row['rating_count']!r} "
            f"price={row['price']!r} name={name!r}"
        )

    missing = args.target - len(rows)
    if missing > 0:
        log(f'WARNING page={page_no} missing_cards={missing}')


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--product', choices=sorted(L.BSR_URL_TEMPLATES), default='ldy')
    parser.add_argument('--target', type=int, default=50)
    parser.add_argument('--rounds', type=int, default=12)
    parser.add_argument('--initial-wait', type=float, default=5.0)
    parser.add_argument('--width', type=int, default=1920)
    parser.add_argument('--height', type=int, default=1400)
    parser.add_argument('--headless', action='store_true')
    parser.add_argument('--pause', action='store_true', help='wait before closing browser')
    parser.add_argument('--out-dir', default=os.path.join(_ROOT, 'amzn', 'logs'))
    parser.add_argument(
        '--page-load-strategy',
        choices=['none', 'eager', 'normal'],
        default='none',
        help='Chrome pageLoadStrategy for BSR page loading (default none)',
    )
    args = parser.parse_args()

    log(
        f'start product={args.product} headless={args.headless} '
        f'target={args.target} rounds={args.rounds} window={args.width}x{args.height} '
        f'page_load_strategy={args.page_load_strategy}'
    )
    log('starting Chrome driver...')
    driver = L.make_driver(
        headless=args.headless,
        page_load_strategy=args.page_load_strategy,
    )
    try:
        log('Chrome driver started')
        try:
            driver.set_window_size(args.width, args.height)
        except WebDriverException:
            pass
        for page_no, url in enumerate(L.BSR_URL_TEMPLATES[args.product], start=1):
            run_page(driver, args.product, page_no, url, args)
        if args.pause:
            input('Press Enter to close browser...')
    finally:
        try:
            driver.quit()
        except Exception:
            pass
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
