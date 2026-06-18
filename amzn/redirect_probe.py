r"""Probe Amazon redirect cases from a SIEL run JSONL.

This script does not write to DB. It reads redirect=true detail records,
loads the landing URL in Chrome, compares listing metadata with the landing
page, and writes a CSV for manual review / rule calibration.

Usage:
  python amzn\redirect_probe.py --jsonl amzn\logs\siel_amazon_hhp_run_20260618013018.jsonl --product hhp
  python amzn\redirect_probe.py --jsonl amzn\logs\run.jsonl --product hhp --limit 3 --pause --save-html
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.by import By

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from amzn import detail as D  # noqa: E402


_SPACE_RE = re.compile(r'\s+')
_PUNCT_RE = re.compile(r'[^a-z0-9+ ]+')
_RAM_STORAGE_RE = re.compile(r'\b(\d+)\s*gb\s*\+\s*(\d+)\s*gb\b', re.I)
_RAM_RE = re.compile(r'\b(\d+)\s*gb\s*(?:ram|memory)\b', re.I)
_STORAGE_RE = re.compile(r'\b(\d+)\s*(gb|tb)\b', re.I)
_ASIN_RE = re.compile(r'/(?:dp|gp/product)/([A-Z0-9]{10})', re.I)


def normalize_text(value: str | None) -> str:
    text = (value or '').lower()
    text = text.replace('\u2019', "'").replace('\u2033', '"')
    text = _PUNCT_RE.sub(' ', text)
    return _SPACE_RE.sub(' ', text).strip()


def name_similarity(left: str | None, right: str | None) -> float:
    a = normalize_text(left)
    b = normalize_text(right)
    if not a or not b:
        return 0.0
    seq = SequenceMatcher(None, a, b).ratio()
    ta = set(a.split())
    tb = set(b.split())
    jaccard = len(ta & tb) / len(ta | tb) if ta and tb else 0.0
    return round((seq * 0.65) + (jaccard * 0.35), 4)


def asin_from_url(url: str | None) -> str | None:
    match = _ASIN_RE.search(url or '')
    return match.group(1).upper() if match else None


def extract_hhp_specs(text: str | None) -> dict:
    value = text or ''
    specs = {'ram_gb': '', 'storage': '', 'color': ''}
    pair = _RAM_STORAGE_RE.search(value)
    if pair:
        specs['ram_gb'] = f'{pair.group(1)}GB'
        specs['storage'] = f'{pair.group(2)}GB'
    else:
        ram = _RAM_RE.search(value)
        if ram:
            specs['ram_gb'] = f'{ram.group(1)}GB'
        storage_hits = _STORAGE_RE.findall(value)
        if storage_hits:
            num, unit = storage_hits[-1]
            specs['storage'] = f'{num}{unit.upper()}'

    # Conservative color extraction for common Amazon HHP title patterns.
    paren = re.findall(r'\(([^)]{3,80})\)', value)
    if paren:
        first = paren[0]
        color_part = re.split(r'\d+\s*gb|\d+\s*tb|\|', first, flags=re.I)[0]
        specs['color'] = _SPACE_RE.sub(' ', color_part).strip(' ,-')
    else:
        pipe_parts = [p.strip() for p in value.split('|')]
        if len(pipe_parts) >= 2:
            maybe_color = pipe_parts[1]
            if not re.search(r'\d+\s*(gb|tb)|processor|battery|camera|display', maybe_color, re.I):
                specs['color'] = maybe_color[:60].strip()
    return specs


def spec_conflicts(left: dict, right: dict) -> list[str]:
    conflicts = []
    for key in ('ram_gb', 'storage', 'color'):
        a = normalize_text(left.get(key))
        b = normalize_text(right.get(key))
        if a and b and a != b:
            conflicts.append(f'{key}:{left.get(key)}!={right.get(key)}')
    return conflicts


def read_jsonl(path: Path) -> tuple[dict, dict, list[dict]]:
    listing_by_asin: dict[str, dict] = {}
    detail_by_asin: dict[str, dict] = {}
    redirects: list[dict] = []
    with path.open('r', encoding='utf-8') as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            stage = rec.get('stage')
            asin = rec.get('asin') or asin_from_url(rec.get('product_url') or rec.get('source_url'))
            if not asin:
                continue
            if stage in ('main', 'bsr'):
                # Prefer main over bsr as listing source when both exist.
                old = listing_by_asin.get(asin)
                if old is None or (old.get('stage') != 'main' and stage == 'main'):
                    listing_by_asin[asin] = rec
            elif stage == 'detail':
                detail_by_asin[asin] = rec
                if rec.get('redirect') is True:
                    redirects.append(rec)
    return listing_by_asin, detail_by_asin, redirects


def safe_text(driver, xpath: str) -> str:
    try:
        el = driver.find_element(By.XPATH, xpath)
        return (el.text or el.get_attribute('textContent') or '').strip()
    except Exception:
        return ''


def safe_attr(driver, xpath: str, attr: str) -> str:
    try:
        return (driver.find_element(By.XPATH, xpath).get_attribute(attr) or '').strip()
    except Exception:
        return ''


def landing_snapshot(driver) -> dict:
    title = safe_text(driver, '//*[@id="productTitle"]')
    if not title:
        title = safe_attr(driver, '//meta[@name="title" or @property="og:title"]', 'content')
    price = (
        safe_text(driver, '//*[@id="corePriceDisplay_desktop_feature_div"]//*[contains(@class,"a-offscreen")][1]')
        or safe_text(driver, '//*[contains(@class,"apexPriceToPay")]//*[contains(@class,"a-offscreen")][1]')
    )
    star = (
        safe_attr(driver, '//*[@id="acrPopover"]', 'title')
        or safe_text(driver, '//*[@id="acrPopover"]//span[contains(@class,"a-icon-alt")]')
    )
    rating_count = safe_text(driver, '//*[@id="acrCustomerReviewText"]')
    specs = driver.execute_script(
        """
        const out = {};
        const clean = s => (s || '').replace(/\\s+/g, ' ').trim();
        document.querySelectorAll('#productOverview_feature_div tr').forEach(tr => {
          const cells = tr.querySelectorAll('td,th');
          if (cells.length >= 2) out[clean(cells[0].innerText)] = clean(cells[1].innerText);
        });
        document.querySelectorAll('#productDetails_expanderTables_depthLeftSections tr, #productDetails_techSpec_section_1 tr').forEach(tr => {
          const th = tr.querySelector('th');
          const td = tr.querySelector('td');
          if (th && td) out[clean(th.innerText)] = clean(td.innerText);
        });
        return out;
        """
    ) or {}
    variants = driver.execute_script(
        """
        const rows = [];
        const seen = new Set();
        const clean = s => (s || '').replace(/\\s+/g, ' ').trim();
        document.querySelectorAll('#twister_feature_div a[href*="/dp/"], #variation_color_name a[href*="/dp/"], #variation_size_name a[href*="/dp/"], #variation_style_name a[href*="/dp/"]').forEach(a => {
          const href = a.href || '';
          const m = href.match(/\\/(?:dp|gp\\/product)\\/([A-Z0-9]{10})/i);
          if (!m) return;
          const asin = m[1].toUpperCase();
          if (seen.has(asin)) return;
          seen.add(asin);
          rows.push({asin, href, label: clean(a.getAttribute('title') || a.getAttribute('aria-label') || a.innerText)});
        });
        return rows;
        """
    ) or []
    return {
        'landing_title': title,
        'landing_price': price,
        'landing_star': star,
        'landing_rating_count': rating_count,
        'landing_specs': specs,
        'variant_asins': ';'.join(v.get('asin', '') for v in variants),
        'variant_labels': ' || '.join(f"{v.get('asin')}:{v.get('label')}" for v in variants[:20]),
    }


def decide(product: str, listing: dict, redirect: dict, snap: dict) -> tuple[str, str, float]:
    listing_name = listing.get('retailer_sku_name') or ''
    landing_name = snap.get('landing_title') or ''
    sim = name_similarity(listing_name, landing_name)
    variant_asins = set((snap.get('variant_asins') or '').split(';'))
    original_asin = redirect.get('asin') or ''

    listing_specs = extract_hhp_specs(listing_name) if product == 'hhp' else {}
    landing_specs = extract_hhp_specs(landing_name) if product == 'hhp' else {}
    conflicts = spec_conflicts(listing_specs, landing_specs)

    if original_asin and original_asin in variant_asins:
        return 'variant_redirect_original_found', 'original_asin_found_in_variant_links', sim
    if conflicts:
        return 'redirect_unknown', 'spec_conflict:' + '|'.join(conflicts), sim
    if sim >= 0.88:
        return 'same_product_redirect', 'high_name_similarity_no_spec_conflict', sim
    if sim < 0.55:
        return 'different_product_original_not_found', 'low_name_similarity_no_variant_match', sim
    return 'redirect_unknown', 'medium_name_similarity_needs_review', sim


def run(args) -> int:
    jsonl_path = Path(args.jsonl)
    listing_by_asin, _detail_by_asin, redirects = read_jsonl(jsonl_path)
    if args.limit:
        redirects = redirects[:args.limit]
    out_dir = Path(args.out_dir) if args.out_dir else jsonl_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d%H%M%S')
    out_csv = Path(args.output) if args.output else out_dir / f'redirect_probe_{args.product}_{stamp}.csv'
    html_dir = out_dir / f'redirect_probe_html_{stamp}'
    if args.save_html:
        html_dir.mkdir(parents=True, exist_ok=True)

    driver = D.make_driver(headless=args.headless)
    rows = []
    try:
        for idx, rec in enumerate(redirects, start=1):
            original_asin = rec.get('asin') or ''
            landing_url = rec.get('landing_url') or rec.get('source_url') or ''
            listing = listing_by_asin.get(original_asin, {})
            print(f'[{idx}/{len(redirects)}] original={original_asin} landing={landing_url}')
            try:
                driver.get(landing_url)
                time.sleep(args.wait)
                D.check_and_recover_sorry(driver, landing_url)
                D.check_and_recover_continue_shopping(driver, landing_url)
                time.sleep(1)
                snap = landing_snapshot(driver)
                if args.save_html:
                    html_path = html_dir / f'{idx:02d}_{original_asin}_{rec.get("landing_asin") or "landing"}.html'
                    html_path.write_text(driver.page_source, encoding='utf-8')
                decision, reason, sim = decide(args.product, listing, rec, snap)
                listing_specs = extract_hhp_specs(listing.get('retailer_sku_name') or '') if args.product == 'hhp' else {}
                landing_specs = extract_hhp_specs(snap.get('landing_title') or '') if args.product == 'hhp' else {}
                rows.append({
                    'original_asin': original_asin,
                    'original_url': rec.get('source_url') or '',
                    'landing_asin': rec.get('landing_asin') or asin_from_url(driver.current_url),
                    'landing_url_recorded': landing_url,
                    'landing_url_current': driver.current_url,
                    'listing_stage': listing.get('stage') or '',
                    'listing_rank': listing.get('main_rank') or listing.get('bsr_rank') or '',
                    'listing_name': listing.get('retailer_sku_name') or '',
                    'landing_name': snap.get('landing_title') or '',
                    'name_similarity': sim,
                    'listing_price': listing.get('final_sku_price') or '',
                    'landing_price': snap.get('landing_price') or '',
                    'listing_star': listing.get('star_rating') or '',
                    'landing_star': snap.get('landing_star') or '',
                    'listing_rating_count': listing.get('count_of_star_ratings') or '',
                    'landing_rating_count': snap.get('landing_rating_count') or '',
                    'listing_ram_gb': listing_specs.get('ram_gb', ''),
                    'landing_ram_gb': landing_specs.get('ram_gb', ''),
                    'listing_storage': listing_specs.get('storage', ''),
                    'landing_storage': landing_specs.get('storage', ''),
                    'listing_color': listing_specs.get('color', ''),
                    'landing_color': landing_specs.get('color', ''),
                    'variant_asins': snap.get('variant_asins') or '',
                    'variant_labels': snap.get('variant_labels') or '',
                    'decision': decision,
                    'reason': reason,
                    'manual_review': 'yes' if decision == 'redirect_unknown' else 'no',
                    'error': '',
                })
                print(f'  decision={decision} sim={sim} reason={reason}')
                if args.pause:
                    input('  press Enter for next...')
            except Exception as exc:
                rows.append({
                    'original_asin': original_asin,
                    'original_url': rec.get('source_url') or '',
                    'landing_asin': rec.get('landing_asin') or '',
                    'landing_url_recorded': landing_url,
                    'decision': 'probe_error',
                    'reason': '',
                    'manual_review': 'yes',
                    'error': f'{type(exc).__name__}: {exc}',
                })
                print(f'  error={type(exc).__name__}: {exc}')
    finally:
        try:
            driver.quit()
        except WebDriverException:
            pass

    fieldnames = [
        'original_asin', 'original_url', 'landing_asin', 'landing_url_recorded',
        'landing_url_current', 'listing_stage', 'listing_rank', 'listing_name',
        'landing_name', 'name_similarity', 'listing_price', 'landing_price',
        'listing_star', 'landing_star', 'listing_rating_count', 'landing_rating_count',
        'listing_ram_gb', 'landing_ram_gb', 'listing_storage', 'landing_storage',
        'listing_color', 'landing_color', 'variant_asins', 'variant_labels',
        'decision', 'reason', 'manual_review', 'error',
    ]
    with out_csv.open('w', newline='', encoding='utf-8-sig') as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)
    print(f'output={out_csv}')
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description='Amazon redirect decision probe')
    parser.add_argument('--jsonl', required=True)
    parser.add_argument('--product', required=True, choices=['hhp', 'tv', 'ref', 'ldy'])
    parser.add_argument('--limit', type=int, default=0)
    parser.add_argument('--wait', type=float, default=4.0)
    parser.add_argument('--headless', action='store_true')
    parser.add_argument('--pause', action='store_true')
    parser.add_argument('--save-html', action='store_true')
    parser.add_argument('--out-dir')
    parser.add_argument('--output')
    return run(parser.parse_args())


if __name__ == '__main__':
    raise SystemExit(main())
