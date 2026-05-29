-- =============================================================================
-- dx_siel_hhp_item_mst  — HHP 마스터 (sku / carrier / color / storage / memory)
-- =============================================================================
-- amzn + fpkt 통합. (item, account_name) UNIQUE.
-- detail 추출 결과로 매번 upsert. 기존 row 중 NULL/공백 필드만 update.
-- =============================================================================

\encoding UTF8

CREATE TABLE IF NOT EXISTS dx_siel_hhp_item_mst (
  id              SERIAL PRIMARY KEY,
  item            TEXT,
  product_url     TEXT,
  sku             TEXT,
  account_name    TEXT,
  created_at      TIMESTAMPTZ DEFAULT NOW(),
  updated_at      TIMESTAMPTZ,
  is_product      BOOLEAN DEFAULT TRUE,
  is_checked      BOOLEAN DEFAULT FALSE,
  hhp_carrier     TEXT,
  hhp_color       TEXT,
  hhp_storage     TEXT,
  hhp_memory_ram  TEXT,
  CONSTRAINT uq_dx_siel_hhp_item_mst_item_account UNIQUE (item, account_name)
);

CREATE INDEX IF NOT EXISTS idx_dx_siel_hhp_item_mst_lookup
  ON dx_siel_hhp_item_mst (account_name, item);
