-- =============================================================================
-- dx_siel_test_retail_com  — 통합 테이블 (test 용, ERD v1 기준)
-- =============================================================================
-- main + detail record 를 product_url 로 merge 후 INSERT.
-- 운영 dx_siel_retail_com 과 동일 스키마. 사용자 직접 검수용.
-- =============================================================================

\encoding UTF8

DROP TABLE IF EXISTS dx_siel_test_retail_com;

CREATE TABLE dx_siel_test_retail_com (
  id                SERIAL PRIMARY KEY,
  -- 식별
  country           VARCHAR(20),                  -- 'siel'
  product           VARCHAR(10),                  -- 'TV' / 'HHP' / 'REF' / 'LDY'
  item              VARCHAR(64),                  -- Flipkart fsn / Amazon asin
  sku               TEXT,
  account_name      VARCHAR(20),                  -- 'Flipkart' / 'Amazon'
  page_type         VARCHAR(10),                  -- 'main' / 'bsr' / 'detail' (병합 후 'main' 기본)
  -- 메타
  retailer_sku_name TEXT,
  product_url       TEXT,
  calendar_week     VARCHAR(10),                  -- e.g. '2026-W18'
  crawl_datetime    TIMESTAMPTZ,
  batch_id          VARCHAR(128),
  -- 평점/리뷰 (공통)
  star_rating               VARCHAR(10),
  count_of_star_ratings     VARCHAR(20),
  count_of_reviews          VARCHAR(20),
  detailed_review_content   TEXT,
  retailer_sku_name_similar TEXT,
  -- 가격 (공통)
  final_sku_price    VARCHAR(30),
  original_sku_price VARCHAR(30),
  savings            VARCHAR(20),
  discount_type      VARCHAR(64),
  -- 배송/재고
  delivery_availability             TEXT,
  available_quantity_for_purchase   VARCHAR(64),
  -- 마케팅
  sku_popularity VARCHAR(64),
  sku_status     VARCHAR(20),
  -- 순위
  main_rank INTEGER,
  bsr_rank  INTEGER,
  -- TV 전용
  screen_size                       VARCHAR(64),
  model_year                        VARCHAR(10),
  estimated_annual_electricity_use  VARCHAR(64),
  -- HHP 전용
  hhp_storage TEXT,
  hhp_color   VARCHAR(64),
  trade_in    TEXT,
  -- REF 전용
  ref_refrigerator_type VARCHAR(64),
  ref_capacity          VARCHAR(64),
  -- LDY 전용
  ldy_loading_type VARCHAR(64),
  ldy_capacity     VARCHAR(64),
  -- Amazon 전용
  summarized_review_content             TEXT,
  fastest_delivery                      TEXT,
  inventory_status                      VARCHAR(64),
  sku_assurance                         VARCHAR(64),
  number_of_units_purchased_past_month  VARCHAR(64),  -- "2K+ bought in past month" 형식 (24 char)
  -- 시스템
  inserted_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_test_retail_com_lookup
  ON dx_siel_test_retail_com (account_name, product, item);
