"""amzn 테스트 — REF → LDY 순차, BSR (page 1+2) + detail 까지 수집.

RDP 2 (REF+LDY 머신) 에서 실행:
  python amzn/run_ref_ldy.py

amzn/run.py 의 main() 을 sys.argv 만 박아서 호출. driver 공유, streaming INSERT.
BSR_URL_TEMPLATES 에 page 1, 2 두 URL 박혀있어 두 페이지 다 처리.
bsr-max-rank=0 → 무제한 (cards 두 페이지 합 ~100 그대로 emit).
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

sys.argv = [
    sys.argv[0],
    '--product', 'ref', 'ldy',
    '--stages', 'bsr', 'detail',
    '--bsr-max-rank', '0',
]

from amzn.run import main

if __name__ == '__main__':
    sys.exit(main())
