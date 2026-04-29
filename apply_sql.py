"""sql/*.sql 을 config.DB_CONFIG 로 적용 (psycopg2).

git post-merge hook 이 호출. 수동도 가능: python apply_sql.py [path1.sql path2.sql ...]
인자 없으면 sql/ 의 모든 .sql 알파벳 순서로 적용.

각 .sql 은 idempotent 해야 함 (DROP IF EXISTS / CREATE IF NOT EXISTS / INSERT ON CONFLICT 등).
psql meta-command (\\encoding 등) 은 자동 strip — psycopg2 가 실행 못함.
"""
from __future__ import annotations

import os
import re
import sys
import traceback

_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import psycopg2
import config


def _connect():
    cfg = dict(config.DB_CONFIG)
    cfg.setdefault('database', 'postgres')
    cfg.setdefault('client_encoding', 'utf8')
    return psycopg2.connect(**cfg)


def _strip_psql_meta(sql: str) -> str:
    """psql backslash meta-command (\\encoding, \\c 등) 줄 제거."""
    out = []
    for line in sql.split('\n'):
        if line.lstrip().startswith('\\'):
            continue
        out.append(line)
    return '\n'.join(out)


def _strip_sql_comments(sql: str) -> str:
    """-- 줄 주석 + /* */ 블록 주석 제거 (executable 판정용)."""
    sql = re.sub(r'/\*.*?\*/', '', sql, flags=re.DOTALL)
    sql = re.sub(r'--[^\n]*', '', sql)
    return sql


def apply(sql_file: str) -> bool:
    if not os.path.exists(sql_file):
        print(f'[apply_sql] skip (not found): {sql_file}', file=sys.stderr)
        return True
    with open(sql_file, 'r', encoding='utf-8') as f:
        sql = f.read()
    sql_no_meta = _strip_psql_meta(sql)
    sql_executable = _strip_sql_comments(sql_no_meta).strip()
    if not sql_executable:
        print(f'[apply_sql] skip (no executable statements): {sql_file}',
              file=sys.stderr)
        return True
    print(f'[apply_sql] applying: {sql_file}', file=sys.stderr)
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql_no_meta)
        conn.commit()
        print(f'[apply_sql] OK: {sql_file}', file=sys.stderr)
        return True
    except psycopg2.ProgrammingError as e:
        conn.rollback()
        if 'empty query' in str(e).lower():
            print(f'[apply_sql] skip (empty query): {sql_file}', file=sys.stderr)
            return True
        print(f'[apply_sql] FAIL: {sql_file} — {type(e).__name__}: {e}',
              file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return False
    except Exception as e:
        conn.rollback()
        print(f'[apply_sql] FAIL: {sql_file} — {type(e).__name__}: {e}',
              file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return False
    finally:
        conn.close()


def main() -> int:
    if len(sys.argv) > 1:
        files = sys.argv[1:]
    else:
        sql_dir = os.path.join(_ROOT, 'sql')
        if not os.path.isdir(sql_dir):
            print('[apply_sql] no sql/ directory', file=sys.stderr)
            return 0
        files = sorted(
            os.path.join(sql_dir, f)
            for f in os.listdir(sql_dir)
            if f.endswith('.sql')
        )
    rc = 0
    for f in files:
        if not apply(f):
            rc = 1
    return rc


if __name__ == '__main__':
    sys.exit(main())
