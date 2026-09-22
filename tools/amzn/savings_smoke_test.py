"""Check Amazon detail savings for three supplied URLs and three list items.

Run on the RDP machine. The browser is visible; PostgreSQL is queried only for
selectors in a read-only session. No results are inserted into the database.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from amzn import detail, listing
import insert_test_retail_com as rows
from tools.amzn.discount_type_smoke_test import load_selectors_read_only


SAMPLES = {
    'tv': ('https://www.amazon.in/inches-Spectra-Ready-Android-VW32AQ3/dp/B0GX5BF141', '-50%'),
    'ref': ('https://www.amazon.in/Samsung-Direct-Cool-Refrigerator-RR20H28249U-NL/dp/B0G8JR8VNZ', '-18%'),
    'ldy': ('https://www.amazon.in/VW-Automatic-Washing-Machine-AquaSpin0075P/dp/B0DV43D663', '-61%'),
}


def list_candidate(driver, product: str, selectors: dict, excluded_asin: str):
    container_xpath = (selectors.get('base_container') or {}).get('xpath')
    if not container_xpath:
        raise RuntimeError(f'{product}: main base_container selector missing')
    for page in (1, 2):
        url = listing.MAIN_URL_TEMPLATES[product].format(page=page)
        print(f'{product} list page {page}: {url}', flush=True)
        driver.get(url)
        time.sleep(3)
        listing.scroll_to_bottom(driver, pause=0.8, max_scrolls=8)
        for card in driver.find_elements(listing.By.XPATH, container_xpath):
            try:
                record = listing.extract_card(card, selectors, collect_quantity=False)
            except listing.WebDriverException:
                continue
            asin = record.get('asin')
            final = rows.amazon_price_amount(record.get('final_sku_price'))
            original = rows.amazon_price_amount(record.get('original_sku_price'))
            if not asin or asin == excluded_asin or final is None or original is None:
                continue
            if original <= final:
                continue
            record.update({'account_name': 'amazon', 'product': product, 'stage': 'main'})
            return f'https://www.amazon.in/dp/{asin}', record
    raise RuntimeError(f'{product}: discounted list item not found on the first two pages')


def check_detail(driver, product: str, source: str, url: str, listing_record: dict,
                 selectors: dict, expected: str | None):
    result = detail.crawl_detail(driver, product, url, selectors,
                                 detail.make_batch_id(product),
                                 listing_rec=listing_record)
    if result.get('_error') or result.get('_detail_skip'):
        raise RuntimeError(f'{product} {source}: detail skipped: '
                           f"{result.get('_error') or result.get('_detail_skip')}")
    raw = result.get('savings')
    merged = rows.make_row(listing_record, None, result) or {}
    stored = merged.get('savings')
    valid = rows.amazon_displayed_savings(raw) == raw and stored == raw
    if expected is not None:
        valid = valid and raw == expected
    print(f'{product} {source} ASIN={result.get("asin")} '
          f'page={raw or "NULL"} retail_com={stored or "NULL"} '
          f'expected={expected or "displayed percentage"} '
          f'{"PASS" if valid else "FAIL"}', flush=True)
    return valid


def main() -> int:
    driver = None
    failures = []
    try:
        driver = listing.make_driver(headless=False)
        for product, (sample_url, expected) in SAMPLES.items():
            try:
                detail_selectors = load_selectors_read_only(
                    detail.db_connect, 'detail', product)
                if 'savings' not in detail_selectors:
                    raise RuntimeError(f'{product}: detail savings selector missing')
                main_selectors = load_selectors_read_only(
                    listing.db_connect, 'main', product)
                sample_asin = detail.asin_from_url(sample_url)
                sample_record = {'account_name': 'amazon', 'product': product,
                                 'asin': sample_asin, 'product_url': sample_url}
                if not check_detail(driver, product, 'given URL', sample_url,
                                    sample_record, detail_selectors, expected):
                    failures.append(f'{product} given URL')
                list_url, list_record = list_candidate(
                    driver, product, main_selectors, sample_asin)
                if not check_detail(driver, product, 'list item', list_url,
                                    list_record, detail_selectors, None):
                    failures.append(f'{product} list item')
            except Exception as exc:
                failures.append(f'{product}: {type(exc).__name__}: {exc}')
                print(f'{product} ERROR {type(exc).__name__}: {exc}', flush=True)
    finally:
        if driver is not None:
            driver.quit()
    print('RESULT ' + ('PASS' if not failures else 'FAIL: ' + '; '.join(failures)),
          flush=True)
    return 0 if not failures else 1


if __name__ == '__main__':
    sys.exit(main())
