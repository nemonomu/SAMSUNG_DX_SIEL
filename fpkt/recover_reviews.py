"""Recover four missing review fields from one existing Flipkart JSONL batch.

--plan: offline selection only. Default: crawl + append audit, no DB writes.
--apply: also fill NULL/blank cells in the existing retail_com/product_list rows.
Never INSERT rows, re-run listing/specs/prices, or overwrite populated cells.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fpkt.review_fields import FIELDS, fill_missing, missing, product_pid

PRODUCTS = ('tv', 'hhp', 'ref', 'ldy')


def required_fields(values):
    fields = [field for field in FIELDS if missing(values.get(field))]
    # Explicit zero reviews means an absent body is expected, not a failed crawl.
    if str(values.get('count_of_reviews')).strip() == '0':
        fields = [field for field in fields if field != 'detailed_review_content']
    return fields


def load_candidates(path, product, batch_id):
    groups = {}
    # utf-8-sig accepts both crawler output and PowerShell UTF-8 BOM output.
    with Path(path).open(encoding='utf-8-sig') as stream:
        for line_no, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError as exc:
                raise ValueError(f'Invalid JSONL at line {line_no}') from exc
            if not isinstance(row, dict):
                raise ValueError(f'Expected object at line {line_no}')
            if (str(row.get('account_name', '')).lower() != 'flipkart'
                    or row.get('product') != product or row.get('batch_id') != batch_id
                    or row.get('stage') not in ('main', 'bsr', 'detail')):
                continue
            url = (row.get('source_url') if row['stage'] == 'detail' else
                   row.get('product_url')) or row.get('product_url') or row.get('source_url') or ''
            pid = product_pid(url)
            if not pid or row.get('fsn') not in (None, '', pid):
                raise ValueError(f'Invalid or mismatched Flipkart PID at line {line_no}')
            group = groups.setdefault(pid, {})
            stage = row['stage']
            # First listing rank wins; repeated detail results may supply missing fields.
            if stage not in group:
                group[stage] = dict(row)
            else:
                fill_missing(group[stage], row)
    candidates = []
    for pid, group in groups.items():
        if 'detail' not in group:
            continue
        detail = group['detail']
        primary = group.get('main') or group.get('bsr') or {}
        values = {}
        for field in FIELDS:
            sources = (primary, detail) if field == 'count_of_reviews' else (detail, primary)
            values[field] = next((r.get(field) for r in sources if not missing(r.get(field))), None)
        pending = required_fields(values)
        if pending:
            candidates.append(dict(fsn=pid, source_url=detail.get('source_url') or
                                   detail.get('product_url'), values=values, missing=pending))
    return candidates, sum('detail' in group for group in groups.values())


def append_audit(stream, record):
    import os
    stream.write(json.dumps(record, ensure_ascii=False) + '\n')
    stream.flush()
    os.fsync(stream.fileno())


def read_resume(path, metadata):
    cache = {}
    with Path(path).open(encoding='utf-8') as stream:
        first = json.loads(next(stream))
        if first != metadata:
            raise ValueError('Resume file belongs to a different input, batch or product')
        for line in stream:
            try:
                row = json.loads(line)
            except ValueError as exc:
                raise ValueError('Incomplete audit line; preserve this file and use a new output') from exc
            if row.get('type') == 'collected':
                values = cache.setdefault(row['fsn'], {})
                fill_missing(values, row.get('values', {}))
    return cache


def db_connect():
    # Imported only on the RDP host when --apply is explicitly selected.
    import config
    import psycopg2
    cfg = dict(config.DB_CONFIG)
    cfg.setdefault('database', 'postgres')
    cfg.setdefault('connect_timeout', 15)
    cfg.setdefault('client_encoding', 'utf8')
    return psycopg2.connect(**cfg)


def apply_missing(conn, product, batch_id, pid, values, audit):
    """Per-product transaction; row locks live only during these small updates."""
    if product not in PRODUCTS:
        raise ValueError('Unsupported product')
    scope = (batch_id, pid, product.upper())
    matched, changed_cells = 0, 0
    with conn.cursor() as cur:
        cur.execute("SET LOCAL lock_timeout = '10s'")
        cur.execute("SET LOCAL statement_timeout = '30s'")
        for suffix, fields in [('retail_com', FIELDS), ('product_list', FIELDS[:3])]:
            table = f'dx_siel_{product}_{suffix}'
            where = ("batch_id = %s AND item = %s AND LOWER(account_name) = 'flipkart' "
                     "AND UPPER(country) = 'SIEL' AND UPPER(product) = %s")
            cur.execute(f"SELECT id, {', '.join(fields)} FROM {table} WHERE {where} FOR UPDATE", scope)
            for row in cur.fetchall():
                matched += 1
                before = dict(zip(fields, row[1:]))
                after = dict(before)
                changes = fill_missing(after, {f: values.get(f) for f in fields})
                if not changes:
                    continue
                # Durable intent before the write, then an explicit commit event in main.
                audit(dict(type='db_update_planned', table=table, id=row[0], fsn=pid,
                           before={f: before[f] for f in changes}, changes=changes))
                setters = ', '.join(f'{f} = %s' for f in changes)
                cur.execute(f'UPDATE {table} SET {setters} WHERE id = %s AND {where}',
                            (*changes.values(), row[0], *scope))
                if cur.rowcount != 1:
                    raise RuntimeError('Recovery row changed during locked update')
                changed_cells += len(changes)
    return matched, changed_cells


def check_db_scope(product, batch_id, pids):
    """Fail early on the wrong batch or schema, before starting Chrome."""
    conn = db_connect()
    try:
        with conn.cursor() as cur:
            for suffix, fields in [('retail_com', FIELDS), ('product_list', FIELDS[:3])]:
                table = f'dx_siel_{product}_{suffix}'
                cur.execute(f"SELECT id, {', '.join(fields)} FROM {table} "
                            "WHERE batch_id = %s AND item = ANY(%s) "
                            "AND LOWER(account_name) = 'flipkart' AND UPPER(country) = 'SIEL' "
                            "AND UPPER(product) = %s", (batch_id, pids, product.upper()))
                rows = cur.fetchall()
                print(f'DB scope: {table} matched_rows={len(rows)}', flush=True)
                if suffix == 'retail_com' and not rows:
                    raise ValueError('No existing retail rows for this batch; recovery never INSERTs rows')
    finally:
        conn.rollback()
        conn.close()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True, type=Path)
    parser.add_argument('--product', choices=PRODUCTS, required=True)
    parser.add_argument('--batch-id', required=True)
    parser.add_argument('--output', type=Path, help='Recovery journal JSONL (not input for INSERT)')
    parser.add_argument('--plan', action='store_true', help='Offline counts only; no DB/browser/files')
    parser.add_argument('--apply', action='store_true', help='Fill only NULL/blank DB cells in this batch')
    parser.add_argument('--resume', action='store_true', help='Reuse --output; retry remaining missing fields')
    parser.add_argument('--limit', type=int, default=0)
    parser.add_argument('--sleep', type=float, default=2)
    parser.add_argument('--headless', action='store_true')
    args = parser.parse_args(argv)
    if args.limit < 0 or args.sleep < 0 or not args.batch_id.strip():
        parser.error('limit/sleep must be nonnegative and batch-id nonempty')
    if args.resume and not args.output:
        parser.error('--resume requires --output')
    if args.plan and args.apply:
        parser.error('--plan and --apply cannot be combined')
    if args.output and args.output.resolve() == args.input.resolve():
        parser.error('Output must not overwrite the input JSONL')
    return args


def main(argv=None):
    args = parse_args(argv)
    candidates, detail_count = load_candidates(args.input, args.product, args.batch_id)
    planned = dict(batch_id=args.batch_id, product=args.product, detail_products=detail_count,
                   candidates=len(candidates), missing=dict(Counter(f for c in candidates for f in c['missing'])))
    print(json.dumps(planned), flush=True)
    if detail_count == 0:
        print('No matching detail records: check input/product/batch-id', file=sys.stderr)
        return 2
    if args.plan or not candidates:
        return 0
    if args.limit:
        candidates = candidates[:args.limit]
    if args.apply:
        check_db_scope(args.product, args.batch_id, [c['fsn'] for c in candidates])
    output = args.output or args.input.with_name(
        args.input.stem + '_review_recovery_' + datetime.now().strftime('%Y%m%d%H%M%S') + '.jsonl')
    metadata = dict(type='metadata', version=1, batch_id=args.batch_id, product=args.product,
                    input_sha256=hashlib.sha256(args.input.read_bytes()).hexdigest())
    cache = read_resume(output, metadata) if args.resume else {}
    output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation prevents accidental loss of an earlier run.
    with output.open('a' if args.resume else 'x', encoding='utf-8') as stream:
        audit = lambda row: append_audit(stream, row)
        if not args.resume:
            audit(metadata)
        logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
        from fpkt import detail
        detail._logger = logging.getLogger('review_recovery')
        selectors = detail.load_selectors('Flipkart', 'detail', args.product)
        if not (selectors.get('open_reviews_panel') or {}).get('xpath'):
            raise RuntimeError('Apply sql/manual/dx_siel_fpkt_review_panel.sql before recovery')
        if not (selectors.get('detailed_review_content') or {}).get('xpath'):
            raise RuntimeError('Missing detailed_review_content selector')
        driver = None
        unresolved = db_errors = updated = fetched = 0
        try:
            for index, candidate in enumerate(candidates, 1):
                pid = candidate['fsn']
                values = dict(candidate['values'])
                fill_missing(values, cache.get(pid, {}))
                pending = required_fields(values)
                error = None
                status = 'resumed'
                if pending:
                    try:
                        if driver is None:
                            driver = detail.make_driver(headless=args.headless)
                            driver.set_page_load_timeout(45)
                        collected = detail.crawl_review_fields(
                            driver, args.product, candidate['source_url'], selectors,
                            args.batch_id, need_body='detailed_review_content' in pending)
                        fill_missing(values, collected)
                        status = collected.get('_review_status', 'numeric_fields_collected')
                        error = collected.get('_error')
                        fetched += 1
                    except Exception as exc:
                        # Keep errors free of connection strings/credentials.
                        error = type(exc).__name__
                        if driver is not None:
                            try:
                                driver.quit()
                            except Exception:
                                pass
                        driver = None
                    if args.sleep:
                        time.sleep(args.sleep)
                pending = required_fields(values)
                unresolved += bool(pending)
                audit(dict(type='collected', fsn=pid, source_url=candidate['source_url'],
                           recovered_at=datetime.now(timezone.utc).isoformat(), values=values,
                           pending=pending, status=status, error=error))
                if args.apply:
                    conn = None
                    committed = False
                    try:
                        conn = db_connect()
                        matched, cells = apply_missing(conn, args.product, args.batch_id, pid, values, audit)
                        conn.commit()
                        committed = True
                        updated += cells
                        if not matched:
                            db_errors += 1
                        audit(dict(type='db_committed', fsn=pid, matched_rows=matched, updated_cells=cells))
                    except Exception as exc:
                        if committed:
                            raise RuntimeError(f'DB committed for {pid}, audit failed; resume is null-only safe') from exc
                        if conn is not None:
                            conn.rollback()
                        db_errors += 1
                        audit(dict(type='db_failed', fsn=pid, error=type(exc).__name__))
                    finally:
                        if conn is not None:
                            conn.close()
                print(f'[{index}/{len(candidates)}] {pid} pending={",".join(pending) or "none"} error={error or "none"}', flush=True)
        finally:
            if driver is not None:
                try:
                    driver.quit()
                except Exception:
                    pass
        summary = dict(type='summary', processed=len(candidates), fetched=fetched,
                       unresolved_products=unresolved, db_errors=db_errors,
                       updated_cells=updated, applied=args.apply)
        audit(summary)
        print(json.dumps(summary), flush=True)
        print(f'Audit: {output}', flush=True)
    return 2 if unresolved or db_errors else 0


if __name__ == '__main__':
    sys.exit(main())
