from __future__ import annotations

from datetime import datetime


def next_batch_id(prefix: str, root: str = '', now: datetime | None = None) -> str:
    """크롤 시작 시각 (서버 시간) 기준 batch id: 'a_20260530_143022' 형식.
    뒤 6자리는 HHMMSS. 같은 초 collision 은 호출 측이 보장 (listing/detail 다른 시점).
    root 인자는 file lock 시절 잔재 — 호환 위해 시그니처만 유지.
    """
    if not prefix:
        raise ValueError('batch prefix is required')
    dt = now or datetime.now()
    return f'{prefix}_{dt.strftime("%Y%m%d_%H%M%S")}'
