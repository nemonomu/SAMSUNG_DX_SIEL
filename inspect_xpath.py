"""단일 URL inspect REPL — driver 1번 띄우고 xpath 반복 입력으로 결과 확인.

사용:
  python inspect_xpath.py <URL>
  python inspect_xpath.py <URL> --headless

REPL 명령:
  <xpath>                  → find_elements + 모든 결과 text 출력 (count + 처음 5개)
  attr <xpath>             → 첫 element href attr
  attr <xpath> <attr_name> → 첫 element 의 specified attr
  click <xpath>            → 클릭 (expand 등) — 성공/실패 출력
  scroll                   → scroll_to_bottom 1회
  spec                     → expand_specifications 클릭 (Specifications div)
  see                      → expand_see_more 클릭 (Read More)
  reload                   → driver.get(url) 다시
  source <path>            → 현재 page_source 를 파일로 저장
  ?, help                  → 도움말
  q, exit                  → 종료
"""
from __future__ import annotations

import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import undetected_chromedriver as uc
from selenium.common.exceptions import NoSuchElementException, WebDriverException
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By

import siel_log

uc.Chrome.__del__ = lambda self: None

HELP = __doc__.split('REPL 명령:')[1] if 'REPL 명령:' in __doc__ else ''


def make_driver(headless: bool = False):
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


def scroll_to_bottom(driver, pause=1.0, max_scrolls=15):
    last_h = driver.execute_script('return document.body.scrollHeight')
    for _ in range(max_scrolls):
        driver.execute_script('window.scrollTo(0, document.body.scrollHeight);')
        time.sleep(pause)
        new_h = driver.execute_script('return document.body.scrollHeight')
        if new_h == last_h:
            break
        last_h = new_h


def robust_click(driver, xpath: str) -> bool:
    try:
        el = driver.find_element(By.XPATH, xpath)
    except (NoSuchElementException, WebDriverException) as e:
        print(f'  [click] not found: {e}')
        return False
    try:
        driver.execute_script('arguments[0].scrollIntoView({block: "center"});', el)
        time.sleep(0.3)
    except WebDriverException:
        pass
    for kind, fn in [
        ('native', lambda: el.click()),
        ('js',     lambda: driver.execute_script('arguments[0].click();', el)),
        ('action', lambda: ActionChains(driver).move_to_element(el).pause(0.2).click(el).perform()),
    ]:
        try:
            fn()
            print(f'  [click] OK ({kind})')
            return True
        except WebDriverException as e:
            print(f'  [click] {kind} fail: {type(e).__name__}')
    return False


def find_elements_safe(driver, xpath: str):
    try:
        return driver.find_elements(By.XPATH, xpath)
    except WebDriverException as e:
        print(f'  [xpath error] {type(e).__name__}: {e}')
        return []


def get_text(el) -> str:
    try:
        return (el.text or el.get_attribute('textContent') or '').strip()
    except WebDriverException:
        return ''


def show_xpath(driver, xpath: str):
    els = find_elements_safe(driver, xpath)
    print(f'  count={len(els)}')
    for i, e in enumerate(els[:5]):
        t = get_text(e)
        if len(t) > 200:
            t = t[:200] + '...'
        print(f'  [{i}] {t!r}')
    if len(els) > 5:
        print(f'  ... +{len(els) - 5} more')


def show_attr(driver, xpath: str, attr: str):
    els = find_elements_safe(driver, xpath)
    print(f'  count={len(els)}')
    for i, e in enumerate(els[:5]):
        try:
            v = e.get_attribute(attr)
        except WebDriverException:
            v = None
        if v and len(v) > 200:
            v = v[:200] + '...'
        print(f'  [{i}] {attr}={v!r}')


def repl(driver, url: str):
    print(f'\n[repl] driver loaded: {url}\n[repl] ? for help, q to quit\n')
    while True:
        try:
            line = input('xpath> ').strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line in ('q', 'exit', 'quit'):
            break
        if line in ('?', 'help'):
            print(HELP)
            continue
        if line == 'reload':
            driver.get(url)
            time.sleep(3)
            print('  [reload] OK')
            continue
        if line == 'scroll':
            scroll_to_bottom(driver, pause=1.0, max_scrolls=10)
            print('  [scroll] OK')
            continue
        if line == 'spec':
            robust_click(driver, '//div[normalize-space(text())="Specifications" and not(ancestor::head)]')
            time.sleep(1)
            continue
        if line == 'see':
            robust_click(driver, '//button[normalize-space(text())="Read More" or normalize-space(text())="See more" or normalize-space(text())="Show more" or normalize-space(text())="View More"] | //div[normalize-space(text())="Read More" or normalize-space(text())="See more" or normalize-space(text())="Show more" or normalize-space(text())="View More"]')
            time.sleep(1)
            continue
        # 컬럼별 단축키 — Flipkart detail spec 의 SQL xpath 와 동일
        SHORTCUTS = {
            'sku':       '//div[normalize-space(text())="Model Name"]/following-sibling::div[1]',
            'storage':   '//div[contains(text(),"GB ROM") or contains(text(),"TB ROM")][1]',
            'color':     '//div[normalize-space(text())="Color"]/following-sibling::div[1]',
            'screen':    '//div[normalize-space(text())="Display Size"]/following-sibling::div[1]',
            'year':      '//div[normalize-space(text())="Launch Year:" or normalize-space(text())="Launch Year"]/following-sibling::div[1]',
            'power':     '//div[normalize-space(text())="Power Consumption" or normalize-space(text())="Annual Energy Consumption" or normalize-space(text())="Energy Consumption"]/following-sibling::div[1]',
            'delivery':  '//div[normalize-space(text())="Delivery"]/parent::div',
            'trade':     '(//div[normalize-space(text())="Exchange offer"])[1]/following::div[(contains(text(),"Up to") or contains(text(),"₹") or contains(text(),"Off")) and not(contains(text(),"Pincode")) and not(contains(text(),"Servicea"))][1]',
            'rating':    '(//a[contains(@href,"ratings-reviews-details-page")]//div[@dir="auto"])[1]',
            'ratings':   '(//a[contains(@href,"ratings-reviews-details-page")]//div[@dir="auto"])[2]',
            'similar':   '//a[contains(@href,"hl_lid=")]//div[contains(@style,"text-overflow: ellipsis") and (contains(text(),"GB") or contains(text(),"TB")) and not(contains(text(),"₹"))]',
            'capacity':  '//div[normalize-space(text())="Capacity:" or normalize-space(text())="Capacity"]/following-sibling::div[1]',
            'type':      '//div[normalize-space(text())="Refrigerator Type:" or normalize-space(text())="Refrigerator Type" or normalize-space(text())="Function Type:" or normalize-space(text())="Function Type"]/following-sibling::div[1]',
        }
        if line in SHORTCUTS:
            xp = SHORTCUTS[line]
            print(f'  [shortcut={line}] xpath={xp}')
            if line == 'similar':
                # multi 결과
                els = find_elements_safe(driver, xp)
                print(f'  count={len(els)}')
                for i, e in enumerate(els):
                    t = get_text(e)
                    if len(t) > 150:
                        t = t[:150] + '...'
                    print(f'  [{i}] {t!r}')
            else:
                show_xpath(driver, xp)
            continue
        if line.startswith('click '):
            xp = line[6:].strip()
            robust_click(driver, xp)
            time.sleep(0.5)
            continue
        if line.startswith('attr '):
            rest = line[5:].strip()
            # attr <xpath> [attr_name]  — split on last space if attr_name 가 짧으면
            parts = rest.rsplit(' ', 1)
            if len(parts) == 2 and len(parts[1]) < 30 and ' ' not in parts[1]:
                xp, attr = parts
            else:
                xp, attr = rest, 'href'
            show_attr(driver, xp, attr)
            continue
        if line.startswith('multi '):
            xp = line[6:].strip()
            els = find_elements_safe(driver, xp)
            print(f'  count={len(els)}')
            for i, e in enumerate(els):
                t = get_text(e)
                if len(t) > 150:
                    t = t[:150] + '...'
                print(f'  [{i}] {t!r}')
            continue
        if line.startswith('source '):
            path = line[7:].strip()
            try:
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(driver.page_source or '')
                print(f'  [source] saved: {path}')
            except Exception as e:
                print(f'  [source] FAIL: {e}')
            continue
        # default: treat as xpath
        show_xpath(driver, line)


def main():
    if len(sys.argv) < 2:
        print('usage: python inspect_xpath.py <URL> [--headless]')
        return 2
    url = sys.argv[1]
    headless = '--headless' in sys.argv

    driver = make_driver(headless=headless)
    try:
        print(f'[repl] navigating to {url}')
        driver.get(url)
        time.sleep(3)
        repl(driver, url)
    finally:
        try:
            driver.quit()
        except Exception:
            pass


if __name__ == '__main__':
    sys.exit(main())
