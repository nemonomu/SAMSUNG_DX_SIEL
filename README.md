# SAMSUNG_DX_SIEL — v2

Samsung India (SIEL) retail.com 크롤러. **4 제품군 × 2 사이트 = 8 조합**.

| 제품군 | Amazon.In | Flipkart |
|---|---|---|
| HHP (smartphone)         | O | O |
| TV                       | O | O |
| REF (refrigerator)       | O | O |
| LDY (laundry / 세탁기)    | O | O |

## 아키텍처 (v2)

- **드라이버**: `undetected_chromedriver` (anti-bot 우회)
- **xpath**: `dx_siel_xpath_selectors` 테이블에서 DB 로드 (코드 하드코딩 X)
- **출력**: stdout JSONL — 사용자 orchestrator 가 받아서 결과 DB INSERT
- **시각**: 모든 batch_id / crawl_datetime 인도 IST (Asia/Kolkata, UTC+5:30) 기준
- **배치**: AWS (Amazon 크롤러) + GCP (Flipkart 크롤러) 인스턴스에서 Windows Task Scheduler 가 주기 실행

## 폴더

```
samsung_dx_siel/
├ siel_log.py                공통 로깅 helper (logs/, IST, 가격 warning)
├ requirements.txt
├ config.example.py          DB_CONFIG 템플릿 (config.py 는 gitignored)
├ amzn/
│  ├ listing.py              Amazon Main + BSR (4 제품군 공유)
│  ├ detail.py               Amazon Product page
│  ├ run.py                  listing → detail 통합 진입점
│  └ logs/                   .log + .html (auto, gitignored)
├ fpkt/
│  ├ listing.py              Flipkart Main + BSR
│  ├ detail.py               Specifications click + Show all reviews + React click 좌표 fallback
│  ├ run.py
│  └ logs/
├ docs/
│  ├ TARGETS.md              제품군 × 사이트 × URL × 컬럼 매트릭스
│  └ COOKIES.md              계정/쿠키 운영 메모
└ sql/
   ├ create_result_db.sql
   ├ seed_meta_pages.sql
   └ seed_meta_selectors.sql
```

## DB 컨벤션

### `dx_siel_xpath_selectors` (사용자 생성 예정)

```sql
CREATE TABLE dx_siel_xpath_selectors (
  id              SERIAL PRIMARY KEY,
  site_account    VARCHAR(20)  NOT NULL,   -- 'Amazon' / 'Flipkart'
  page_type       VARCHAR(10)  NOT NULL,   -- 'main' / 'bsr' / 'detail'
  domain          VARCHAR(10)  NOT NULL,   -- 'hhp' / 'tv' / 'ref' / 'ldy'
  data_field      VARCHAR(64)  NOT NULL,
  xpath_primary   TEXT         NOT NULL,
  fallback_xpath  TEXT,
  is_active       BOOLEAN      DEFAULT TRUE,
  notes           TEXT,
  created_at      TIMESTAMPTZ  DEFAULT NOW(),
  updated_at      TIMESTAMPTZ  DEFAULT NOW(),
  UNIQUE (site_account, page_type, domain, data_field)
);
```

특수 `data_field` 키 (코드가 인식):
- `base_container` — listing 카드 anchor (다른 listing xpath 의 부모)
- `product_url` — href attr 추출 (다른 컬럼은 text)
- `expand_additional_details` / `expand_item_details` — Amazon Product 섹션 펼침 클릭
- `expand_specifications` — Flipkart Specifications 클릭 (robust click)
- `click_show_all_reviews` — Flipkart Show all reviews 진입 (Buy now 회피)
- `detailed_review_content` — 다중 element. `review{n} - text ||| ...` 합침
- `retailer_sku_name_similar` — 다중 element. `, ` 합침

### 결과 테이블 (사용자가 생성 / 적재)

`dx_siel_{account_name}_{product}_{stage}` — 24 개 (2 × 4 × 3). 통합 = `dx_siel_retail_com`.

**필수 공통 컬럼**: `account_name`, `crawl_datetime`, `batch_id`.
**`batch_id` 형식**: `YYYYMMDDHHMMSS_{account}_{product}_{stage}` (IST 기준).

## 실행

cwd = repo root.

```bash
# Amazon HHP main 30개만
python amzn/listing.py --product hhp --stage main --max-rank 30

# Amazon TV BSR 단독
python amzn/listing.py --product tv --stage bsr

# Amazon detail (단건)
python amzn/detail.py --product hhp --url https://www.amazon.in/dp/B0XXXXXXXX

# Amazon detail (stdin URL list)
cat urls.txt | python amzn/detail.py --product tv

# Amazon 통합 (main → detail)
python amzn/run.py --product hhp --stages main detail --max-rank 5 --max-detail 3

# Flipkart 통합 (bsr → detail)
python fpkt/run.py --product tv --stages bsr detail --max-rank 10 --max-detail 5

# headless (서버용)
python amzn/listing.py --product ref --stage bsr --headless
```

## 로그 / HTML snapshot

각 실행마다 `{site}/logs/` 에 자동 생성:
- `siel_{account_name}_{product}_{stage}_{YYMMDDHHMM}.log` — selector 스키마 + record 요약 + 가격 warning
- `siel_{account_name}_{product}_{stage}_{YYMMDDHHMM}.html` — 첫 페이지 page_source (디버깅용)

가격 warning 예: `final_sku_price > original_sku_price` 시 `[WARNING] price logic violation: final=... original=...`.

## 배포

- **Amazon 크롤러**: AWS Mumbai 리전 instance, RDP, git pull → 실행
- **Flipkart 크롤러**: GCP Mumbai 리전 instance, 동일 패턴
- 각 instance 의 자체 orchestrator 가 Task Scheduler 로 실행 → stdout JSONL 받아서 사용자 지정 DB 적재

## 셋업

```bash
git clone https://github.com/nemonomu/SAMSUNG_DX_SIEL.git samsung_dx_siel
cd samsung_dx_siel
python -m venv .venv
.venv\Scripts\activate            # Windows / RDP
pip install -r requirements.txt
cp config.example.py config.py    # config.py 에 실제 DB 접속 입력 (gitignored)
```

### git pull 시 SQL 자동 적용 — 한 번만 실행

```cmd
setup_hooks.bat
```

`git config core.hooksPath .githooks` 설정. 그 후 `git pull` 마다 `.githooks/post-merge` 가 `python apply_sql.py` 를 호출해 `sql/*.sql` 전체 자동 적용. 각 SQL 은 idempotent (`DROP TABLE IF EXISTS` 등) 라 재적용 안전.

첫 적용은 수동으로:
```cmd
python apply_sql.py
```

## 보안

- `config.py`, `.env`, `*.pkl` 등 평문 secret/cookie 파일은 모두 `.gitignore`. 절대 commit X.
- 결과 DB / 메타 DB / SFTP / 계정 비번은 `config.py` 에서만 로드. 코드 하드코딩 X.
