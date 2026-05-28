-- =============================================================================
-- dx_siel_ldy_product_list  — LDY main + bsr 통합 테이블 (운영)
-- =============================================================================
-- main + bsr record 만 INSERT (detail 단계 제외).
-- amzn + fpkt 통합. SKU 단위 1 row (page_type='main' 우선).
-- =============================================================================

\encoding UTF8

CREATE TABLE IF NOT EXISTS dx_siel_ldy_product_list (
  id                SERIAL PRIMARY KEY,
  country           TEXT,
  product           TEXT,
  item              TEXT,
  account_name      TEXT,
  page_type         TEXT,
  retailer_sku_name TEXT,
  product_url       TEXT,
  redirect          BOOLEAN,
  calendar_week     TEXT,
  crawl_datetime    TIMESTAMPTZ,
  batch_id          TEXT,
  star_rating               TEXT,
  count_of_star_ratings     TEXT,
  count_of_reviews          TEXT,
  final_sku_price    TEXT,
  original_sku_price TEXT,
  savings            TEXT,
  discount_type      TEXT,
  available_quantity_for_purchase   TEXT,
  sku_popularity TEXT,
  sku_status     TEXT,
  main_rank INTEGER,
  bsr_rank  INTEGER,
  number_of_units_purchased_past_month  TEXT,
  inserted_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_dx_siel_ldy_product_list_lookup
  ON dx_siel_ldy_product_list (account_name, product, item);
