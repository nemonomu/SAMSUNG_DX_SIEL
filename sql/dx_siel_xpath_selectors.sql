-- =============================================================================
-- dx_siel_xpath_selectors  — SIEL 크롤러 셀렉터 테이블
-- =============================================================================
-- 코드 (.py) 가 SELECT 하는 형식. 컬럼/이름 변경 시 .py 코드 도 같이 수정 필요.
-- 특수 data_field 키 (코드가 인식):
--   base_container             listing 카드 anchor
--   product_url                href attr 추출 (다른 컬럼은 text)
--   expand_additional_details  Amazon Product 섹션 펼침 클릭
--   expand_item_details        Amazon Product 섹션 펼침 클릭
--   expand_specifications      Flipkart Specifications 클릭 (robust click)
--   click_show_all_reviews     Flipkart 리뷰 페이지 진입 (robust click)
--   detailed_review_content    다중 element. 'review{n} - text ||| ...' 합침
--   retailer_sku_name_similar  다중 element. ', ' 합침
-- =============================================================================

\encoding UTF8

DROP TABLE IF EXISTS dx_siel_xpath_selectors;

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

CREATE INDEX idx_dx_siel_xpath_lookup ON dx_siel_xpath_selectors
  (site_account, page_type, domain, is_active);

-- =============================================================================
-- AMAZON × MAIN  (검색 결과 페이지)
-- 4 제품군 동일 DOM — 같은 셀렉터 4 row 씩
-- =============================================================================

-- 헬퍼: 4 제품군에 동일 셀렉터를 한꺼번에 INSERT
-- (PostgreSQL 익명 procedural 블록)
DO $$
DECLARE
  d TEXT;
  domains TEXT[] := ARRAY['hhp','tv','ref','ldy'];
BEGIN
  FOREACH d IN ARRAY domains LOOP
    INSERT INTO dx_siel_xpath_selectors
      (site_account, page_type, domain, data_field, xpath_primary, fallback_xpath, notes)
    VALUES
      ('Amazon','main',d,'base_container',
       '//div[@data-component-type="s-search-result" and @data-asin and @data-asin!=""]',
       NULL,
       'Amazon 검색 결과 카드 wrapper'),
      ('Amazon','main',d,'product_url',
       './/a[contains(@class,"a-link-normal") and contains(@href,"/dp/")]',
       './/h2//a',
       'dp 링크 직접 매칭 — h2 위치 변경에도 robust'),
      ('Amazon','main',d,'retailer_sku_name',
       './/a[contains(@href,"/dp/")]//span[normalize-space(text())][1]',
       './/h2//span[normalize-space(text())][1]',
       '상품명 — dp 링크 첫 비빈 span'),
      ('Amazon','main',d,'final_sku_price',
       './/span[@class="a-price" and @data-a-color="base"]//span[@class="a-offscreen"]',
       './/span[contains(@class,"a-price") and not(@data-a-strike)]//span[@class="a-offscreen"]',
       'CSV 검증된 robust pattern'),
      ('Amazon','main',d,'original_sku_price',
       './/span[@class="a-price a-text-price" and @data-a-strike="true"]//span[@class="a-offscreen"]',
       './/span[@data-a-strike="true"]//span[@class="a-offscreen"]',
       'M.R.P. 가격 (할인 전)'),
      ('Amazon','main',d,'discount_type',
       './/*[contains(@id,"DEAL_") and contains(@id,"-label")]//span[contains(@class,"a-badge-text")]',
       './/span[contains(@class,"s-coupon-clipped")]',
       'Limited time deal / Coupon 등 — Amazon Choice 배지 분리'),
      ('Amazon','main',d,'sku_popularity',
       './/span[@aria-label="Amazon''s Choice" or contains(text(),"Best Seller")]',
       './/*[contains(@id,"amazons-choice-label")]//span',
       'Amazon Choice / Best Seller 배지'),
      ('Amazon','main',d,'sku_status',
       './/span[contains(@class,"puis-sponsored-label-text") or text()="Sponsored"]',
       './/a[contains(@aria-label,"Sponsored")]//span',
       'Sponsored 광고 표시'),
      ('Amazon','main',d,'number_of_units_purchased_past_month',
       './/span[contains(@class,"a-color-secondary") and contains(text(),"bought in past month")]',
       './/span[contains(text(),"bought in past")]',
       'e.g. "2K+ bought in past month"');
  END LOOP;
END $$;

-- =============================================================================
-- AMAZON × BSR  (Best Seller 페이지, /gp/bestsellers/)
-- ERD: BSR Page = bsr_rank 만 명시. bsr_rank 는 코드의 positional counter 가 자동 할당.
-- 따라서 base_container + product_url 만 시드. 4 제품군 동일 DOM.
-- =============================================================================

DO $$
DECLARE
  d TEXT;
  domains TEXT[] := ARRAY['hhp','tv','ref','ldy'];
BEGIN
  FOREACH d IN ARRAY domains LOOP
    INSERT INTO dx_siel_xpath_selectors
      (site_account, page_type, domain, data_field, xpath_primary, fallback_xpath, notes)
    VALUES
      ('Amazon','bsr',d,'base_container',
       '//div[@id="gridItemRoot"]',
       '//div[contains(@class,"zg-grid-general-faceout")]',
       'Amazon BSR 카드'),
      ('Amazon','bsr',d,'product_url',
       './/a[contains(@class,"a-link-normal") and contains(@href,"/dp/")]',
       './/a[contains(@href,"/dp/")]',
       'href attr — BSR 의 ASIN 은 url 에서 추출 가능');
  END LOOP;
END $$;

-- =============================================================================
-- AMAZON × DETAIL × HHP  (Product Page, smartphone)
-- =============================================================================

INSERT INTO dx_siel_xpath_selectors
  (site_account, page_type, domain, data_field, xpath_primary, fallback_xpath, notes)
VALUES
  ('Amazon','detail','hhp','expand_additional_details',
   '//a[contains(@class,"a-expander-prompt") and contains(text(),"See more")]',
   '//div[@id="productOverview_feature_div"]//a[contains(@class,"a-expander")]',
   'Additional details 섹션 펼치기'),
  ('Amazon','detail','hhp','delivery_availability',
   '//*[@id="mir-layout-DELIVERY_BLOCK-slot-PRIMARY_DELIVERY_MESSAGE_LARGE"]//span[1]',
   '//div[@id="deliveryBlockMessage"]//span',
   '끝 "Details" 텍스트 후처리에서 제거'),
  ('Amazon','detail','hhp','fastest_delivery',
   '//*[contains(@class,"udm-delivery-")]//span[contains(text(),"fastest")] | //*[@id="mir-layout-DELIVERY_BLOCK-slot-SECONDARY_DELIVERY_MESSAGE_LARGE"]//span[1]',
   NULL,
   'udm-delivery (신) | mir-layout (구). product 따라 valid null 가능'),
  ('Amazon','detail','hhp','inventory_status',
   '//*[@id="availability"]//span',
   '//div[@id="availability"]//span',
   '"In Stock" 등. 빈값/공백 → null'),
  ('Amazon','detail','hhp','retailer_sku_name_similar',
   '//*[contains(@id,"anonCarousel")]//li//a/span[1] | //*[@id="anonCarousel2"]//li//a[contains(@class,"a-link-normal")]//div[contains(@class,"a-truncate-full")]',
   '//*[contains(@id,"sims-fbt")]//div[contains(@class,"a-truncate-full")]',
   'anonCarousel id varies (1/2/3) — span[1] 신패턴 + truncate-full 구패턴 union'),
  ('Amazon','detail','hhp','star_rating',
   '//*[@data-hook="rating-out-of-text"] | //*[@id="acrPopover"]//span[@class="a-size-base a-color-base"]',
   '//*[@id="cm_cr_dp_d_rating_histogram"]//div[contains(@class,"a-section")]/div/div/span/span',
   'data-hook="rating-out-of-text" (Amazon 공식 위젯) 우선. "4.2 out of 5" 후처리'),
  ('Amazon','detail','hhp','count_of_star_ratings',
   '//*[@id="acrCustomerReviewText"]',
   '//*[@id="cm_cr_dp_d_rating_histogram"]//span[contains(text(),"global ratings")]',
   '"1,009 ratings" or "(6,743)" — 후처리에서 숫자만 + paren strip'),
  ('Amazon','detail','hhp','summarized_review_content',
   '//*[@data-testid="overall-summary"] | //div[@data-hook="cr-insights-widget"]//span | //*[@id="reviewsMedley"]//div[contains(@id,"review-summary")]//span',
   '//div[@data-hook="cr-summarization-attributes-list"]//span',
   '리뷰 AI 요약 — overall-summary (신 testid) | cr-insights-widget | reviewsMedley union'),
  ('Amazon','detail','hhp','detailed_review_content',
   '//div[@data-hook="review-collapsed" or @data-hook="review-body"]//span[not(@class)]',
   '//div[contains(@id,"customer_review-")]//span[@data-hook="review-body"]',
   '다중 추출 — 페이지 하단 끝까지 스크롤 후'),
  ('Amazon','detail','hhp','sku',
   '//input[@id="ASIN"] | //div[@id="detailBullets_feature_div"]//li[.//span[contains(text(),"ASIN")]]/span[2] | //table[contains(@id,"productDetails_detailBullets") or contains(@id,"productDetails_techSpec") or contains(@id,"productDetails_expanderTables")]//tr[.//th[contains(text(),"ASIN")]]/td',
   '//div[@id="detailBullets_feature_div"]//li[.//span[contains(text(),"ASIN")]]/span[2]',
   'HHP ASIN: hidden input[@id="ASIN"] (신 UI, primary) | detailBullets | productDetails table. input 은 코드가 attr value 로 추출'),
  ('Amazon','detail','hhp','item',
   '//table//tr[.//th[contains(text(),"ASIN")]]/td',
   '//div[@id="detailBullets_feature_div"]//li[.//span[contains(text(),"ASIN")]]/span[2]',
   'item = asin (코드는 url 에서도 추출 시도)'),
  ('Amazon','detail','hhp','hhp_storage',
   '//table//tr[.//th[contains(text(),"Memory Storage Capacity") or contains(text(),"Internal Memory")]]/td',
   '//div[@id="poExpander"]//table//tr[.//td[contains(text(),"Memory")]]/td[2]',
   'e.g. "64 GB"'),
  ('Amazon','detail','hhp','hhp_color',
   '//table//tr[.//th[contains(text(),"Colour") or contains(text(),"Color")]]/td',
   '//div[@id="poExpander"]//table//tr[.//td[contains(text(),"Colour") or contains(text(),"Color")]]/td[2]',
   'e.g. "Black"'),
  ('Amazon','detail','hhp','trade_in',
   '//*[@id="buyBackAccordionRow"]//h5',
   '//div[contains(@id,"buyBack") or contains(@id,"exchangePopover")]//*[contains(text(),"Exchange") or contains(text(),"Trade-in")]',
   '"Trade-in and save" / "With Exchange Up to ..."');

-- =============================================================================
-- AMAZON × DETAIL × TV
-- =============================================================================

INSERT INTO dx_siel_xpath_selectors
  (site_account, page_type, domain, data_field, xpath_primary, fallback_xpath, notes)
VALUES
  ('Amazon','detail','tv','expand_item_details',
   '//a[contains(@class,"a-expander-prompt") and contains(text(),"See more")]',
   NULL,
   'Item details 섹션 펼치기'),
  ('Amazon','detail','tv','delivery_availability',
   '//*[@id="mir-layout-DELIVERY_BLOCK-slot-PRIMARY_DELIVERY_MESSAGE_LARGE"]//span[1]',
   NULL, NULL),
  ('Amazon','detail','tv','fastest_delivery',
   '//*[contains(@class,"udm-delivery-")]//span[contains(text(),"fastest")] | //*[@id="mir-layout-DELIVERY_BLOCK-slot-SECONDARY_DELIVERY_MESSAGE_LARGE"]//span[1]',
   NULL, 'udm-delivery (신) | mir-layout (구) union'),
  ('Amazon','detail','tv','inventory_status',
   '//*[@id="availability"]//span', NULL, NULL),
  ('Amazon','detail','tv','retailer_sku_name_similar',
   '//*[contains(@id,"anonCarousel")]//li//a/span[1] | //*[@id="anonCarousel2"]//li//div[contains(@class,"a-truncate-full")]',
   NULL, 'anonCarousel id varies — span[1] 신 | truncate-full 구 union'),
  ('Amazon','detail','tv','star_rating',
   '//*[@data-hook="rating-out-of-text"] | //*[@id="acrPopover"]//span[@class="a-size-base a-color-base"]',
   NULL, 'data-hook="rating-out-of-text" 우선'),
  ('Amazon','detail','tv','count_of_star_ratings',
   '//*[@id="acrCustomerReviewText"]', NULL, NULL),
  ('Amazon','detail','tv','summarized_review_content',
   '//div[@data-hook="cr-insights-widget"]//span | //*[@id="reviewsMedley"]//div[contains(@id,"review-summary")]//span',
   NULL, 'cr-insights-widget (신) | reviewsMedley (구) union'),
  ('Amazon','detail','tv','detailed_review_content',
   '//div[@data-hook="review-collapsed" or @data-hook="review-body"]//span[not(@class)]',
   NULL, NULL),
  ('Amazon','detail','tv','sku',
   '//table//tr[.//th[contains(text(),"Manufacturer") and contains(text(),"Part Number")]]/td',
   '//table//tr[.//th[contains(text(),"Item model number")]]/td',
   'TV: Manufacturer Part Number'),
  ('Amazon','detail','tv','sku_assurance',
   '//*[@id="freeShippingPriceBadging_feature_div"]//i//span',
   '//*[contains(@id,"shippedBy") or contains(@id,"merchant-info")]//span',
   '"Fulfilled" → 후처리에서 "Amazon Fulfilled" 로 저장'),
  ('Amazon','detail','tv','screen_size',
   '//*[@id="poExpander"]//table//tr[.//td[contains(text(),"Screen Size")]]/td[2]',
   '//table//tr[.//th[contains(text(),"Screen Size")]]/td',
   'e.g. "43 Inches"'),
  ('Amazon','detail','tv','estimated_annual_electricity_use',
   '//table//tr[.//th[contains(text(),"Annual Energy Consumption")]]/td',
   '//div[@id="productDetails_techSpec_section_2"]//tr[.//th[contains(text(),"Energy")]]/td',
   'e.g. "237.25 Kilowatt Hours Per Year"'),
  ('Amazon','detail','tv','model_year',
   '//table//tr[.//th[contains(text(),"Model Year")]]/td',
   NULL, NULL);

-- =============================================================================
-- AMAZON × DETAIL × REF
-- =============================================================================

INSERT INTO dx_siel_xpath_selectors
  (site_account, page_type, domain, data_field, xpath_primary, fallback_xpath, notes)
VALUES
  ('Amazon','detail','ref','expand_item_details',
   '//a[contains(@class,"a-expander-prompt") and contains(text(),"See more")]',
   NULL, NULL),
  ('Amazon','detail','ref','delivery_availability',
   '//*[@id="mir-layout-DELIVERY_BLOCK-slot-PRIMARY_DELIVERY_MESSAGE_LARGE"]//span[1]', NULL, NULL),
  ('Amazon','detail','ref','fastest_delivery',
   '//*[contains(@class,"udm-delivery-")]//span[contains(text(),"fastest")] | //*[@id="mir-layout-DELIVERY_BLOCK-slot-SECONDARY_DELIVERY_MESSAGE_LARGE"]//span[1]',
   NULL, 'udm-delivery (신) | mir-layout (구) union'),
  ('Amazon','detail','ref','inventory_status',
   '//*[@id="availability"]//span', NULL, NULL),
  ('Amazon','detail','ref','retailer_sku_name_similar',
   '//*[contains(@id,"anonCarousel")]//li//a/span[1] | //*[@id="anonCarousel2"]//li//div[contains(@class,"a-truncate-full")]',
   NULL, 'anonCarousel id varies — span[1] 신 | truncate-full 구 union'),
  ('Amazon','detail','ref','star_rating',
   '//*[@data-hook="rating-out-of-text"] | //*[@id="acrPopover"]//span[@class="a-size-base a-color-base"]',
   NULL, 'data-hook="rating-out-of-text" 우선'),
  ('Amazon','detail','ref','count_of_star_ratings',
   '//*[@id="acrCustomerReviewText"]', NULL, NULL),
  ('Amazon','detail','ref','summarized_review_content',
   '//div[@data-hook="cr-insights-widget"]//span | //*[@id="reviewsMedley"]//div[contains(@id,"review-summary")]//span',
   NULL, 'cr-insights-widget (신) | reviewsMedley (구) union'),
  ('Amazon','detail','ref','detailed_review_content',
   '//div[@data-hook="review-collapsed" or @data-hook="review-body"]//span[not(@class)]',
   NULL, NULL),
  ('Amazon','detail','ref','sku',
   '//table//tr[.//th[contains(text(),"Manufacturer") and contains(text(),"Part Number")]]/td',
   '//table//tr[.//th[contains(text(),"Item model number")]]/td', NULL),
  ('Amazon','detail','ref','ref_refrigerator_type',
   '//table//tr[.//th[contains(text(),"Configuration") or contains(text(),"Refrigerator Type")]]/td',
   '//table//tr[.//th[contains(text(),"Style")]]/td',
   'e.g. Side-by-Side, French Door, Top Mount'),
  ('Amazon','detail','ref','ref_capacity',
   '//table//tr[.//th[contains(text(),"Capacity")]]/td',
   '//table//tr[.//th[contains(text(),"Total Capacity") or contains(text(),"Capacity (Litres)")]]/td',
   'e.g. "300L"');

-- =============================================================================
-- AMAZON × DETAIL × LDY (laundry / 세탁기)
-- =============================================================================

INSERT INTO dx_siel_xpath_selectors
  (site_account, page_type, domain, data_field, xpath_primary, fallback_xpath, notes)
VALUES
  ('Amazon','detail','ldy','expand_item_details',
   '//a[contains(@class,"a-expander-prompt") and contains(text(),"See more")]', NULL, NULL),
  ('Amazon','detail','ldy','delivery_availability',
   '//*[@id="mir-layout-DELIVERY_BLOCK-slot-PRIMARY_DELIVERY_MESSAGE_LARGE"]//span[1]', NULL, NULL),
  ('Amazon','detail','ldy','fastest_delivery',
   '//*[contains(@class,"udm-delivery-")]//span[contains(text(),"fastest")] | //*[@id="mir-layout-DELIVERY_BLOCK-slot-SECONDARY_DELIVERY_MESSAGE_LARGE"]//span[1]',
   NULL, 'udm-delivery (신) | mir-layout (구) union'),
  ('Amazon','detail','ldy','inventory_status',
   '//*[@id="availability"]//span', NULL, NULL),
  ('Amazon','detail','ldy','retailer_sku_name_similar',
   '//*[contains(@id,"anonCarousel")]//li//a/span[1] | //*[@id="anonCarousel2"]//li//div[contains(@class,"a-truncate-full")]',
   NULL, 'anonCarousel id varies — span[1] 신 | truncate-full 구 union'),
  ('Amazon','detail','ldy','star_rating',
   '//*[@data-hook="rating-out-of-text"] | //*[@id="acrPopover"]//span[@class="a-size-base a-color-base"]',
   NULL, 'data-hook="rating-out-of-text" 우선'),
  ('Amazon','detail','ldy','count_of_star_ratings',
   '//*[@id="acrCustomerReviewText"]', NULL, NULL),
  ('Amazon','detail','ldy','summarized_review_content',
   '//div[@data-hook="cr-insights-widget"]//span | //*[@id="reviewsMedley"]//div[contains(@id,"review-summary")]//span',
   NULL, 'cr-insights-widget (신) | reviewsMedley (구) union'),
  ('Amazon','detail','ldy','detailed_review_content',
   '//div[@data-hook="review-collapsed" or @data-hook="review-body"]//span[not(@class)]',
   NULL, NULL),
  ('Amazon','detail','ldy','sku',
   '//table//tr[.//th[contains(text(),"Manufacturer") and contains(text(),"Part Number")]]/td',
   NULL, NULL),
  ('Amazon','detail','ldy','ldy_loading_type',
   '//table//tr[.//th[contains(text(),"Access Location") or contains(text(),"Loading Type") or contains(text(),"Configuration")]]/td',
   NULL,
   'e.g. "Top load" / "Front load"'),
  ('Amazon','detail','ldy','ldy_capacity',
   '//table//tr[.//th[contains(text(),"Capacity")]]/td',
   '//table//tr[.//th[contains(text(),"Washing Capacity")]]/td',
   'e.g. "8kg"');

-- =============================================================================
-- FLIPKART × MAIN  (relevance 정렬 검색 결과)
-- HHP 만 가격 미수집 (ERD: HHP 가격은 product page 에서)
-- =============================================================================

DO $$
DECLARE
  d TEXT;
  domains TEXT[] := ARRAY['hhp','tv','ref','ldy'];
BEGIN
  FOREACH d IN ARRAY domains LOOP
    INSERT INTO dx_siel_xpath_selectors
      (site_account,page_type,domain,data_field,xpath_primary,fallback_xpath,notes)
    VALUES
      ('Flipkart','main',d,'base_container',
       '//div[@data-id and .//a[contains(@href,"/p/")]]',
       '//a[contains(@href,"/p/")]/ancestor::div[count(.//a[contains(@href,"/p/")])=1][last()]',
       'Flipkart 카드 = data-id attr 가진 div (pid 와 일치). 페이지당 ~24 개'),
      ('Flipkart','main',d,'product_url',
       './/a[contains(@href,"/p/")]',
       NULL,
       'href attr (코드 자동)'),
      ('Flipkart','main',d,'retailer_sku_name',
       './/a[contains(@href,"/p/")]//div[string-length(normalize-space(text()))>10][1]',
       './/a[contains(@href,"/p/")]/@title',
       'Flipkart 상품명 — 충분히 긴 텍스트 div 첫 번째'),
      ('Flipkart','main',d,'discount_type',
       './/*[contains(text(),"% off") or contains(text(),"Limited") or contains(text(),"Hot deal")]',
       './/div[contains(.,"% off")][not(.//div[contains(.,"% off")])]',
       'e.g. "53% off". any-element + text() 직접; fallback 은 innermost div 의 string-value'),
      ('Flipkart','main',d,'sku_popularity',
       './/a[contains(@href,"spotlightTagId=default_BestsellerId")] | .//img[contains(@src,"/fa_")]',
       NULL,
       'Bestseller (anchor href spotlightTagId) / Flipkart Assured (img src /fa_*.png). 둘 다 marker — 코드가 attribute 검사 후 라벨 합침'),
      ('Flipkart','main',d,'sku_status',
       './/div[contains(@class,"t7gRps")]',
       NULL,
       'Sponsored marker — SVG path 안에 raster된 텍스트라 추출 불가. 코드가 element 존재 시 "Sponsored" 강제'),
      ('Flipkart','main',d,'available_quantity_for_purchase',
       './/*[contains(text(),"Only") and contains(text(),"left")]',
       './/div[contains(.,"Only") and contains(.,"left")][not(.//div[contains(.,"Only")])]',
       'e.g. "Only 2 left" — 재고 적은 카드만');
  END LOOP;
END $$;

-- TV / REF / LDY 만 final/original price (HHP 제외)
DO $$
DECLARE
  d TEXT;
  domains TEXT[] := ARRAY['tv','ref','ldy'];
BEGIN
  FOREACH d IN ARRAY domains LOOP
    INSERT INTO dx_siel_xpath_selectors
      (site_account,page_type,domain,data_field,xpath_primary,fallback_xpath,notes)
    VALUES
      ('Flipkart','main',d,'final_sku_price',
       './/div[starts-with(normalize-space(text()),"₹") and not(text()[contains(.,"M.R.P")])][1]',
       './/div[contains(@class,"_30jeq3") or contains(@class,"_3I9_wc")]',
       '최종 판매가'),
      ('Flipkart','main',d,'original_sku_price',
       './/div[starts-with(normalize-space(text()),"₹") and (preceding-sibling::div[contains(@style,"text-decoration") or contains(@class,"strike")] or contains(@class,"strike"))]',
       './/div[contains(@class,"_3I9_wc") or contains(@class,"strike")]',
       'M.R.P. — strike-through');
  END LOOP;
END $$;

-- =============================================================================
-- FLIPKART × BSR  (popularity 정렬)
-- ERD: bsr_rank 만 명시. 코드의 positional counter 가 자동 할당.
-- base_container + product_url 만 시드.
-- =============================================================================

DO $$
DECLARE
  d TEXT;
  domains TEXT[] := ARRAY['hhp','tv','ref','ldy'];
BEGIN
  FOREACH d IN ARRAY domains LOOP
    INSERT INTO dx_siel_xpath_selectors
      (site_account,page_type,domain,data_field,xpath_primary,fallback_xpath,notes)
    VALUES
      ('Flipkart','bsr',d,'base_container',
       '//div[@data-id and .//a[contains(@href,"/p/")]]',
       '//a[contains(@href,"/p/")]/ancestor::div[count(.//a[contains(@href,"/p/")])=1][last()]',
       'Flipkart BSR 카드 = data-id attr div'),
      ('Flipkart','bsr',d,'product_url',
       './/a[contains(@href,"/p/")]',
       NULL,
       'href attr');
  END LOOP;
END $$;

-- =============================================================================
-- FLIPKART × DETAIL  (Product Page)
-- 공통 base + 제품군별 spec
-- =============================================================================

-- 공통 (모든 4 제품군 공유)
DO $$
DECLARE
  d TEXT;
  domains TEXT[] := ARRAY['hhp','tv','ref','ldy'];
BEGIN
  FOREACH d IN ARRAY domains LOOP
    INSERT INTO dx_siel_xpath_selectors
      (site_account,page_type,domain,data_field,xpath_primary,fallback_xpath,notes)
    VALUES
      ('Flipkart','detail',d,'expand_specifications',
       '//button[normalize-space(text())="Read More" or contains(text(),"All Specifications")] | //div[normalize-space(text())="Read More"]',
       NULL,
       'modern Flipkart 는 spec table 페이지에 안 보임 — 보통 매치 X. 무해'),
      ('Flipkart','detail',d,'click_show_all_reviews',
       '//a[contains(@href,"/product-reviews/") and not(contains(@href,"buynow"))]',
       NULL,
       '리뷰 페이지 anchor — Buy now 회피'),
      ('Flipkart','detail',d,'star_rating',
       '(//a[contains(@href,"ratings-reviews-details-page")]//div[@dir="auto"])[1]',
       '//div[@dir="auto" and (string-length(normalize-space(text()))<=4) and (number(text()) = number(text()))][1]',
       '"4.5" — modern Flipkart 는 ratings-reviews-details-page anchor 안 첫 dir=auto div'),
      ('Flipkart','detail',d,'count_of_star_ratings',
       '(//a[contains(@href,"ratings-reviews-details-page")]//div[@dir="auto"])[2]',
       NULL,
       '"| 9,687" 형식 — siel_log.parse_count_of_ratings 가 | 제거 + 숫자만'),
      ('Flipkart','detail',d,'count_of_reviews',
       '//div[number(translate(text()," ,","")) = number(translate(text()," ,",""))][following-sibling::*[contains(text(),"Reviews")] or following::div[1][contains(text(),"Reviews")]]',
       NULL,
       'modern Flipkart 는 보통 비표시 — null 가능. 코드는 None 시 best-effort review 추출'),
      ('Flipkart','detail',d,'delivery_availability',
       '//div[contains(text(),"Delivery by") or contains(text(),"FREE Delivery") or contains(text(),"Free Delivery")][1]',
       NULL,
       'pincode 미선택 시 보통 null'),
      ('Flipkart','detail',d,'detailed_review_content',
       '//span[@class="css-1jxf684" and not(normalize-space(text())="more")]',
       '//div[@dir="auto"]/span[string-length(normalize-space(text()))>5 and not(normalize-space(text())="more")]',
       '리뷰 페이지 navigate 후. body 는 div[dir=auto] > span.css-1jxf684. "more" expand 버튼 제외. max 20'),
      ('Flipkart','detail',d,'retailer_sku_name_similar',
       '//div[normalize-space(text())="Similar Products"]/following::a[contains(@href,"/p/")]//h1 | //div[normalize-space(text())="Similar Products"]/following::a[contains(@href,"/p/")][position()<=10]//div[string-length(normalize-space(text()))>15]',
       NULL,
       'Similar Products 헤딩 다음 a /p/ 카드들의 제목'),
      ('Flipkart','detail',d,'sku',
       '//h1[1]',
       NULL,
       'modern Flipkart spec table 에 안 보임 — h1 전체 텍스트 반환. orchestrator 후처리에서 모델명 파싱');
  END LOOP;
END $$;

-- HHP 전용 (Flipkart)
INSERT INTO dx_siel_xpath_selectors
  (site_account,page_type,domain,data_field,xpath_primary,fallback_xpath,notes)
VALUES
  ('Flipkart','detail','hhp','final_sku_price',
   '(//div[starts-with(normalize-space(text()),"₹") and not(ancestor::a[contains(@href,"/p/")])])[1]',
   '//div[contains(@class,"_30jeq3") or contains(@class,"Nx9bqj")]',
   'HHP: detail 첫 ₹ 가격 (similar product 링크 안 제외)'),
  ('Flipkart','detail','hhp','original_sku_price',
   '(//div[contains(@style,"line-through") and not(ancestor::a[contains(@href,"/p/")])])[1]',
   '//div[contains(@class,"_3I9_wc")]',
   'HHP: M.R.P. — line-through style. modern Flipkart 는 ₹ prefix 없을 수 있음 (예: "33,999")'),
  ('Flipkart','detail','hhp','savings',
   '(//div[contains(text(),"%") and string-length(normalize-space(text()))<=5 and not(ancestor::a[contains(@href,"/p/")])])[1]',
   '//div[contains(text(),"% off")]',
   '"21%" 형식 (modern Flipkart). off 텍스트 없음'),
  ('Flipkart','detail','hhp','trade_in',
   '(//div[normalize-space(text())="Exchange offer"])[1]/following::div[(contains(text(),"Up to") or contains(text(),"₹") or contains(text(),"Off")) and not(contains(text(),"Pincode")) and not(contains(text(),"Servicea"))][1]',
   '//*[contains(text(),"Exchange")]/ancestor::div[1]',
   '두 xpath 결과 공백 1개 두고 합치기 — 후처리 필요'),
  ('Flipkart','detail','hhp','hhp_storage',
   '//h1[1]',
   '//ul/li[contains(text(),"GB ROM") or contains(text(),"GB RAM")][1]',
   'modern Flipkart h1 = "vivo T5x 5G (Star Silver, 256 GB) (8 GB RAM)" — orchestrator 후처리에서 storage 추출'),
  ('Flipkart','detail','hhp','hhp_color',
   '//div[normalize-space(text())="Selected Color:"]/following::div[1]',
   '//h1[1]',
   '"Selected Color:" 라벨 다음 div. fallback h1 색상 부분 후처리');

-- TV 전용
INSERT INTO dx_siel_xpath_selectors
  (site_account,page_type,domain,data_field,xpath_primary,fallback_xpath,notes)
VALUES
  ('Flipkart','detail','tv','savings',
   '//div[contains(text(),"% off")]', NULL, NULL),
  ('Flipkart','detail','tv','model_year',
   '//td[normalize-space(text())="Launch Year"]/following-sibling::td[1]',
   '//tr[.//td[contains(text(),"Year")]]/td[2]', NULL),
  ('Flipkart','detail','tv','screen_size',
   '//td[normalize-space(text())="Display Size"]/following-sibling::td[1]',
   '//tr[.//td[contains(text(),"Display Size") or contains(text(),"Screen Size")]]/td[2]', NULL),
  ('Flipkart','detail','tv','estimated_annual_electricity_use',
   '//td[normalize-space(text())="Power Consumption"]/following-sibling::td[1]',
   '//tr[.//td[contains(text(),"Power")]]/td[2]', NULL);

-- REF 전용
INSERT INTO dx_siel_xpath_selectors
  (site_account,page_type,domain,data_field,xpath_primary,fallback_xpath,notes)
VALUES
  ('Flipkart','detail','ref','ref_refrigerator_type',
   '//td[normalize-space(text())="Type" or normalize-space(text())="Refrigerator Type"]/following-sibling::td[1]',
   '//tr[.//td[contains(text(),"Type")]]/td[2]',
   'Side-by-Side / French Door / Top Mount 등'),
  ('Flipkart','detail','ref','ref_capacity',
   '//td[normalize-space(text())="Capacity"]/following-sibling::td[1]',
   '//tr[.//td[contains(text(),"Capacity")]]/td[2]', NULL);

-- LDY 전용
INSERT INTO dx_siel_xpath_selectors
  (site_account,page_type,domain,data_field,xpath_primary,fallback_xpath,notes)
VALUES
  ('Flipkart','detail','ldy','ldy_loading_type',
   '//td[normalize-space(text())="Function Type" or normalize-space(text())="Loading Type"]/following-sibling::td[1]',
   '//tr[.//td[contains(text(),"Load")]]/td[2]',
   'Function Type 의 끝 "Top Load" / "Front Load" 만 후처리에서 추출'),
  ('Flipkart','detail','ldy','ldy_capacity',
   '//td[normalize-space(text())="Washing Capacity"]/following-sibling::td[1]',
   '//tr[.//td[contains(text(),"Capacity")]]/td[2]', NULL);

-- =============================================================================
-- 확인 쿼리
-- =============================================================================
-- SELECT page_type, domain, COUNT(*) AS n
--   FROM dx_siel_xpath_selectors
--  WHERE site_account = 'Amazon' AND is_active
--  GROUP BY page_type, domain
--  ORDER BY page_type, domain;
