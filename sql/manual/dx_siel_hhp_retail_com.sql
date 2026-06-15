-- =============================================================================
-- dx_siel_hhp_retail_com  — HHP 제품 최종 통합 테이블 (운영)
-- =============================================================================
-- main + bsr + detail record 를 ASIN/fsn key 로 merge 후 INSERT.
-- 4 retail_com 테이블 (HHP/TV/REF/LDY) 모두 동일 schema.
-- amzn + fpkt 통합. SKU 단위 1 row (page_type='main' 우선).
-- =============================================================================

\encoding UTF8

-- 데이터 보존 — DROP 금지. schema 변경 시 사용자가 수동 DROP 후 재실행.
CREATE TABLE IF NOT EXISTS dx_siel_hhp_retail_com (
  id                SERIAL PRIMARY KEY,
  -- 식별
  country           TEXT,                         -- 'SIEL'
  product           TEXT,                         -- 'HHP'
  item              TEXT,                         -- Flipkart fsn / Amazon asin
  sku               TEXT,
  account_name      TEXT,                         -- 'Flipkart' / 'Amazon'
  page_type         TEXT,                         -- 'main' / 'bsr' (병합 후 'main' 우선)
  -- 메타
  retailer_sku_name TEXT,
  product_url       TEXT,
  redirect          BOOLEAN,
  calendar_week     TEXT,
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
  -- HHP 전용 (다른 product 전용 컬럼은 제외 — TV/REF/LDY 전용은 각 retail_com 만)
  hhp_storage TEXT,
  hhp_color   TEXT,
  hhp_memory_ram  TEXT,
  trade_in    TEXT,
  -- Amazon 전용
  summarized_review_content             TEXT,
  fastest_delivery                      TEXT,
  inventory_status                      TEXT,
  sku_assurance                         TEXT,
  number_of_units_purchased_past_month  TEXT,
  -- 시스템
  inserted_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_dx_siel_hhp_retail_com_lookup
  ON dx_siel_hhp_retail_com (account_name, product, item);
