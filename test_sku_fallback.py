"""sku null URL 에 Model Name fallback 적용 검증 (DB 미적용).

기존 union 과 신규 union (Model Name 추가) 을 둘 다 시도해서
어느 쪽에서 잡히는지 한 줄씩 출력. 여러 urls 파일 동시 처리.

사용:
  python test_sku_fallback.py sku_null_ldy.txt sku_null_tv.txt

파일명 sku_null_<label>.txt 의 <label> 이 출력의 product 라벨로 들어감.

출력:
  IMPROVE    — primary + old_fb 둘 다 NULL, new_fb 가 잡음 (Model Name fix 효과)
  OK         — primary 또는 old_fb 에서 이미 잡힘 (이번 batch 가 lazy-load 였을 수도)
  STILL_NULL — 셋 다 NULL (Item details 자체 없는 부품/누락 케이스)
"""
from __future__ import annotations

import os
import random
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from selenium.common.exceptions import NoSuchElementException, WebDriverException
from selenium.webdriver.common.by import By

from amzn.detail import make_driver, scroll_to_bottom

# 현 DB selector
PRIMARY = (
    '//table//tr[.//th[contains(text(),"Manufacturer") '
    'and contains(text(),"Part Number")]]/td'
)
OLD_FALLBACK = (
    '//table//tr[.//th['
    'normalize-space()="Model Number" or '
    'normalize-space()="Part Number" or '
    'normalize-space()="Item Model Number" or '
    'normalize-space()="Item model number" or '
    'normalize-space()="Item Part Number" or '
    'normalize-space()="Item part number"'
    ']]/td'
)
NEW_FALLBACK = (
    '//table//tr[.//th['
    'normalize-space()="Model Number" or '
    'normalize-space()="Part Number" or '
    'normalize-space()="Item Model Number" or '
    'normalize-space()="Item model number" or '
    'normalize-space()="Item Part Number" or '
    'normalize-space()="Item part number" or '
    'normalize-space()="Model Name"'
    ']]/td'
)

EXPAND_XPATHS = [
    "//a[@data-action='a-expander-toggle' and .//span[contains(text(), 'Item details')]]",
    "//a[@data-action='a-expander-toggle' and .//span[contains(text(), 'Additional details')]]",
]


def extract(driver, xpath):
    try:
        el = driver.find_element(By.XPATH, xpath)
        v = el.text or el.get_attribute('textContent') or ''
        return v.strip() or None
    except (NoSuchElementException, WebDriverException):
        return None


def test_one(driver, url):
    try:
        driver.get(url)
        time.sleep(3)
    except WebDriverException as e:
        return None, None, None, f'goto_err: {type(e).__name__}'
    for xp in EXPAND_XPATHS:
        try:
            btn = driver.find_element(By.XPATH, xp)
            btn.click()
            time.sleep(0.5)
        except (NoSuchElementException, WebDriverException):
            pass
    try:
        scroll_to_bottom(driver, pause=0.7, max_scrolls=5)
    except Exception:
        pass
    return extract(driver, PRIMARY), extract(driver, OLD_FALLBACK), extract(driver, NEW_FALLBACK), None


def file_label(path):
    base = os.path.basename(path)
    if base.endswith('.txt'):
        base = base[:-4]
    if base.startswith('sku_null_'):
        base = base[len('sku_null_'):]
    return base or 'urls'


def main():
    if len(sys.argv) < 2:
        print('usage: python test_sku_fallback.py <urls_file>...', file=sys.stderr)
        sys.exit(2)
    items = []  # [(label, url), ...]
    for path in sys.argv[1:]:
        label = file_label(path)
        with open(path, encoding='utf-8') as f:
            for ln in f:
                ln = ln.strip()
                if ln and not ln.startswith('#'):
                    items.append((label, ln))
    if not items:
        print('[test] no URLs to test', file=sys.stderr)
        sys.exit(1)
    print(f'[test] {len(items)} URLs from {len(sys.argv) - 1} file(s)')
    driver = make_driver(headless=False)
    counts = {'IMPROVE': 0, 'OK': 0, 'STILL_NULL': 0, 'ERR': 0}
    per_label = {}
    try:
        for i, (label, url) in enumerate(items, 1):
            primary, old_fb, new_fb, err = test_one(driver, url)
            asin = url.rsplit('/', 1)[-1]
            if err:
                mark = 'ERR'
                detail = err
            elif new_fb and not primary and not old_fb:
                mark = 'IMPROVE'
                detail = f'new_fb={new_fb!r}'
            elif primary or old_fb:
                mark = 'OK'
                detail = f'primary={primary!r}  old_fb={old_fb!r}'
            else:
                mark = 'STILL_NULL'
                detail = '(셋 다 NULL — Item details 자체 없는 케이스 의심)'
            counts[mark] += 1
            per_label.setdefault(label, {'IMPROVE': 0, 'OK': 0, 'STILL_NULL': 0, 'ERR': 0})[mark] += 1
            print(f'[{i:3}/{len(items)}] {label:4} {mark:11} {asin}  {detail}')
            time.sleep(random.uniform(2, 3))
    finally:
        try:
            driver.quit()
        except Exception:
            pass
    print()
    print('=== 전체 요약 ===')
    for k, v in counts.items():
        print(f'  {k:11} : {v}')
    print()
    print('=== product 별 ===')
    for label, sub in per_label.items():
        cells = '  '.join(f'{k}={v}' for k, v in sub.items())
        print(f'  {label:4} : {cells}')


if __name__ == '__main__':
    main()
