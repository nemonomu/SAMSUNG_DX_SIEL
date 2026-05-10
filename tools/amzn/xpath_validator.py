r"""
Amazon xpath 검증 REPL — main/bsr/detail page 별 selector 자동 검증 + 사용자
입력 xpath 매치 확인. fpkt/xpath_validator.py 와 같은 패턴.

사용:
  py -3 tools\amzn\xpath_validator.py --product hhp --stage main \
    --url "https://www.amazon.in/s?k=smartphone&i=electronics&page=1"
  py -3 tools\amzn\xpath_validator.py --product tv --stage bsr \
    --url "https://www.amazon.in/gp/bestsellers/electronics/1389396031/"
  py -3 tools\amzn\xpath_validator.py --product hhp --stage detail \
    --url https://www.amazon.in/dp/B0XXXXXXXX

동작:
  1. uc.Chrome 시작 (headless=False — 사용자가 page 시각 확인) → URL navigate → scroll
  2. DB 에서 dx_siel_xpath_selectors load (Amazon, stage, domain=product)
  3. 모든 data_field 자동 매치 — count + first 3 elements text/href 출력
  4. interactive REPL — 'xpath> ' prompt 에 사용자 자유 입력
  5. 'quit' / 'exit' / 'q' / Ctrl+C 종료 (driver.quit)

main/bsr 시 base_container 첫 카드 컨텍스트 사용 (relative xpath 정확 매치).
detail 시 driver 전체 컨텍스트, expand control field (expand_additional_details /
expand_item_details) 자동 click 처리 (enter 시).
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

from amzn.listing import db_connect, extract_card, make_driver, scroll_to_bottom

# Amazon detail control fields — 클릭 트리거 (expand spec section)
CONTROL_EXPAND = {'expand_additional_details', 'expand_item_details'}
CONTROL_FIELDS = CONTROL_EXPAND


def load_selectors_ordered(site_account, stage, domain):
    """id ASC 순서 — INSERT 순서 보장."""
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


def short_text(e, n=60):
    try:
        t = (e.text or '').strip().replace('\n', ' / ').replace('\r', '')
    except Exception:
        return '<err>'
    if len(t) > n:
        t = t[:n] + '…'
    return t


def evaluate(ctx, xpath, max_n=3, label='match'):
    try:
        els = ctx.find_elements(By.XPATH, xpath)
    except WebDriverException as e:
        print(f'  {label}: error {type(e).__name__}')
        return
    print(f'  {label}: {len(els)}건')
    for e in els[:max_n]:
        print(f'    └─ {short_text(e)!r}')


def try_click_expand(driver, xpath: str) -> bool:
    """expand_additional_details / expand_item_details 클릭."""
    try:
        btn = driver.find_element(By.XPATH, xpath)
        btn.click()
        return True
    except WebDriverException as e:
        print(f'  ⚙ click fail: {type(e).__name__}')
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--product', required=True, choices=['hhp', 'tv', 'ref', 'ldy'])
    ap.add_argument('--stage', required=True, choices=['main', 'bsr', 'detail'])
    ap.add_argument('--url', required=True)
    ap.add_argument('--scroll', type=int, default=8,
                    help='scroll_to_bottom max_scrolls (default 8)')
    ap.add_argument('--card-index', type=int, default=0,
                    help='main/bsr 시 base_container N번째 카드 (default 0=첫 카드)')
    ap.add_argument('--site-account', default='Amazon',
                    help='DB site_account (default Amazon)')
    ap.add_argument('--only', default=None,
                    help='단일 schema 만 검증 (예: --only original_sku_price)')
    args = ap.parse_args()

    print(f'[info] loading selectors: {args.site_account} / {args.stage} / {args.product}',
          file=sys.stderr)
    ordered = load_selectors_ordered(args.site_account, args.stage, args.product)
    print(f'[info] {len(ordered)} selectors loaded (id ASC 순서 — 수집 순서)',
          file=sys.stderr)
    if not ordered:
        print('[error] no selectors. site_account/stage/product 확인.', file=sys.stderr)
        return 1

    bc_entry = next(((f, xp, fb) for f, xp, fb in ordered if f == 'base_container'), None)

    print('[info] starting driver (headless=False)', file=sys.stderr)
    driver = make_driver(headless=False)
    try:
        print(f'[info] navigating: {args.url}', file=sys.stderr)
        driver.get(args.url)
        time.sleep(3)
        if args.scroll > 0:
            scroll_to_bottom(driver, pause=1.0, max_scrolls=args.scroll)

        # main/bsr — base_container 카드 list 보관
        cards_list = []
        card_idx = 0
        ctx = driver
        if args.stage in ('main', 'bsr') and bc_entry:
            bc_xpath = bc_entry[1]
            try:
                cards_list = driver.find_elements(By.XPATH, bc_xpath)
                if cards_list:
                    card_idx = max(0, min(args.card_index, len(cards_list) - 1))
                    ctx = cards_list[card_idx]
                else:
                    print('[warn] base_container 매치 0 — driver 컨텍스트 사용',
                          file=sys.stderr)
            except WebDriverException as e:
                print(f'[warn] base_container fail: {type(e).__name__}', file=sys.stderr)

        steps = [(f, xp, fb) for f, xp, fb in ordered if f != 'base_container']
        sel_dict = {f: {'xpath': xp, 'fallback': fb} for f, xp, fb in ordered}

        # detail 시 step 순서 — expand control 먼저, 일반 schema 나중
        if args.stage == 'detail':
            expand_steps = [s for s in steps if s[0] in CONTROL_EXPAND]
            other_steps = [s for s in steps if s[0] not in CONTROL_FIELDS]
            steps = expand_steps + other_steps

        if args.only:
            steps = [s for s in steps if s[0] == args.only]
            if not steps:
                print(f'[error] --only "{args.only}" 매치 selector 없음', file=sys.stderr)
                return 1
            print(f'[info] --only mode: {args.only} 1 schema 만 검증', file=sys.stderr)

        def compute_final(card_ctx):
            """현 컨텍스트의 extract_card 결과 (main/bsr 시만)."""
            if args.stage in ('main', 'bsr'):
                try:
                    return extract_card(card_ctx, sel_dict)
                except Exception as e:
                    print(f'[warn] extract_card fail: {type(e).__name__}: {e}',
                          file=sys.stderr)
            return {}

        final_rec = compute_final(ctx)

        def card_label():
            if cards_list:
                return f'card {card_idx+1}/{len(cards_list)}'
            return 'page'

        print()
        print('=' * 70)
        print(f'STEP-BY-STEP 검증 — schema {len(steps)}개')
        if cards_list:
            print(f'카드 {len(cards_list)}개 — 시작: card {card_idx+1}')
        print('명령:')
        print('  enter         → 다음 schema')
        print('  xpath 입력    → 추가 검증 (같은 schema 유지)')
        print('  back / b      → 이전 schema')
        print('  jump <N>      → N번째 schema 로 이동')
        print('  list / l      → schema 목록')
        if cards_list:
            print('  cn / cp       → 다음/이전 카드')
            print('  c<N>          → N번째 카드 (예 c5)')
            print('  cards / cl    → 카드 목록 (data-asin)')
        print('  quit / q      → 종료')
        print('=' * 70)

        i = 0
        while True:
            if i >= len(steps):
                # schema loop 끝 — 다음 카드 자동 진행
                if cards_list and card_idx + 1 < len(cards_list):
                    card_idx += 1
                    ctx = cards_list[card_idx]
                    final_rec = compute_final(ctx)
                    i = 0
                    print()
                    print('▼' * 70)
                    print(f'다음 카드 진입 — {card_label()}')
                    print('▼' * 70)
                    continue
                break

            field, xp, fb = steps[i]
            is_control = field in CONTROL_FIELDS
            print()
            print('─' * 70)
            print(f'[{card_label()}] [{i+1}/{len(steps)}] {field}')
            print('─' * 70)
            if is_control:
                print('  ⚙ control action (click) — enter 시 자동 수행')
            if args.stage in ('main', 'bsr'):
                v = final_rec.get(field, '<not_extracted>')
                print(f'  ★ saved: {v!r}')
            if not xp:
                print('  xpath: (없음)')
            else:
                print(f'  xpath: {xp}')
                evaluate(ctx, xp, max_n=3, label='match')
                if fb:
                    print(f'  fallback: {fb}')
                    evaluate(ctx, fb, max_n=3, label='fb')

            advance = True
            while True:
                try:
                    line = input(f'[{card_label()}] [{i+1}/{len(steps)}] {field}> ').strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    return 0
                if not line:
                    # enter — control field 시 click 후 다음 schema
                    if is_control and xp:
                        ok = try_click_expand(driver, xp)
                        if ok:
                            print('  ⚙ click 성공')
                            time.sleep(1.0)
                    break
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
                if cards_list and line == 'cn':
                    if card_idx + 1 < len(cards_list):
                        card_idx += 1
                        ctx = cards_list[card_idx]
                        final_rec = compute_final(ctx)
                        advance = False
                        break
                    print(f'  마지막 카드 (총 {len(cards_list)})')
                    continue
                if cards_list and line == 'cp':
                    if card_idx > 0:
                        card_idx -= 1
                        ctx = cards_list[card_idx]
                        final_rec = compute_final(ctx)
                        advance = False
                        break
                    print('  첫 카드')
                    continue
                if cards_list and len(line) > 1 and line[0] == 'c' and line[1:].isdigit():
                    n = int(line[1:])
                    if 1 <= n <= len(cards_list):
                        card_idx = n - 1
                        ctx = cards_list[card_idx]
                        final_rec = compute_final(ctx)
                        advance = False
                        break
                    print(f'  card range 1..{len(cards_list)}')
                    continue
                if cards_list and line in ('cards', 'cl'):
                    print(f'  총 {len(cards_list)} 카드:')
                    for j, c in enumerate(cards_list):
                        marker = ' →' if j == card_idx else '  '
                        asin = ''
                        try:
                            asin = c.get_attribute('data-asin') or ''
                        except Exception:
                            pass
                        print(f' {marker} [c{j+1}] {asin}')
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
