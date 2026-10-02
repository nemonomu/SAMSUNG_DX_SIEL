-- Deploy with fpkt/detail.py. Selector rows only; no result data changes.
DO $$
DECLARE d TEXT;
BEGIN
  FOREACH d IN ARRAY ARRAY['hhp','tv','ref','ldy'] LOOP
    INSERT INTO dx_siel_xpath_selectors
      (site_account,page_type,domain,data_field,xpath_primary,fallback_xpath,notes)
    VALUES
      ('Flipkart','detail',d,'open_reviews_panel',
       '//*[@id="slot-list-container"]//a[contains(@href,"/ratings-reviews-details-page")]',
       NULL,
       '2026-10-02: Click the same-PID top rating link to mount the review panel; do not navigate to its href directly.'),
      ('Flipkart','detail',d,'click_show_all_reviews',
       '//div[normalize-space(text())="Ratings and reviews"]/ancestor::div[.//a[contains(@href,"/product-reviews/")]][1]//a[contains(@href,"/product-reviews/") and not(contains(@href,"buynow")) and not(contains(@href,"&an="))]',
       '//*[@id="slot-list-container"]/div/div[2]//a[contains(@href,"/product-reviews/") and not(contains(@href,"buynow")) and not(contains(@href,"&an="))]',
       '2026-10-02: Panel lives outside slot-list-container. Validate exact PID and reject aspect filters in code. Fallback supports legacy inline links.')
    ON CONFLICT (site_account,page_type,domain,data_field) DO UPDATE SET
      xpath_primary=EXCLUDED.xpath_primary,
      fallback_xpath=EXCLUDED.fallback_xpath,
      notes=EXCLUDED.notes, is_active=TRUE, updated_at=NOW();
  END LOOP;
END $$;
