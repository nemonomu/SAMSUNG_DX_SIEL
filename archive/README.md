# archive

운영 실행(schedule/ → amzn/, fpkt/, fpkt_api/)과 직접 관련 없는 일회성 스크립트·자료 보관.

- `inspect_*.py`, `analyze.py`, `dump_urls.py`, `q.py`, `select_test.py` — 개발 중 selector/데이터 검증용 일회성 스크립트
- `ingest_retail_com.py` — 구 테스트 테이블(dx_siel_test_retail_com) ingester, insert_test_retail_com.py 로 대체됨
- `fpkt_api/price_probe.py` — Flipkart 가격 API 분석용 probe (main_probe/phase_probe 는 ops_test.py 가 import 하는 실행 모듈이라 fpkt_api/ 에 유지)
- `amzn/inspect_main_dom.py`, `amzn/inspect_strike.py` — Amazon HTML snapshot 분석용 일회성 스크립트
- `target_page_url.docx` — 초기 수집 대상 URL 문서
- `*.csv` — 로컬 데이터 덤프 (gitignore 대상, push 안 됨)

주의: 루트 기준 상대 import/경로를 쓰던 스크립트는 여기서 그대로 실행되지 않을 수 있음 (필요 시 repo 루트로 복사 후 사용).
