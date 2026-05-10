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

from fpkt.listing import db_connect, extract_card, make_driver, scroll_to_bottom
from fpkt.detail import robust_click as _fpkt_robust_click

CONTROL_EXPAND = {'expand_specifications', 'expand_see_more'}
CONTROL_NAVIGATE = {'click_show_all_reviews'}
CONTROL_FIELDS = CONTROL_EXPAND | CONTROL_NAVIGATE


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


def short_text(e, n=60):
    """element text 한 줄 요약 — newline 제거 + n자 cut."""
    try:
        t = (e.text or '').strip().replace('\n', ' / ').replace('\r', '')
    except Exception:
        return '<err>'
    if len(t) > n:
        t = t[:n] + '…'
    return t


def evaluate(ctx, xpath, max_n=3, label='match'):
    """단순 매치 — REPL / fallback 검증용."""
    try:
        els = ctx.find_elements(By.XPATH, xpath)
    except WebDriverException as e:
        print(f'  {label}: error {type(e).__name__}')
        return
    print(f'  {label}: {len(els)}건')
    for e in els[:max_n]:
        print(f'    └─ {short_text(e)!r}')


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
    ap.add_argument('--only', default=None,
                    help='단일 schema 만 검증 (예: --only detailed_review_content). '
                         'review URL 직접 검증 시 detailed_review_content 만 빠르게.')
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

        # 카드 list 보관 (main/bsr 시 — base_container 매치)
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
                print(f'[warn] base_container 매치 fail: {type(e).__name__}',
                      file=sys.stderr)

        # 검증 순서: base_container 는 컨텍스트 잡았으니 step 에서 제외
        steps = [(f, xp, fb) for f, xp, fb in ordered if f != 'base_container']
        sel_dict = {f: {'xpath': xp, 'fallback': fb} for f, xp, fb in ordered}

        # detail 시 step 순서 재배치 — detail.py crawl_detail 흐름 정합:
        #   1. EXPAND control (expand_specifications / expand_see_more) — spec expand
        #   2. 일반 schema — detail page 의 메인 product 매치
        #   3. NAVIGATE control (click_show_all_reviews) — review page 이동
        #   4. detailed_review_content — review page 매치 (가장 마지막)
        # SQL id ASC 순서 ↔ detail.py 흐름 다름 (id ASC 면 click_review 가 step 3 으로
        # 너무 일찍, 후속 일반 schema 가 review page 에서 매치 결함).
        if args.stage == 'detail':
            expand_steps = [s for s in steps if s[0] in CONTROL_EXPAND]
            other_steps = [s for s in steps
                           if s[0] not in CONTROL_FIELDS and s[0] != 'detailed_review_content']
            navigate_steps = [s for s in steps if s[0] in CONTROL_NAVIGATE]
            review_steps = [s for s in steps if s[0] == 'detailed_review_content']
            steps = expand_steps + other_steps + navigate_steps + review_steps

        # --only filter (단일 schema 만 검증)
        if args.only:
            steps = [s for s in steps if s[0] == args.only]
            if not steps:
                print(f'[error] --only "{args.only}" 매치 selector 없음', file=sys.stderr)
                return 1
            print(f'[info] --only mode: {args.only} 1 schema 만 검증', file=sys.stderr)

        def compute_final(card_ctx):
            """현재 컨텍스트 의 extract_card 결과 (main/bsr 시만)."""
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
            print('  cn            → 다음 카드 (같은 schema 다시)')
            print('  cp            → 이전 카드')
            print('  c<N>          → N번째 카드 (예 c5)')
            print('  cards / cl    → 카드 목록 (retailer_sku_name)')
        print('  quit / q      → 종료')
        print('=' * 70)

        # 마지막 schema enter 시 자동 다음 카드 진행 (cards_list 있을 때).
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
                break  # 마지막 카드 의 마지막 schema

            field, xp, fb = steps[i]
            is_control = field in CONTROL_FIELDS
            print()
            print('─' * 70)
            print(f'[{card_label()}] [{i+1}/{len(steps)}] {field}')
            print('─' * 70)
            if is_control:
                action = 'navigate' if field in CONTROL_NAVIGATE else 'click'
                print(f'  ⚙ control action ({action}) — enter 시 자동 수행')
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
                    # enter — control field 시 action 수행 후 다음 schema
                    if is_control and xp:
                        if field in CONTROL_NAVIGATE:
                            # click_show_all_reviews — anchor href 추출 후 driver.get
                            try:
                                els = driver.find_elements(By.XPATH, xp)
                                rev_href = None
                                for e in els:
                                    href = e.get_attribute('href') or ''
                                    if '/product-reviews/' in href:
                                        rev_href = href
                                        break
                                if rev_href:
                                    print(f'  ⚙ navigating: {rev_href[:80]}…')
                                    driver.get(rev_href)
                                    time.sleep(3)
                                    scroll_to_bottom(driver, pause=1.2, max_scrolls=10)
                                    # review body lazy load wait — length>50 element 등장 까지 15s
                                    try:
                                        from selenium.webdriver.support.ui import WebDriverWait
                                        from selenium.common.exceptions import TimeoutException
                                        WebDriverWait(driver, 15, poll_frequency=0.5).until(
                                            lambda d: any(
                                                len((e.text or '').strip()) > 50
                                                for e in d.find_elements(By.XPATH,
                                                    '//span[@class="css-1jxf684"]')
                                            )
                                        )
                                        print('  ⚙ review body lazy load 완료')
                                    except TimeoutException:
                                        print('  ⚙ review body lazy 15s 미완 — 진행 (자연 NULL 가능)')
                                    print('  ⚙ navigate 완료')
                                else:
                                    print('  ⚙ anchor href 미매치 — navigate skip')
                            except WebDriverException as e:
                                print(f'  ⚙ navigate fail: {type(e).__name__}')
                        else:
                            # expand_specifications / expand_see_more — robust_click
                            try:
                                ok = _fpkt_robust_click(driver, xp, wait_s=20.0)
                                print(f'  ⚙ click {"성공" if ok else "실패"}')
                                if ok:
                                    time.sleep(1.5)
                                    scroll_to_bottom(driver, pause=1.0, max_scrolls=5)
                            except Exception as e:
                                print(f'  ⚙ click fail: {type(e).__name__}: {e}')
                    break  # 다음 schema (또는 다음 카드 — outer loop 처리)
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
                # 카드 변경 명령
                if cards_list and line == 'cn':
                    if card_idx + 1 < len(cards_list):
                        card_idx += 1
                        ctx = cards_list[card_idx]
                        final_rec = compute_final(ctx)
                        advance = False  # 같은 schema 다시
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
                        # 카드 별 retailer_sku_name 매치 — listing.py 의 selector
                        rsku = ''
                        try:
                            r_sel = sel_dict.get('retailer_sku_name', {}).get('xpath')
                            if r_sel:
                                els = c.find_elements(By.XPATH, r_sel)
                                if els:
                                    rsku = (els[0].text or '').strip().replace('\n', ' / ')[:60]
                        except Exception:
                            pass
                        print(f' {marker} [c{j+1}] {rsku}')
                    continue
                # 추가 xpath 검증
                evaluate(ctx, line, max_n=10)
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
