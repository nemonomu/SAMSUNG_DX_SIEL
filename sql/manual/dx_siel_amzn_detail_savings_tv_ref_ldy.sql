-- Apply only Amazon TV/REF/LDY detail savings selectors in PostgreSQL.
INSERT INTO dx_siel_xpath_selectors
  (site_account, page_type, domain, data_field,
   xpath_primary, fallback_xpath, is_active, notes)
VALUES
  ('Amazon', 'detail', 'tv', 'savings',
   '//*[@id="corePriceDisplay_desktop_feature_div"]//span[contains(concat(" ",normalize-space(@class)," ")," apex-savings-percentage ")]',
   NULL, TRUE, 'Displayed Amazon detail savings percentage'),
  ('Amazon', 'detail', 'ref', 'savings',
   '//*[@id="corePriceDisplay_desktop_feature_div"]//span[contains(concat(" ",normalize-space(@class)," ")," apex-savings-percentage ")]',
   NULL, TRUE, 'Displayed Amazon detail savings percentage'),
  ('Amazon', 'detail', 'ldy', 'savings',
   '//*[@id="corePriceDisplay_desktop_feature_div"]//span[contains(concat(" ",normalize-space(@class)," ")," apex-savings-percentage ")]',
   NULL, TRUE, 'Displayed Amazon detail savings percentage')
ON CONFLICT (site_account, page_type, domain, data_field) DO UPDATE SET
  xpath_primary = EXCLUDED.xpath_primary,
  fallback_xpath = EXCLUDED.fallback_xpath,
  is_active = EXCLUDED.is_active,
  notes = EXCLUDED.notes,
  updated_at = NOW();
