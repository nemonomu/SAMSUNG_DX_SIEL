r"""
Flipkart xpath 검증 REPL — main/bsr/detail page 별 selector 자동 검증 + 사용자
입력 xpath 매치 확인.

사용:
  py -3 tools\fpkt\xpath_validator.py --product hhp --stage main \
    --url "https://www.flipkart.com/search?q=smartphone&store=tyy%2F4io&sort=popularity"
  py -3 tools\fpkt\xpath_validator.py --product hhp --stage detail \
    --url https://www.flipkart.com/apple-iphone-16-teal-128-gb/p/itmce4bb3f55cc2f

동작:
  1. uc.Chrome 시작 (headless=False — 사용자가 page 시각 확인) → URL navigate → scroll
  2. DB 에서 dx_siel_xpath_selectors load (Flipkart, stage, domain=product)
  3. 모든 data_field 자동 매치 — count + first 5 elements text/href/cls 출력
  4. interactive REPL — 'xpath> ' prompt 에 사용자 자유 입력
  5. 'quit' / 'exit' / 'q' / Ctrl+C 종료 (driver.quit)

main/bsr 시 base_container 첫 카드 컨텍스트 사용 (relative xpath 정확 매치).
detail 시 driver 전체 컨텍스트.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.by import By

from fpkt.listing import db_connect, load_selectors, make_driver, scroll_to_bottom  # noqa: F401


def describe_element(e):
    try:
        txt = (e.text or '').strip()[:120]
    except Exception:
        txt = '<err>'
    parts = [f'text={txt!r}']
    for attr in ('href', 'src', 'class'):
        try:
            v = e.get_attribute(attr)
            if v:
                parts.append(f'{attr}={v[:80]!r}')
        except Exception:
            pass
    return '  ' + ' '.join(parts)


def evaluate(ctx, xpath, max_n=5):
    try:
        els = ctx.find_elements(By.XPATH, xpath)
    except WebDriverException as e:
        print(f'  [error] {type(e).__name__}: {e}')
        return
    print(f'  count: {len(els)}')
    for e in els[:max_n]:
        print(describe_element(e))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--product', required=True, choices=['hhp', 'tv', 'ref', 'ldy'])
    ap.add_argument('--stage', required=True, choices=['main', 'bsr', 'detail'])
    ap.add_argument('--url', required=True)
    ap.add_argument('--scroll', type=int, default=10,
                    help='scroll_to_bottom max_scrolls (default 10)')
    ap.add_argument('--card-index', type=int, default=0,
                    help='main/bsr 시 base_container N번째 카드 (default 0=첫 카드)')
    ap.add_argument('--site-account', default='Flipkart',
                    help='DB site_account (default Flipkart)')
    args = ap.parse_args()

    print(f'[info] loading selectors: {args.site_account} / {args.stage} / {args.product}',
          file=sys.stderr)
    selectors = load_selectors(args.site_account, args.stage, args.product)
    print(f'[info] {len(selectors)} selectors loaded', file=sys.stderr)
    if not selectors:
        print(f'[error] no selectors. site_account/stage/product 확인.', file=sys.stderr)
        return 1

    print('[info] starting driver (headless=False)', file=sys.stderr)
    driver = make_driver(headless=False)
    try:
        print(f'[info] navigating: {args.url}', file=sys.stderr)
        driver.get(args.url)
        time.sleep(3)
        if args.scroll > 0:
            scroll_to_bottom(driver, pause=1.2, max_scrolls=args.scroll)

        ctx = driver
        ctx_label = 'driver (page 전체)'
        if args.stage in ('main', 'bsr') and 'base_container' in selectors:
            bc_xpath = selectors['base_container']['xpath']
            try:
                cards = driver.find_elements(By.XPATH, bc_xpath)
                if cards:
                    idx = max(0, min(args.card_index, len(cards) - 1))
                    ctx = cards[idx]
                    ctx_label = f'base_container[{idx}] (총 {len(cards)} 카드)'
                else:
                    print('[warn] base_container 매치 0 — driver 컨텍스트 사용',
                          file=sys.stderr)
            except WebDriverException as e:
                print(f'[warn] base_container 매치 fail: {type(e).__name__}',
                      file=sys.stderr)

        print()
        print('=' * 70)
        print(f'AUTO 검증 — selector {len(selectors)}개 / 컨텍스트: {ctx_label}')
        print('=' * 70)
        for field in sorted(selectors.keys()):
            sel = selectors[field]
            xp = sel.get('xpath')
            fb = sel.get('fallback')
            print()
            print(f'[{field}]')
            print(f'  xpath: {xp}')
            if not xp:
                print('  (xpath 없음)')
                continue
            evaluate(ctx, xp)
            if fb:
                print(f'  fallback: {fb}')
                evaluate(ctx, fb)

        print()
        print('=' * 70)
        print('REPL — xpath 입력 (quit/exit/q/Ctrl+C 종료)')
        print('컨텍스트:', ctx_label)
        print('=' * 70)
        while True:
            try:
                line = input('xpath> ').strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not line or line in ('quit', 'exit', 'q'):
                break
            evaluate(ctx, line, max_n=10)
    finally:
        try:
            driver.quit()
        except Exception:
            pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
