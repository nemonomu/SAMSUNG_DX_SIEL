"""NULL product (model_year/screen_size) 만 재navigate + spec label dump."""
import sys
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass
import os
import re
import json
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.common.exceptions import WebDriverException

import siel_log

uc.Chrome.__del__ = lambda self: None

NULL_FSNS = {
    'TVSHHQCRSRBAA7YT', 'TVSHHQCRR3MRSCGW', 'TVSHDACYFMHMXQEQ',
    'TVSHAVHG4F98JA2Y', 'TVSHKEW8MG2BPFNY',
    'TVSH3AB5YNHVBRBZ', 'TVSHBCC6SKFGHZNS',
}

urls = []
with open('tv24.jsonl', encoding='utf-8') as f:
    for l in f:
        try:
            r = json.loads(l)
        except Exception:
            continue
        if r.get('stage') == 'detail' and r.get('fsn') in NULL_FSNS:
            urls.append((r['fsn'], r.get('source_url')))

print(f'targets: {len(urls)}')

opts = uc.ChromeOptions()
opts.add_argument('--no-sandbox')
opts.add_argument('--disable-dev-shm-usage')
opts.add_argument('--window-size=1920,1080')
opts.add_argument('--lang=en-IN')
major = siel_log.detect_chrome_major()
kwargs = {'options': opts}
if major:
    kwargs['version_main'] = major
driver = uc.Chrome(**kwargs)

try:
    for fsn, url in urls:
        print(f'\n=== {fsn} ===')
        if not url:
            print('  no url')
            continue
        try:
            driver.get(url)
            time.sleep(3)
            try:
                el = driver.find_element(
                    By.XPATH,
                    '//div[normalize-space(text())="Specifications" and not(ancestor::head)]')
                driver.execute_script('arguments[0].scrollIntoView({block:"center"});', el)
                time.sleep(0.5)
                driver.execute_script('arguments[0].click();', el)
                time.sleep(1.5)
            except (WebDriverException, Exception):
                pass
            try:
                driver.execute_script('window.scrollTo(0, document.body.scrollHeight);')
                time.sleep(1.0)
            except Exception:
                pass
            html = driver.page_source
            labels = sorted(set(re.findall(
                r'<div[^>]*?>([A-Z][^<>]{2,80}?):</div>', html)))
            print(f'  spec_labels ({len(labels)}):')
            for l in labels:
                print(f'    {l!r}')
        except Exception as e:
            print(f'  ERROR: {type(e).__name__}: {e}')
finally:
    try:
        driver.quit()
    except Exception:
        pass
