-- =============================================================================
-- dx_siel_hhp_product_list  — HHP main + bsr 통합 테이블 (운영)
-- =============================================================================
-- main + bsr record 만 INSERT (detail 단계 제외).
-- 4 product_list 테이블 (HHP/TV/REF/LDY) 모두 동일 schema.
-- amzn + fpkt 통합. SKU 단위 1 row (page_type='main' 우선, retail_com 와 동일 dedupe 정책 P2).
-- retail_com schema 에서 detail 전용 컬럼 제외:
--   star_rating / detailed_review_content / retailer_sku_name_similar / delivery_availability /
--   summarized_review_content / fastest_delivery / inventory_status / sku_assurance /
--   screen_size / model_year / estimated_annual_electricity_use /
--   hhp_storage / hhp_color / trade_in / ref_* / ldy_*
-- available_quantity_for_purchase 는 fpkt main 수집 (Only X left) — fpkt 유일 재고 정보 (fpkt detail 에 inventory_status 미수집)
-- =============================================================================

\encoding UTF8

CREATE TABLE IF NOT EXISTS dx_siel_hhp_product_list (
  id                SERIAL PRIMARY KEY,
  -- 식별
  country           TEXT,                         -- 'siel'
  product           TEXT,                         -- 'HHP'
  item              TEXT,                         -- Flipkart fsn / Amazon asin
  account_name      TEXT,                         -- 'Flipkart' / 'Amazon'
  page_type         TEXT,                         -- 'main' / 'bsr' (병합 후 'main' 우선)
  -- 메타
  retailer_sku_name TEXT,
  product_url       TEXT,
  redirect          BOOLEAN,
  calendar_week     TEXT,
  crawl_datetime    TIMESTAMPTZ,
  batch_id          TEXT,
  -- 평점 (Flipkart main 에서만 — amzn main 미수집)
  star_rating               TEXT,
  count_of_star_ratings     TEXT,
  count_of_reviews          TEXT,
  -- 가격 (공통)
  final_sku_price    TEXT,
  original_sku_price TEXT,
  savings            TEXT,
  discount_type      TEXT,
  -- 재고 (Flipkart main 만 — "Only X left", fpkt 의 유일 재고 정보)
  available_quantity_for_purchase   TEXT,
  -- 마케팅
  sku_popularity TEXT,
  sku_status     TEXT,
  -- 순위
  main_rank INTEGER,
  bsr_rank  INTEGER,
  -- Amazon main 전용
  number_of_units_purchased_past_month  TEXT,
  -- 시스템
  inserted_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_dx_siel_hhp_product_list_lookup
  ON dx_siel_hhp_product_list (account_name, product, item);
