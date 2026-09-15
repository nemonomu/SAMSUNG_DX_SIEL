r"""Amazon discount_type RDP 실페이지 점검 도구.

목적
  - 화면에 표시되는 Chrome으로 Amazon main/detail 페이지를 직접 확인한다.
  - DB selector 원본값, 코드 필터 결과, 최종 병합값을 함께 보여준다.
  - PostgreSQL은 read-only 세션으로 selector SELECT만 수행한다.
  - DB INSERT/UPDATE/DELETE 및 selector SQL 적용은 절대 수행하지 않는다.

기본 실행
  python tools\amzn\discount_type_smoke_test.py

제품 하나만 실행
  python tools\amzn\discount_type_smoke_test.py --products tv

확인 후 직접 닫기
  python tools\amzn\discount_type_smoke_test.py --products tv \
    --main-limit 100 --detail-limit 10 --max-pages 7 --keep-browser-open

기본 범위
  - 제품군: tv, ref, ldy
  - 메인: 제품별 최대 30개
  - 상세: 제품별 최대 5개
  - Chrome: 화면 표시

결과
  test_output\amzn_discount_type_YYYYMMDD_HHMMSS\results.csv
  test_output\amzn_discount_type_YYYYMMDD_HHMMSS\summary.txt
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import siel_log


PRODUCT_LABELS = {
    'hhp': '휴대폰',
    'tv': 'TV',
    'ref': '냉장고',
    'ldy': '세탁기',
}
CSV_COLUMNS = [
    '제품군',
    '단계',
    '순번',
    'ASIN',
    '상품URL',
    '원본_discount_type',
    '필터후_discount_type',
    '최종병합_discount_type',
    '판정',
]


class Reporter:
    """콘솔과 UTF-8 텍스트 파일에 같은 내용을 기록한다."""

    def __init__(self, path: Path):
        self._file = path.open('w', encoding='utf-8-sig', newline='')

    def line(self, text: str = '') -> None:
        print(text, flush=True)
        self._file.write(text + '\n')
        self._file.flush()

    def close(self) -> None:
        self._file.close()


def display_value(value) -> str:
    return str(value) if value not in (None, '') else 'NULL'


def classify(raw_value, filtered_value) -> tuple[str, str]:
    """화면 표시용 판정 코드와 한국어 설명을 반환한다."""
    expected = siel_log.parse_amzn_discount_type(raw_value)
    if filtered_value != expected:
        return 'fail', '실패 - 코드 필터 결과 불일치'
    if filtered_value:
        return 'allowed', '정상 - 허용값 유지'
    if not raw_value:
        return 'empty', '정상 - 할인유형 없음'
    lowered = str(raw_value).casefold()
    if 'coupon' in lowered or lowered.startswith('you pay'):
        return 'coupon_rejected', '정상 - 쿠폰 문구 제외'
    return 'other_rejected', '정상 - 기타 프로모션 제외'


def final_value_is_valid(value) -> bool:
    if not value:
        return True
    return siel_log.parse_amzn_discount_type(value) == value


def listing_key(record: dict) -> str:
    return str(record.get('asin') or record.get('product_url') or '')


def load_selectors_read_only(db_connect, page_type: str, product: str) -> dict:
    """DB를 read-only로 고정하고 selector SELECT 한 번만 수행한다."""
    import psycopg2.extras

    sql = """
        SELECT data_field, xpath_primary, fallback_xpath
          FROM dx_siel_xpath_selectors
         WHERE site_account = %s
           AND page_type = %s
           AND domain = %s
           AND is_active = TRUE
    """
    conn = db_connect()
    try:
        # 실수로 DML 코드가 추가되더라도 PostgreSQL이 거부하도록 세션 자체를
        # read-only로 설정한다. 이 도구에서는 아래 SELECT 외 SQL을 실행하지 않는다.
        conn.set_session(readonly=True, autocommit=True)
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cursor:
            cursor.execute(sql, ('Amazon', page_type, product))
            rows = cursor.fetchall()
    finally:
        conn.close()
    return {
        row['data_field']: {
            'xpath': row['xpath_primary'],
            'fallback': row['fallback_xpath'],
        }
        for row in rows
    }


def raw_listing_discount(listing, card, selectors: dict):
    selector = selectors.get('discount_type') or {}
    return listing.first_text(
        card,
        selector.get('xpath'),
        selector.get('fallback'),
    )


def raw_detail_discount(detail, driver, selectors: dict):
    selector = selectors.get('discount_type') or {}
    xpath = selector.get('xpath')
    fallback = selector.get('fallback')
    value = detail.extract_single(driver, xpath) if xpath else None
    if value is None and fallback:
        value = detail.extract_single(driver, fallback)
    return value


def emit_observation(
        reporter: Reporter,
        stage_label: str,
        index: int,
        asin: str,
        raw_value,
        filtered_value,
        verdict: str,
        final_value=None) -> None:
    reporter.line()
    reporter.line(f'[{stage_label} {index:02d}] ASIN={asin or "확인 불가"}')
    reporter.line(f'  원본값       : {display_value(raw_value)}')
    reporter.line(f'  필터 결과    : {display_value(filtered_value)}')
    if stage_label == '상세':
        reporter.line(f'  최종 병합값  : {display_value(final_value)}')
    reporter.line(f'  판정         : {verdict}')


def main_records(
        driver,
        listing,
        product: str,
        selectors: dict,
        limit: int,
        max_pages: int,
        wait_seconds: float,
        reporter: Reporter,
        csv_rows: list[dict],
        counters: Counter) -> list[dict]:
    container_xpath = (selectors.get('base_container') or {}).get('xpath')
    if not container_xpath:
        raise RuntimeError('메인 base_container 셀렉터가 DB에 없습니다.')
    if 'discount_type' not in selectors:
        raise RuntimeError('메인 discount_type 셀렉터가 DB에 없습니다.')

    collected: list[dict] = []
    seen: set[str] = set()
    template = listing.MAIN_URL_TEMPLATES[product]
    for page_no in range(1, max_pages + 1):
        if len(collected) >= limit:
            break
        url = template.format(page=page_no)
        reporter.line()
        reporter.line(f'[진행] {PRODUCT_LABELS[product]} 메인 {page_no}페이지 접속')
        driver.get(url)
        time.sleep(wait_seconds)
        listing.scroll_to_bottom(driver, pause=0.8, max_scrolls=8)
        cards = driver.find_elements(listing.By.XPATH, container_xpath)
        reporter.line(f'[진행] 메인 {page_no}페이지 상품카드 {len(cards)}개 발견')

        for card in cards:
            if len(collected) >= limit:
                break
            raw_value = raw_listing_discount(listing, card, selectors)
            record = listing.extract_card(card, selectors, collect_quantity=False)
            key = listing_key(record)
            if not key or key in seen:
                continue
            seen.add(key)
            record.update({
                'account_name': 'Amazon',
                'product': product,
                'stage': 'main',
                'main_rank': len(collected) + 1,
            })
            filtered_value = record.get('discount_type')
            status, verdict = classify(raw_value, filtered_value)
            counters[status] += 1
            counters['main_checked'] += 1
            index = len(collected) + 1
            observation = {
                'record': record,
                'raw_value': raw_value,
                'filtered_value': filtered_value,
                'index': index,
            }
            collected.append(observation)
            csv_rows.append({
                '제품군': product,
                '단계': '메인',
                '순번': index,
                'ASIN': record.get('asin'),
                '상품URL': record.get('product_url'),
                '원본_discount_type': raw_value,
                '필터후_discount_type': filtered_value,
                '최종병합_discount_type': '',
                '판정': verdict,
            })
            # 할인 관련 원본이 있는 카드와 실패만 자세히 표시한다. 빈 카드도 CSV에는 남긴다.
            if raw_value or status == 'fail':
                emit_observation(
                    reporter,
                    '메인',
                    index,
                    str(record.get('asin') or ''),
                    raw_value,
                    filtered_value,
                    verdict,
                )
    return collected


def prioritized_detail_candidates(records: list[dict], limit: int) -> list[dict]:
    """허용 deal, 제외 대상 promotion, 빈값 순으로 detail 후보를 고른다."""
    def priority(item: dict) -> tuple[int, int]:
        if item.get('filtered_value'):
            return 0, item['index']
        if item.get('raw_value'):
            return 1, item['index']
        return 2, item['index']

    candidates = [item for item in records if item['record'].get('product_url')]
    return sorted(candidates, key=priority)[:limit]


def detail_records(
        driver,
        detail,
        insert_rows,
        product: str,
        selectors: dict,
        candidates: list[dict],
        wait_seconds: float,
        reporter: Reporter,
        csv_rows: list[dict],
        counters: Counter) -> None:
    if 'discount_type' not in selectors:
        raise RuntimeError('상세 discount_type 셀렉터가 DB에 없습니다.')

    for detail_index, candidate in enumerate(candidates, start=1):
        listing_record = candidate['record']
        url = listing_record['product_url']
        asin = str(listing_record.get('asin') or '')
        reporter.line()
        reporter.line(
            f'[진행] {PRODUCT_LABELS[product]} 상세 '
            f'{detail_index}/{len(candidates)} 접속: {asin or url}'
        )
        driver.get(url)
        time.sleep(wait_seconds)
        raw_value = raw_detail_discount(detail, driver, selectors)
        filtered_value = siel_log.parse_amzn_discount_type(raw_value)
        detail_record = {'discount_type': filtered_value}
        merged = insert_rows.make_row(listing_record, None, detail_record) or {}
        final_value = merged.get('discount_type')
        status, verdict = classify(raw_value, filtered_value)
        if not final_value_is_valid(final_value):
            status = 'fail'
            verdict = '실패 - 최종 병합값에 허용되지 않은 값 존재'
        counters[status] += 1
        counters['detail_checked'] += 1
        counters['final_invalid'] += int(not final_value_is_valid(final_value))
        csv_rows.append({
            '제품군': product,
            '단계': '상세',
            '순번': detail_index,
            'ASIN': asin,
            '상품URL': url,
            '원본_discount_type': raw_value,
            '필터후_discount_type': filtered_value,
            '최종병합_discount_type': final_value,
            '판정': verdict,
        })
        emit_observation(
            reporter,
            '상세',
            detail_index,
            asin,
            raw_value,
            filtered_value,
            verdict,
            final_value=final_value,
        )


def write_csv(path: Path, rows: list[dict]) -> None:
    with path.open('w', encoding='utf-8-sig', newline='') as file:
        writer = csv.DictWriter(file, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def parse_args():
    parser = argparse.ArgumentParser(
        description='Amazon discount_type RDP 실페이지 테스트 (DB 저장 없음)',
    )
    parser.add_argument(
        '--products', nargs='+', default=['tv', 'ref', 'ldy'],
        choices=['hhp', 'tv', 'ref', 'ldy'],
        help='확인할 제품군 (기본: tv ref ldy)',
    )
    parser.add_argument(
        '--main-limit', type=int, default=30,
        help='제품별 메인 최대 확인 개수 (기본: 30)',
    )
    parser.add_argument(
        '--detail-limit', type=int, default=5,
        help='제품별 상세 최대 확인 개수 (기본: 5)',
    )
    parser.add_argument(
        '--max-pages', type=int, default=2,
        help='제품별 메인 최대 페이지 수 (기본: 2)',
    )
    parser.add_argument(
        '--wait-seconds', type=float, default=3.0,
        help='페이지 접속 후 기본 대기시간 초 (기본: 3)',
    )
    parser.add_argument(
        '--headless', action='store_true',
        help='Chrome을 숨김 실행. 지정하지 않으면 RDP 화면에 표시',
    )
    parser.add_argument(
        '--keep-browser-open', action='store_true',
        help='테스트 종료 후 Enter를 누를 때까지 Chrome을 열어 둠',
    )
    return parser.parse_args()


def validate_args(args) -> None:
    if args.main_limit < 1:
        raise ValueError('--main-limit은 1 이상이어야 합니다.')
    if args.detail_limit < 0:
        raise ValueError('--detail-limit은 0 이상이어야 합니다.')
    if args.max_pages < 1:
        raise ValueError('--max-pages는 1 이상이어야 합니다.')
    if args.wait_seconds < 0:
        raise ValueError('--wait-seconds는 0 이상이어야 합니다.')


def main() -> int:
    args = parse_args()
    try:
        validate_args(args)
    except ValueError as exc:
        print(f'[입력 오류] {exc}', file=sys.stderr)
        return 2

    # 무거운 크롤러/DB 모듈은 --help가 DB 설정 없이도 동작하도록 실행 시점에 import한다.
    from amzn import detail, listing
    import insert_test_retail_com as insert_rows

    started = datetime.now()
    stamp = started.strftime('%Y%m%d_%H%M%S')
    output_dir = ROOT / 'test_output' / f'amzn_discount_type_{stamp}'
    output_dir.mkdir(parents=True, exist_ok=False)
    csv_path = output_dir / 'results.csv'
    summary_path = output_dir / 'summary.txt'
    reporter = Reporter(summary_path)
    counters: Counter = Counter()
    csv_rows: list[dict] = []
    errors: list[str] = []
    driver = None

    reporter.line('=' * 72)
    reporter.line(' Amazon 할인유형 RDP 실페이지 테스트')
    reporter.line(f' 제품군       : {", ".join(PRODUCT_LABELS[p] for p in args.products)}')
    reporter.line(f' 브라우저     : {"숨김" if args.headless else "화면 표시"}')
    reporter.line(
        ' 종료 후 유지 : '
        + ('사용' if args.keep_browser_open and not args.headless else '사용 안 함')
    )
    reporter.line(' DB 셀렉터    : 읽기 전용 조회')
    reporter.line(' DB 저장      : 사용 안 함')
    reporter.line(' SQL 적용     : 사용 안 함')
    reporter.line(f' 메인 확인    : 제품별 최대 {args.main_limit}개')
    reporter.line(f' 상세 확인    : 제품별 최대 {args.detail_limit}개')
    reporter.line('=' * 72)

    try:
        driver = listing.make_driver(headless=args.headless)
        for product in args.products:
            reporter.line()
            reporter.line('#' * 72)
            reporter.line(
                f'# 제품군 시작: {PRODUCT_LABELS[product]} ({product.upper()})'
            )
            reporter.line('#' * 72)
            try:
                main_selectors = load_selectors_read_only(
                    listing.db_connect, 'main', product)
                detail_selectors = load_selectors_read_only(
                    listing.db_connect, 'detail', product)
                fallback = (
                    main_selectors.get('discount_type') or {}
                ).get('fallback')
                reporter.line(
                    '[DB 확인] 메인 쿠폰 보조 XPath: '
                    + (display_value(fallback))
                )
                records = main_records(
                    driver,
                    listing,
                    product,
                    main_selectors,
                    args.main_limit,
                    args.max_pages,
                    args.wait_seconds,
                    reporter,
                    csv_rows,
                    counters,
                )
                candidates = prioritized_detail_candidates(
                    records, args.detail_limit)
                detail_records(
                    driver,
                    detail,
                    insert_rows,
                    product,
                    detail_selectors,
                    candidates,
                    args.wait_seconds,
                    reporter,
                    csv_rows,
                    counters,
                )
            except Exception as exc:
                message = (
                    f'{PRODUCT_LABELS[product]} 테스트 실패: '
                    f'{type(exc).__name__}: {exc}'
                )
                errors.append(message)
                counters['fail'] += 1
                reporter.line(f'[오류] {message}')
    except Exception as exc:
        message = f'테스트 실행 실패: {type(exc).__name__}: {exc}'
        errors.append(message)
        counters['fail'] += 1
        reporter.line(f'[오류] {message}')
    write_csv(csv_path, csv_rows)
    duration = datetime.now() - started
    reporter.line()
    reporter.line('-' * 72)
    reporter.line('최종 요약')
    reporter.line(f'  메인 확인             : {counters["main_checked"]}건')
    reporter.line(f'  상세 확인             : {counters["detail_checked"]}건')
    reporter.line(f'  정상 허용값           : {counters["allowed"]}건')
    reporter.line(f'  제외된 쿠폰           : {counters["coupon_rejected"]}건')
    reporter.line(f'  제외된 기타 프로모션 : {counters["other_rejected"]}건')
    reporter.line(f'  할인유형 없음         : {counters["empty"]}건')
    reporter.line(f'  실패                  : {counters["fail"]}건')
    reporter.line(f'  소요시간              : {str(duration).split(".", 1)[0]}')
    if not counters['allowed']:
        reporter.line('  주의: 이번 표본에서는 허용 할인유형이 발견되지 않았습니다.')
    if not counters['coupon_rejected']:
        reporter.line('  주의: 이번 표본에서는 쿠폰 문구가 발견되지 않았습니다.')
    if errors:
        reporter.line('  오류 목록:')
        for error in errors:
            reporter.line(f'    - {error}')
    passed = not errors and not counters['fail'] and not counters['final_invalid']
    reporter.line()
    reporter.line(f'최종 판정: {"통과" if passed else "실패"}')
    reporter.line('DB 저장: 수행하지 않음')
    reporter.line(f'결과 CSV: {csv_path}')
    reporter.line(f'요약 파일: {summary_path}')
    reporter.line('-' * 72)
    if driver is not None and args.keep_browser_open and not args.headless:
        reporter.line()
        reporter.line('Chrome을 열어 둔 상태입니다.')
        reporter.line('직접 확인을 마친 뒤 이 PowerShell 창에서 Enter를 누르면 종료합니다.')
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            pass
    if driver is not None:
        try:
            driver.quit()
        except Exception:
            pass
    reporter.close()
    return 0 if passed else 1


if __name__ == '__main__':
    sys.exit(main())
