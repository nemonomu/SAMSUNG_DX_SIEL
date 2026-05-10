-- =============================================================================
-- dx_siel_tv_retail_com  — TV 제품 최종 통합 테이블 (운영)
-- =============================================================================
-- main + bsr + detail record 를 ASIN/fsn key 로 merge 후 INSERT.
-- 4 retail_com 테이블 (HHP/TV/REF/LDY) 모두 동일 schema.
-- amzn + fpkt 통합. SKU 단위 1 row (page_type='main' 우선).
-- =============================================================================

\encoding UTF8

CREATE TABLE IF NOT EXISTS dx_siel_tv_retail_com (
  id                SERIAL PRIMARY KEY,
  country           TEXT,
  product           TEXT,
  item              TEXT,
  sku               TEXT,
  account_name      TEXT,
  page_type         TEXT,
  retailer_sku_name TEXT,
  product_url       TEXT,
  calendar_week     TEXT,
  crawl_datetime    TIMESTAMPTZ,
  batch_id          TEXT,
  star_rating               TEXT,
  count_of_star_ratings     TEXT,
  count_of_reviews          TEXT,
  detailed_review_content   TEXT,
  retailer_sku_name_similar TEXT,
  final_sku_price    TEXT,
  original_sku_price TEXT,
  savings            TEXT,
  discount_type      TEXT,
  delivery_availability             TEXT,
  available_quantity_for_purchase   TEXT,
  sku_popularity TEXT,
  sku_status     TEXT,
  main_rank INTEGER,
  bsr_rank  INTEGER,
  screen_size                       TEXT,
  model_year                        TEXT,
  estimated_annual_electricity_use  TEXT,
  hhp_storage TEXT,
  hhp_color   TEXT,
  trade_in    TEXT,
  ref_refrigerator_type TEXT,
  ref_capacity          TEXT,
  ldy_loading_type TEXT,
  ldy_capacity     TEXT,
  summarized_review_content             TEXT,
  fastest_delivery                      TEXT,
  inventory_status                      TEXT,
  sku_assurance                         TEXT,
  number_of_units_purchased_past_month  TEXT,
  inserted_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_dx_siel_tv_retail_com_lookup
  ON dx_siel_tv_retail_com (account_name, product, item);
