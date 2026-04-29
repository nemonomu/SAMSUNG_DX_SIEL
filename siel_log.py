"""SIEL 크롤러 공통 로깅 helper.

사용:
  from siel_log import setup, save_html, log_selectors, log_record_summary
  HERE = os.path.dirname(os.path.abspath(__file__))
  logger, html_path = setup(ACCOUNT_NAME, product, stage, HERE)

base_dir 는 호출 module 의 디렉토리 (amzn/ 또는 fpkt/). 그 안에 logs/ 자동 생성.
파일명: siel_{account_name}_{product}_{stage}_{YYMMDDHHMM}.log / .html
시각: 인도 IST 기준.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))


def detect_chrome_major() -> int | None:
    """현재 Windows 에 설치된 Chrome major 버전 (int) 반환. 못 찾으면 None.
    undetected_chromedriver 의 version_main 인자로 사용 — driver 와 browser 버전 매칭.
    """
    try:
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(hive, r"Software\Google\Chrome\BLBeacon") as key:
                    version, _ = winreg.QueryValueEx(key, "version")
                m = re.match(r'(\d+)', version)
                if m:
                    return int(m.group(1))
            except OSError:
                continue
    except ImportError:
        pass
    for path in (
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ):
        try:
            out = subprocess.check_output([path, '--version'], timeout=5).decode()
            m = re.search(r'(\d+)\.', out)
            if m:
                return int(m.group(1))
        except Exception:
            continue
    return None

REVIEW_SEP = ' ||| '
REVIEW_PREFIX_FMT = 'review{n} - {text}'
SIMILAR_SEP = ', '

_PRICE_NUM_RE = re.compile(r'[-+]?\d+(?:\.\d+)?')
_INT_RE = re.compile(r'\d+')


def make_basename(account_name: str, product: str, stage: str) -> str:
    ts = datetime.now(IST).strftime('%y%m%d%H%M')
    return f'siel_{account_name}_{product}_{stage}_{ts}'


def setup(account_name: str, product: str, stage: str, base_dir: str):
    """logs 디렉토리 만들고 (logger, html_path) 반환."""
    logs_dir = os.path.join(base_dir, 'logs')
    os.makedirs(logs_dir, exist_ok=True)
    base = make_basename(account_name, product, stage)
    log_path = os.path.join(logs_dir, base + '.log')
    html_path = os.path.join(logs_dir, base + '.html')

    logger = logging.getLogger(f'siel.{account_name}.{product}.{stage}')
    logger.setLevel(logging.INFO)
    # 동일 logger 재사용 시 handler 중복 방지
    logger.handlers.clear()
    logger.propagate = False
    fmt = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')

    fh = logging.FileHandler(log_path, encoding='utf-8')
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    logger.info('=== siel crawler start: account=%s product=%s stage=%s ===',
                account_name, product, stage)
    logger.info('log_file=%s', log_path)
    logger.info('html_file=%s', html_path)
    return logger, html_path


def save_html(driver, html_path: str) -> bool:
    try:
        with open(html_path, 'w', encoding='utf-8') as f:
            f.write(driver.page_source or '')
        return True
    except Exception as e:
        try:
            with open(html_path + '.err', 'w', encoding='utf-8') as f:
                f.write(f'save_html failed: {type(e).__name__}: {e}\n')
        except Exception:
            pass
        return False


def log_selectors(logger, selectors: dict) -> None:
    logger.info('수집 대상 스키마 (selectors): %d 개', len(selectors))
    for field in sorted(selectors.keys()):
        sel = selectors[field]
        xp = sel.get('xpath') if isinstance(sel, dict) else sel
        fb = sel.get('fallback') if isinstance(sel, dict) else None
        logger.info('  - %s: xpath=%s%s', field, xp,
                    f' (fallback={fb})' if fb else '')


def _truncate(s, n: int = 80) -> str:
    if s is None:
        return ''
    s = str(s)
    return s if len(s) <= n else s[:n] + '...'


def parse_price(v):
    """price string ('₹79,999', 'M.R.P.: ₹96,999', '79,999.00') → float | None."""
    if v is None:
        return None
    s = str(v).replace(',', '')
    m = _PRICE_NUM_RE.search(s)
    if not m:
        return None
    try:
        return float(m.group())
    except ValueError:
        return None


def parse_int_field(v):
    """'2,391 Reviews', '39132 ratings' 등 → int | None."""
    if v is None:
        return None
    s = str(v).replace(',', '')
    m = _INT_RE.search(s)
    return int(m.group()) if m else None


def format_review_content(parts) -> str | None:
    """[review_text, ...] → 'review1 - X ||| review2 - Y ||| ...'"""
    if not parts:
        return None
    return REVIEW_SEP.join(REVIEW_PREFIX_FMT.format(n=i + 1, text=t)
                           for i, t in enumerate(parts))


def format_similar_names(parts) -> str | None:
    """[name, ...] → 'A, B, C'"""
    if not parts:
        return None
    return SIMILAR_SEP.join(parts)


def count_review_cards(v) -> int:
    """'review1 - X ||| review2 - Y' 같은 포맷에서 카드 수 카운트.
    단일/다중 모두 review{n} prefix 매칭으로 처리 (1개 케이스 누락 방지).
    """
    if v is None or v == '':
        return 0
    return len(re.findall(r'\breview\d+\s-\s', str(v)))


def count_similar_names(v) -> int:
    if v is None or v == '':
        return 0
    return str(v).count(SIMILAR_SEP) + 1


def warn_price_logic(logger, rec: dict) -> None:
    """final_sku_price > original_sku_price 면 warning. 논리적으로 불가능."""
    fp = rec.get('final_sku_price')
    op = rec.get('original_sku_price')
    fpv = parse_price(fp)
    opv = parse_price(op)
    if fpv is not None and opv is not None and fpv > opv:
        logger.warning(
            'price logic violation: final=%s (%.2f) > original=%s (%.2f) | url=%s',
            fp, fpv, op, opv, rec.get('source_url'))


_DEFAULT_EXCLUDE = {
    'account_name', 'product', 'stage', 'company', 'division',
    'source_url', 'batch_id', 'crawl_datetime', 'page_no',
}


def log_record_summary(logger, rec: dict, exclude=None) -> None:
    """한 record 의 추출된 값 요약 1줄.
    - detailed_review_content → detailed_review_content_card={n} 으로 표기
    - retailer_sku_name_similar → 카운트 + truncated value
    """
    skip = set(exclude) if exclude is not None else _DEFAULT_EXCLUDE
    rank_parts = []
    for k in ('main_rank', 'bsr_rank'):
        if k in rec:
            rank_parts.append(f"{k}={rec[k]}")
    parts = []
    for k, v in rec.items():
        if k in skip:
            continue
        if k == 'detailed_review_content':
            parts.append(f'detailed_review_content_card={count_review_cards(v)}')
        elif k == 'retailer_sku_name_similar':
            n = count_similar_names(v)
            parts.append(f'retailer_sku_name_similar_count={n}')
        else:
            parts.append(f"{k}={_truncate(v)}")
    head = ' '.join(rank_parts) + ' | ' if rank_parts else ''
    logger.info('record: %s%s', head, ' | '.join(parts) if parts else '(empty)')
