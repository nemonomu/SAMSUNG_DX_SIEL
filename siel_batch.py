from __future__ import annotations

import os
import time
from datetime import datetime


def next_batch_id(prefix: str, root: str, now: datetime | None = None) -> str:
    """Return a daily sequential batch id such as a_20260526_000004."""
    if not prefix:
        raise ValueError('batch prefix is required')
    dt = now or datetime.now()
    day = dt.strftime('%Y%m%d')
    state_dir = os.path.join(root, '.batch_ids')
    os.makedirs(state_dir, exist_ok=True)
    counter_path = os.path.join(state_dir, f'{prefix}_{day}.txt')
    lock_path = counter_path + '.lock'

    acquired = False
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            acquired = True
            break
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lock_path) > 30:
                    os.remove(lock_path)
                    continue
            except OSError:
                pass
            time.sleep(0.05)
    if not acquired:
        raise TimeoutError(f'could not acquire batch id lock: {lock_path}')

    try:
        try:
            with open(counter_path, 'r', encoding='utf-8') as f:
                n = int((f.read() or '0').strip())
        except (FileNotFoundError, ValueError):
            n = 0
        n += 1
        with open(counter_path, 'w', encoding='utf-8') as f:
            f.write(str(n))
        return f'{prefix}_{day}_{n:06d}'
    finally:
        try:
            os.remove(lock_path)
        except OSError:
            pass
