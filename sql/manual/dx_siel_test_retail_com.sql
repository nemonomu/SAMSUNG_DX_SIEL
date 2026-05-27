-- =============================================================================
-- dx_siel_test_retail_com  — 통합 테이블 (test 용, ERD v1 기준)
-- =============================================================================
-- main + detail record 를 product_url 로 merge 후 INSERT.
-- 운영 dx_siel_retail_com 과 동일 스키마. 사용자 직접 검수용.
-- =============================================================================

\encoding UTF8

-- 데이터 보존 — DROP 금지. schema 변경 시 사용자가 수동 DROP 후 재실행.
CREATE TABLE IF NOT EXISTS dx_siel_test_retail_com (
  id                SERIAL PRIMARY KEY,
  -- 식별
  country           TEXT,                         -- 'siel'
  product           TEXT,                         -- 'TV' / 'HHP' / 'REF' / 'LDY'
  item              TEXT,                         -- Flipkart fsn / Amazon asin
  sku               TEXT,
  account_name      TEXT,                         -- 'Flipkart' / 'Amazon'
  page_type         TEXT,                         -- 'main' / 'bsr' / 'detail' (병합 후 'main' 기본)
  -- 메타
  retailer_sku_name TEXT,
  product_url       TEXT,
  calendar_week     TEXT,                         -- e.g. '2026-W18'
  crawl_datetime    TIMESTAMPTZ,
  batch_id          TEXT,
  -- 평점/리뷰 (공통)
  star_rating               TEXT,
  count_of_star_ratings     TEXT,
  count_of_reviews          TEXT,
  detailed_review_content   TEXT,
  retailer_sku_name_similar TEXT,
  -- 가격 (공통)
  final_sku_price    TEXT,
  original_sku_price TEXT,
  savings            TEXT,
  discount_type      TEXT,
  -- 배송/재고
  delivery_availability             TEXT,
  available_quantity_for_purchase   TEXT,
  -- 마케팅
  sku_popularity TEXT,
  sku_status     TEXT,
  -- 순위
  main_rank INTEGER,
  bsr_rank  INTEGER,
  -- TV 전용
  screen_size                       TEXT,
  model_year                        TEXT,
  estimated_annual_electricity_use  TEXT,
  -- HHP 전용
  hhp_storage TEXT,
  hhp_color   TEXT,
  ram_memory  TEXT,
  trade_in    TEXT,
  -- REF 전용
  ref_refrigerator_type TEXT,
  ref_capacity          TEXT,
  -- LDY 전용
  ldy_loading_type TEXT,
  ldy_capacity     TEXT,
  -- Amazon 전용
  summarized_review_content             TEXT,
  fastest_delivery                      TEXT,
  inventory_status                      TEXT,
  sku_assurance                         TEXT,
  number_of_units_purchased_past_month  TEXT,
  -- 시스템
  inserted_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_test_retail_com_lookup
  ON dx_siel_test_retail_com (account_name, product, item);
