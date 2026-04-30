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
-- AMAZON × DETAIL — 4 도메인 (HHP/TV/REF/LDY)
-- 도메인별 per-domain INSERT 유지 (회귀 위험 0).
-- 통합 (DO $$ FOREACH) 은 ALL 도메인 검증 후에만 — 검증 안 된 도메인 통합 금지.
-- HHP path 는 신성불가침 — TV/REF/LDY 가 안 맞으면 그 도메인만 분기 추가.
-- 메모: feedback_domain_branching_pattern.md
-- =============================================================================

-- =============================================================================
-- AMAZON × DETAIL × HHP  (Product Page, smartphone) — 검증 완료
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
   'rollback: dp link narrow over-narrow (carousel-card=Bank Offer 카드라 dp link 부재). 옛 broad union 복구. 노이즈는 후처리'),
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
   '//div[@data-hook="review-collapsed" or @data-hook="review-body"]//span[not(@class)] | //div[@data-hook="reviewRichContentContainer"]',
   '//div[contains(@id,"customer_review-")]//span[@data-hook="review-body"]',
   'Amazon HHP 도 review widget A/B 마이그레이션 영향 — full-run 247/400 회귀 (3d4e280 retry 강화로도 복구 안 됨). 옛 review-collapsed/body | 신 cr-top-reviews carousel reviewRichContentContainer union. superset 이라 옛 케이스 회귀 0'),
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
-- AMAZON × DETAIL × TV  — 미검증, HHP 와 동일 selector 가정 (검증 후 결정)
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
   '//div[@id="deliveryBlockMessage"]//span', NULL),
  ('Amazon','detail','tv','fastest_delivery',
   '//*[contains(@class,"udm-delivery-")]//span[contains(text(),"fastest")] | //*[@id="mir-layout-DELIVERY_BLOCK-slot-SECONDARY_DELIVERY_MESSAGE_LARGE"]//span[1]',
   NULL, 'udm-delivery (신) | mir-layout (구) union'),
  ('Amazon','detail','tv','inventory_status',
   '//*[@id="availability"]//span',
   '//div[@id="availability"]//span', NULL),
  ('Amazon','detail','tv','retailer_sku_name_similar',
   '//*[contains(@id,"anonCarousel")]//li//a/span[1] | //*[@id="anonCarousel2"]//li//a[contains(@class,"a-link-normal")]//div[contains(@class,"a-truncate-full")]',
   '//*[contains(@id,"sims-fbt")]//div[contains(@class,"a-truncate-full")]',
   'broad union — anonCarousel li a span[1] | anonCarousel2 a-link-normal a-truncate-full. 노이즈 후처리'),
  ('Amazon','detail','tv','star_rating',
   '//*[@data-hook="rating-out-of-text"] | //*[@id="acrPopover"]//span[@class="a-size-base a-color-base"]',
   '//*[@id="cm_cr_dp_d_rating_histogram"]//div[contains(@class,"a-section")]/div/div/span/span',
   'data-hook="rating-out-of-text" 우선'),
  ('Amazon','detail','tv','count_of_star_ratings',
   '//*[@id="acrCustomerReviewText"]',
   '//*[@id="cm_cr_dp_d_rating_histogram"]//span[contains(text(),"global ratings")]', NULL),
  ('Amazon','detail','tv','summarized_review_content',
   '//*[@data-testid="overall-summary"] | //div[@data-hook="cr-insights-widget"]//span | //*[@id="reviewsMedley"]//div[contains(@id,"review-summary")]//span',
   '//div[@data-hook="cr-summarization-attributes-list"]//span',
   '리뷰 AI 요약 — overall-summary (신 testid) | cr-insights-widget | reviewsMedley union'),
  ('Amazon','detail','tv','detailed_review_content',
   '//div[@data-hook="review-collapsed" or @data-hook="review-body"]//span[not(@class)] | //div[@data-hook="reviewRichContentContainer"]',
   '//div[contains(@id,"customer_review-")]//span[@data-hook="review-body"]',
   'TV review widget A/B 마이그레이션 — 옛 review-collapsed/body | 신 cr-top-reviews carousel reviewRichContentContainer. load 마다 둘 중 하나만 옴'),
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
-- AMAZON × DETAIL × REF  — 미검증, HHP 와 동일 selector 가정 (검증 후 결정)
-- =============================================================================

INSERT INTO dx_siel_xpath_selectors
  (site_account, page_type, domain, data_field, xpath_primary, fallback_xpath, notes)
VALUES
  ('Amazon','detail','ref','expand_item_details',
   '//a[contains(@class,"a-expander-prompt") and contains(text(),"See more")]',
   NULL,
   'Item details 섹션 펼치기'),
  ('Amazon','detail','ref','delivery_availability',
   '//*[@id="mir-layout-DELIVERY_BLOCK-slot-PRIMARY_DELIVERY_MESSAGE_LARGE"]//span[1]',
   '//div[@id="deliveryBlockMessage"]//span', NULL),
  ('Amazon','detail','ref','fastest_delivery',
   '//*[contains(@class,"udm-delivery-")]//span[contains(text(),"fastest")] | //*[@id="mir-layout-DELIVERY_BLOCK-slot-SECONDARY_DELIVERY_MESSAGE_LARGE"]//span[1]',
   NULL, 'udm-delivery (신) | mir-layout (구) union'),
  ('Amazon','detail','ref','inventory_status',
   '//*[@id="availability"]//span',
   '//div[@id="availability"]//span', NULL),
  ('Amazon','detail','ref','retailer_sku_name_similar',
   '//*[contains(@id,"anonCarousel")]//li//a/span[1] | //*[@id="anonCarousel2"]//li//a[contains(@class,"a-link-normal")]//div[contains(@class,"a-truncate-full")]',
   '//*[contains(@id,"sims-fbt")]//div[contains(@class,"a-truncate-full")]',
   'broad union — anonCarousel li a span[1] | anonCarousel2 a-link-normal a-truncate-full. 노이즈 후처리'),
  ('Amazon','detail','ref','star_rating',
   '//*[@data-hook="rating-out-of-text"] | //*[@id="acrPopover"]//span[@class="a-size-base a-color-base"]',
   '//*[@id="cm_cr_dp_d_rating_histogram"]//div[contains(@class,"a-section")]/div/div/span/span',
   'data-hook="rating-out-of-text" 우선'),
  ('Amazon','detail','ref','count_of_star_ratings',
   '//*[@id="acrCustomerReviewText"]',
   '//*[@id="cm_cr_dp_d_rating_histogram"]//span[contains(text(),"global ratings")]', NULL),
  ('Amazon','detail','ref','summarized_review_content',
   '//*[@data-testid="overall-summary"] | //div[@data-hook="cr-insights-widget"]//span | //*[@id="reviewsMedley"]//div[contains(@id,"review-summary")]//span',
   '//div[@data-hook="cr-summarization-attributes-list"]//span',
   '리뷰 AI 요약 — overall-summary (신 testid) | cr-insights-widget | reviewsMedley union'),
  ('Amazon','detail','ref','detailed_review_content',
   '//*[@data-hook="reviewText"] | //div[@data-hook="review-collapsed" or @data-hook="review-body"]//span[not(@class)]',
   '//*[@data-hook="reviewTextContainer"]//span',
   'REF 전용 — Amazon refrigerator page DOM 2 variant flicker (같은 URL 도 visit 마다 markup 변동): A) data-hook=reviewText (camelCase), B) data-hook=review-collapsed/review-body (HHP-style). union 으로 둘 다 catch. 메모: feedback_domain_branching_pattern.md'),
  ('Amazon','detail','ref','sku',
   '//table//tr[.//th[contains(text(),"Manufacturer") and contains(text(),"Part Number")]]/td',
   '//table//tr[.//th[contains(text(),"Item model number")]]/td',
   'REF: Manufacturer Part Number'),
  ('Amazon','detail','ref','ref_refrigerator_type',
   '//table//tr[.//th[contains(text(),"Configuration") or contains(text(),"Refrigerator Type")]]/td',
   '//table//tr[.//th[contains(text(),"Style")]]/td',
   'e.g. Side-by-Side, French Door, Top Mount'),
  ('Amazon','detail','ref','ref_capacity',
   '//table//tr[.//th[contains(text(),"Capacity")]]/td',
   '//table//tr[.//th[contains(text(),"Total Capacity") or contains(text(),"Capacity (Litres)")]]/td',
   'e.g. "300L"');

-- =============================================================================
-- AMAZON × DETAIL × LDY (laundry / 세탁기)  — 미검증, HHP 와 동일 selector 가정
-- =============================================================================

INSERT INTO dx_siel_xpath_selectors
  (site_account, page_type, domain, data_field, xpath_primary, fallback_xpath, notes)
VALUES
  ('Amazon','detail','ldy','expand_item_details',
   '//a[contains(@class,"a-expander-prompt") and contains(text(),"See more")]',
   NULL,
   'Item details 섹션 펼치기'),
  ('Amazon','detail','ldy','delivery_availability',
   '//*[@id="mir-layout-DELIVERY_BLOCK-slot-PRIMARY_DELIVERY_MESSAGE_LARGE"]//span[1]',
   '//div[@id="deliveryBlockMessage"]//span', NULL),
  ('Amazon','detail','ldy','fastest_delivery',
   '//*[contains(@class,"udm-delivery-")]//span[contains(text(),"fastest")] | //*[@id="mir-layout-DELIVERY_BLOCK-slot-SECONDARY_DELIVERY_MESSAGE_LARGE"]//span[1]',
   NULL, 'udm-delivery (신) | mir-layout (구) union'),
  ('Amazon','detail','ldy','inventory_status',
   '//*[@id="availability"]//span',
   '//div[@id="availability"]//span', NULL),
  ('Amazon','detail','ldy','retailer_sku_name_similar',
   '//*[contains(@id,"anonCarousel")]//li//a/span[1] | //*[@id="anonCarousel2"]//li//a[contains(@class,"a-link-normal")]//div[contains(@class,"a-truncate-full")]',
   '//*[contains(@id,"sims-fbt")]//div[contains(@class,"a-truncate-full")]',
   'broad union — anonCarousel li a span[1] | anonCarousel2 a-link-normal a-truncate-full. 노이즈 후처리'),
  ('Amazon','detail','ldy','star_rating',
   '//*[@data-hook="rating-out-of-text"] | //*[@id="acrPopover"]//span[@class="a-size-base a-color-base"]',
   '//*[@id="cm_cr_dp_d_rating_histogram"]//div[contains(@class,"a-section")]/div/div/span/span',
   'data-hook="rating-out-of-text" 우선'),
  ('Amazon','detail','ldy','count_of_star_ratings',
   '//*[@id="acrCustomerReviewText"]',
   '//*[@id="cm_cr_dp_d_rating_histogram"]//span[contains(text(),"global ratings")]', NULL),
  ('Amazon','detail','ldy','summarized_review_content',
   '//*[@data-testid="overall-summary"] | //div[@data-hook="cr-insights-widget"]//span | //*[@id="reviewsMedley"]//div[contains(@id,"review-summary")]//span',
   '//div[@data-hook="cr-summarization-attributes-list"]//span',
   '리뷰 AI 요약 — overall-summary (신 testid) | cr-insights-widget | reviewsMedley union'),
  ('Amazon','detail','ldy','detailed_review_content',
   '//*[@data-hook="reviewText"] | //div[@data-hook="review-collapsed" or @data-hook="review-body"]//span[not(@class)]',
   '//*[@data-hook="reviewTextContainer"]//span | //div[contains(@id,"customer_review-")]//span[@data-hook="review-body"]',
   'LDY 전용 — Amazon laundry page 의 review markup 이 시점에 따라 변동: reviewText (REF 패턴) 또는 review-collapsed/review-body (HHP 패턴). 둘 다 union 으로 cover. 메모: feedback_domain_branching_pattern.md'),
  ('Amazon','detail','ldy','sku',
   '//table//tr[.//th[contains(text(),"Manufacturer") and contains(text(),"Part Number")]]/td',
   '//table//tr[.//th[contains(text(),"Item model number")]]/td',
   'LDY: Manufacturer Part Number'),
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
       './/*[contains(text(),"Limited") or contains(text(),"Hot deal") or contains(text(),"Big Saving") or contains(text(),"Coupon") or contains(text(),"Bank Offer")]',
       NULL,
       'promotion 표시만 — % off 는 savings 에서 별도 수집 (분리)'),
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
       'e.g. "Only 2 left" — 재고 적은 카드만'),
      ('Flipkart','main',d,'count_of_star_ratings',
       './/span[contains(text(),"Ratings")]',
       NULL,
       'ERD: Main Page. 카드 안 "33,837 Ratings" — siel_log.parse_count_of_ratings 가 "Ratings" 제거 + 숫자만'),
      ('Flipkart','main',d,'count_of_reviews',
       './/span[contains(text(),"Reviews")]',
       NULL,
       'ERD: Main Page. 카드 안 "1,573 Reviews" — siel_log.parse_count_of_reviews 가 "Reviews" 앞 숫자만');
  END LOOP;
END $$;

-- ERD v1 갱신본: 4 도메인 공통 final/original price (Main Page). 이전엔 tv,ref,ldy 만이었음.
DO $$
DECLARE
  d TEXT;
  domains TEXT[] := ARRAY['hhp','tv','ref','ldy'];
BEGIN
  FOREACH d IN ARRAY domains LOOP
    INSERT INTO dx_siel_xpath_selectors
      (site_account,page_type,domain,data_field,xpath_primary,fallback_xpath,notes)
    VALUES
      ('Flipkart','main',d,'final_sku_price',
       './/div[starts-with(normalize-space(text()),"₹")][1]',
       NULL,
       'modern Flipkart 카드 안 첫 ₹ div = 최종 판매가'),
      ('Flipkart','main',d,'original_sku_price',
       './/div[starts-with(normalize-space(text()),"₹")][1]/following-sibling::div[1][starts-with(normalize-space(text()),"₹")]',
       NULL,
       'M.R.P. — first ₹ div 의 직접 sibling 첫 ₹ (없으면 null = 할인 없는 product)');
  END LOOP;
END $$;

-- ERD v1 row 58: savings 는 HHP+TV 만 Main Page (REF/LDY 는 savings 자체 정의 없음)
DO $$
DECLARE
  d TEXT;
  domains TEXT[] := ARRAY['hhp','tv'];
BEGIN
  FOREACH d IN ARRAY domains LOOP
    INSERT INTO dx_siel_xpath_selectors
      (site_account,page_type,domain,data_field,xpath_primary,fallback_xpath,notes)
    VALUES
      ('Flipkart','main',d,'savings',
       './/*[contains(text(),"% off")]',
       './/div[contains(text(),"%") and string-length(normalize-space(text()))<=5]',
       'ERD: Main Page (HHP+TV). e.g. "21% off"');
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
       '//div[normalize-space(text())="Specifications" and not(ancestor::head)]',
       NULL,
       'modern Flipkart 의 Specifications 토글 버튼 — div 자체가 click target. meta head 의 검색 텍스트 제외'),
      ('Flipkart','detail',d,'click_show_all_reviews',
       '//a[contains(@href,"/product-reviews/") and not(contains(@href,"buynow"))]',
       NULL,
       '리뷰 페이지 anchor — Buy now 회피'),
      ('Flipkart','detail',d,'star_rating',
       '(//a[contains(@href,"ratings-reviews-details-page")]//div[@dir="auto"])[1]',
       '//div[@dir="auto" and (string-length(normalize-space(text()))<=4) and (number(text()) = number(text()))][1]',
       '"4.5" — modern Flipkart 는 ratings-reviews-details-page anchor 안 첫 dir=auto div'),
      -- count_of_star_ratings / count_of_reviews 는 ERD 기준 Main Page 에서 수집 (이전엔 detail 에 정의했으나 ERD 어긋남 — 2026-04-30 이전).
      ('Flipkart','detail',d,'delivery_availability',
       '//div[contains(text(),"Delivery by") or contains(text(),"FREE Delivery") or contains(text(),"Free Delivery") or contains(text(),"Pincode not Serviceable") or contains(text(),"not Serviceable")][1]',
       '//div[contains(text(),"Pincode") and contains(text(),"Serviceable")]',
       'default pincode 미달 시 "Pincode not Serviceable" — 그 자체가 valid delivery status'),
      ('Flipkart','detail',d,'detailed_review_content',
       '//span[@class="css-1jxf684" and not(normalize-space(text())="more")]',
       '//div[@dir="auto"]/span[string-length(normalize-space(text()))>5 and not(normalize-space(text())="more")]',
       '리뷰 페이지 navigate 후. body 는 div[dir=auto] > span.css-1jxf684. "more" expand 버튼 제외. count_of_reviews 만큼 수집 (최대 20) — fpkt/detail.py 가 &page=2,3 누적'),
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

-- HHP 전용 (Flipkart) — ERD v1: 가격 3종 (final/original/savings) 은 Main Page 로 통합. detail 엔 trade_in/storage/color 만.
INSERT INTO dx_siel_xpath_selectors
  (site_account,page_type,domain,data_field,xpath_primary,fallback_xpath,notes)
VALUES
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

-- TV 전용 (modern Flipkart DOM: <div>label:</div><div>value</div> 형제 패턴, td/tr 아님)
INSERT INTO dx_siel_xpath_selectors
  (site_account,page_type,domain,data_field,xpath_primary,fallback_xpath,notes)
VALUES
  ('Flipkart','detail','tv','savings',
   '(//div[contains(text(),"%") and string-length(normalize-space(text()))<=5 and not(ancestor::a[contains(@href,"/p/")])])[1]',
   '//div[contains(text(),"% off")]',
   'modern Flipkart detail: "X%" (off 없음). HHP 패턴 동일. siel_log.parse_savings 가 trailing off 제거'),
  ('Flipkart','detail','tv','model_year',
   '//div[normalize-space(text())="Launch Year:"]/following-sibling::div[1]',
   '//td[normalize-space(text())="Launch Year"]/following-sibling::td[1]',
   'modern Flipkart spec: <div>Launch Year:</div><div>VALUE</div>. 콜론 포함. fallback td/tr 보존'),
  ('Flipkart','detail','tv','screen_size',
   '//div[normalize-space(text())="Display Size:"]/following-sibling::div[1]',
   '//td[normalize-space(text())="Display Size"]/following-sibling::td[1]',
   'modern Flipkart spec. Display Size: 만 사용 (Screen Size 라벨 없음)'),
  ('Flipkart','detail','tv','estimated_annual_electricity_use',
   '//div[normalize-space(text())="Power Consumption:" or normalize-space(text())="Annual Energy Consumption:" or normalize-space(text())="Energy Consumption:"]/following-sibling::div[1]',
   '//td[normalize-space(text())="Power Consumption"]/following-sibling::td[1]',
   'modern Flipkart spec union 3종. 일부 TV 표기 자체 없음 — valid NULL 가능');

-- REF 전용 (Flipkart) — ERD v1: 가격 3종 Main Page 로 통합. REF 는 savings 자체 ERD 에 정의 없음. detail 엔 spec 2종만.
-- modern Flipkart React: <div>label</div><div>value</div> sibling. 콜론 없음 (LDY 와 동일 패턴).
-- "Type" 라벨은 도어타입(Double Door/Single Door) 이라 제외 — "Refrigerator Type" 만 사용.
INSERT INTO dx_siel_xpath_selectors
  (site_account,page_type,domain,data_field,xpath_primary,fallback_xpath,notes)
VALUES
  ('Flipkart','detail','ref','ref_refrigerator_type',
   '//div[normalize-space(text())="Refrigerator Type"]/following-sibling::div[1]',
   '//td[normalize-space(text())="Refrigerator Type"]/following-sibling::td[1]',
   'modern Flipkart spec div 패턴. "Top Freezer Refrigerator" / "Side-by-Side" / "French Door" 등. fallback td 보존'),
  ('Flipkart','detail','ref','ref_capacity',
   '//div[normalize-space(text())="Capacity"]/following-sibling::div[1]',
   '//td[normalize-space(text())="Capacity"]/following-sibling::td[1]',
   'modern Flipkart spec div 패턴. "467 L" 형식. fallback td 보존');

-- LDY 전용 (modern Flipkart DOM: <div>label</div><div>value</div> 형제 패턴, 콜론 없음 — TV 와 다름)
INSERT INTO dx_siel_xpath_selectors
  (site_account,page_type,domain,data_field,xpath_primary,fallback_xpath,notes)
VALUES
  ('Flipkart','detail','ldy','ldy_loading_type',
   '//div[normalize-space(text())="Function Type" or normalize-space(text())="Loading Type"]/following-sibling::div[1]',
   '//td[normalize-space(text())="Function Type" or normalize-space(text())="Loading Type"]/following-sibling::td[1]',
   'modern Flipkart spec: <div>Function Type</div><div>VALUE</div>. 콜론 없음 (TV 는 콜론 있음). fallback td 보존'),
  ('Flipkart','detail','ldy','ldy_capacity',
   '//div[normalize-space(text())="Washing Capacity"]/following-sibling::div[1]',
   '//td[normalize-space(text())="Washing Capacity"]/following-sibling::td[1]',
   'modern Flipkart spec div 패턴. fallback td 보존');

-- =============================================================================
-- 확인 쿼리
-- =============================================================================
-- SELECT page_type, domain, COUNT(*) AS n
--   FROM dx_siel_xpath_selectors
--  WHERE site_account = 'Amazon' AND is_active
--  GROUP BY page_type, domain
--  ORDER BY page_type, domain;
