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

import psycopg2.extras

from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.by import By

from fpkt.listing import db_connect, make_driver, scroll_to_bottom


def load_selectors_ordered(site_account, stage, domain):
    """id ASC 순서 — INSERT 순서 보장 (listing.py extract_card 의 dict iteration 과 같은 순서)."""
    sql = """
        SELECT data_field, xpath_primary, fallback_xpath
          FROM dx_siel_xpath_selectors
         WHERE site_account = %s
           AND page_type    = %s
           AND domain       = %s
           AND is_active    = TRUE
         ORDER BY id ASC
    """
    conn = db_connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(sql, (site_account, stage, domain))
            rows = cur.fetchall()
    finally:
        conn.close()
    return [(r['data_field'], r['xpath_primary'], r['fallback_xpath']) for r in rows]


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
    ordered = load_selectors_ordered(args.site_account, args.stage, args.product)
    print(f'[info] {len(ordered)} selectors loaded (id ASC 순서 — 수집 순서)',
          file=sys.stderr)
    if not ordered:
        print(f'[error] no selectors. site_account/stage/product 확인.', file=sys.stderr)
        return 1

    bc_entry = next(((f, xp, fb) for f, xp, fb in ordered if f == 'base_container'), None)

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
        if args.stage in ('main', 'bsr') and bc_entry:
            bc_xpath = bc_entry[1]
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

        # 검증 순서: base_container 는 컨텍스트 잡았으니 step 에서 제외
        steps = [(f, xp, fb) for f, xp, fb in ordered if f != 'base_container']

        print()
        print('=' * 70)
        print(f'STEP-BY-STEP 검증 — schema {len(steps)}개 / 컨텍스트: {ctx_label}')
        print('각 schema 마다:')
        print('  enter         → 다음 schema')
        print('  xpath 입력    → 추가 검증 (같은 schema 유지)')
        print('  back / b      → 이전 schema')
        print('  list / l      → 전체 schema 목록')
        print('  jump <N>      → N번째 schema 로 이동')
        print('  quit / q      → 종료')
        print('=' * 70)

        i = 0
        while i < len(steps):
            field, xp, fb = steps[i]
            print()
            print(f'[{i+1}/{len(steps)}] {field}')
            print(f'  xpath: {xp}')
            if not xp:
                print('  (xpath 없음)')
            else:
                evaluate(ctx, xp)
                if fb:
                    print(f'  fallback: {fb}')
                    evaluate(ctx, fb)
            # 같은 schema 안 prompt loop
            advance = True
            while True:
                try:
                    line = input(f'[{i+1}/{len(steps)}] {field}> ').strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    return 0
                if not line:
                    break  # enter — 다음
                if line in ('quit', 'exit', 'q'):
                    return 0
                if line in ('back', 'b'):
                    i = max(0, i - 1)
                    advance = False
                    break
                if line in ('list', 'l'):
                    for j, (f, _, _) in enumerate(steps):
                        marker = ' →' if j == i else '  '
                        print(f' {marker} [{j+1}] {f}')
                    continue
                if line.startswith('jump '):
                    try:
                        n = int(line[5:].strip())
                        if 1 <= n <= len(steps):
                            i = n - 1
                            advance = False
                            break
                        print(f'  range 1..{len(steps)}')
                    except ValueError:
                        print('  usage: jump <N>')
                    continue
                # 추가 xpath 검증
                evaluate(ctx, line, max_n=10)
            if advance:
                i += 1

        print()
        print('=== STEP 검증 끝 — free REPL (xpath 자유 / quit 종료) ===')
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
