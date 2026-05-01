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
       './/h2//span[normalize-space(text())][1] | .//a[contains(@href,"/dp/")]//span[normalize-space(text())][1]',
       NULL,
       '상품명 — h2 우선 (sponsored + organic 둘 다 매칭, h2 가 title element). dp link 패턴은 fallback union'),
      ('Amazon','main',d,'final_sku_price',
       './/span[@class="a-price" and @data-a-color="base"]//span[@class="a-offscreen"] | .//span[@aria-label="Currently unavailable."]/span | .//span[normalize-space(text())="Currently unavailable."] | .//*[@id="fod-cx-message-with-learn-more"]/span[1] | .//span[normalize-space(text())="No featured offers available"]',
       './/span[contains(@class,"a-price") and not(@data-a-strike)]//span[@class="a-offscreen"]',
       '가격 매치 우선 (DOM 순서). 가격 부재 시 fallback union: (1) "Currently unavailable." (aria-label 또는 text fullmatch), (2) "No featured offers available" (fod-cx-message-with-learn-more id 또는 text fullmatch). variant chooser ("See options") 카드는 valid null — detail 에서 채움'),
      ('Amazon','main',d,'original_sku_price',
       './/span[@class="a-price a-text-price" and @data-a-strike="true"]//span[@class="a-offscreen"]',
       './/span[@data-a-strike="true"]//span[@class="a-offscreen"]',
       'M.R.P. 가격 (할인 전)'),
      ('Amazon','main',d,'discount_type',
       './/*[contains(@id,"DEAL_") and contains(@id,"-label")]//span[contains(@class,"a-badge-text")] | .//*[contains(@id,"DEAL_") and contains(@id,"-label")]/span/span',
       './/span[contains(@class,"s-coupon-clipped")]',
       'Limited time deal / Coupon 등. union: a-badge-text class layout | nested /span/span layout (class 부재). 첫 매치 우선'),
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
  -- sku/item 둘 다 SQL selector 제거 — amzn/detail.py 가 source_url 에서 ASIN 추출해 rec['sku'] 채움
  -- 이유: input[@id="ASIN"] 은 page 의 default variant 따라 dynamic (B0FC5XDV5R url → B0FC5TBWG5 잡힘 사례).
  -- url 의 ASIN 은 절대 변경 안 됨 → stable. TV/REF/LDY sku (Manufacturer Part Number) 와는 다른 의미.
  ('Amazon','detail','hhp','sku_assurance',
   '//*[@id="freeShippingPriceBadging_feature_div"]//span[contains(@class,"a-icon-text-fba")] | //*[@id="freeShippingPriceBadging_feature_div"]//i//span',
   '//*[contains(@id,"shippedBy") or contains(@id,"merchant-info")]//span',
   '"Fulfilled" → 후처리에서 "Amazon Fulfilled" 로 저장 (전 4 도메인 공통)'),
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
   '//*[@id="freeShippingPriceBadging_feature_div"]//span[contains(@class,"a-icon-text-fba")] | //*[@id="freeShippingPriceBadging_feature_div"]//i//span',
   '//*[contains(@id,"shippedBy") or contains(@id,"merchant-info")]//span',
   '"Fulfilled" → 후처리에서 "Amazon Fulfilled" 로 저장'),
  ('Amazon','detail','tv','screen_size',
   '//*[@id="poExpander"]//table//tr[.//td[contains(text(),"Screen Size")]]/td[2] | //table//tr[.//th[contains(text(),"Screen Size")]]/td | //*[@id="poExpander"]/div[1]/div/table/tbody/tr[1]/td[2]',
   NULL,
   'TV screen_size 0/3 NULL 회복 — primary 를 union 화 (fallback 컬럼은 코드가 미사용). poExpander td=Screen Size /td[2] | top-level table th=Screen Size /td | poExpander 첫 row 의 td[2] (positional)'),
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
   '//*[@data-hook="reviewText"] | //div[@data-hook="review-collapsed" or @data-hook="review-body"]//span[not(@class) or contains(@class,"cr-original-review-content")]',
   '//*[@data-hook="reviewTextContainer"]//span',
   'REF 전용 — Amazon refrigerator page DOM 3 variant: A) data-hook=reviewText (camelCase), B) review-collapsed/review-body 안 span no-class (HHP-style India reviews), C) review-collapsed 안 span class=cr-original-review-content (international reviews — Bosch B08F9CDP8M 같은 글로벌 SKU). 메모: feedback_domain_branching_pattern.md'),
  ('Amazon','detail','ref','sku',
   '//table//tr[.//th[contains(text(),"Manufacturer") and contains(text(),"Part Number")]]/td',
   '//table//tr[.//th[contains(text(),"Item model number")]]/td',
   'REF: Manufacturer Part Number'),
  ('Amazon','detail','ref','sku_assurance',
   '//*[@id="freeShippingPriceBadging_feature_div"]//span[contains(@class,"a-icon-text-fba")] | //*[@id="freeShippingPriceBadging_feature_div"]//i//span',
   '//*[contains(@id,"shippedBy") or contains(@id,"merchant-info")]//span',
   '"Fulfilled" → 후처리에서 "Amazon Fulfilled" 로 저장 (전 4 도메인 공통)'),
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
  ('Amazon','detail','ldy','sku_assurance',
   '//*[@id="freeShippingPriceBadging_feature_div"]//span[contains(@class,"a-icon-text-fba")] | //*[@id="freeShippingPriceBadging_feature_div"]//i//span',
   '//*[contains(@id,"shippedBy") or contains(@id,"merchant-info")]//span',
   '"Fulfilled" → 후처리에서 "Amazon Fulfilled" 로 저장 (전 4 도메인 공통)'),
  ('Amazon','detail','ldy','ldy_loading_type',
   '//table//tr[.//th[contains(text(),"Access Location") or contains(text(),"Loading Type") or contains(text(),"Configuration")]]/td',
   NULL,
   'e.g. "Top load" / "Front load"'),
  ('Amazon','detail','ldy','ldy_capacity',
   '//table//tr[.//th[contains(text(),"Capacity")]]/td | //div[@id="detailBullets_feature_div"]//li[.//span[contains(text(),"Capacity")]]/span[2] | //div[@id="productOverview_feature_div"]//table//tr[.//td[contains(text(),"Capacity")]]/td[2]',
   '//table//tr[.//th[contains(text(),"Washing Capacity") or contains(text(),"Total Capacity") or contains(text(),"Drum Capacity")]]/td',
   'union: table | detailBullets | poExpander. fallback 에 라벨 variation. e.g. "8kg"');

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
       './/*[contains(text(),"Hot deal") or contains(text(),"Super Deals") or contains(text(),"Bank offer") or contains(text(),"Bank Offer") or contains(text(),"Limited") or contains(text(),"Big Saving") or contains(text(),"Coupon") or contains(text(),"Special") or contains(text(),"Deal of") or contains(text(),"Trending")]',
       NULL,
       '사용자 정책: Super Deals / Bank offer / Hot deal / Limited 등 모두 deal type 으로 수집. % off 는 savings 별도'),
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
       'ERD: Main Page. 카드 안 "1,573 Reviews" — siel_log.parse_count_of_reviews 가 "Reviews" 앞 숫자만'),
      ('Flipkart','main',d,'star_rating',
       './/div[(string-length(normalize-space(text()))=3) and (substring(normalize-space(text()),2,1)=".") and (number(text())=number(text()))][1]',
       NULL,
       'ERD reference R56: Main Page 별점. 카드 안 "X.X" 패턴 (length=3, 가운데 dot, 유효 숫자). siel_log.parse_star_rating 후처리');
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
      ('Flipkart','detail',d,'expand_see_more',
       '//div[normalize-space(text())="Specifications" and not(ancestor::head)]/following::div[normalize-space(text())="See more"][1]',
       '(//div[normalize-space(text())="See more"])[1]',
       'Specifications 클릭 후 deep spec 영역 (Power Features 등) lazy load 트리거. Specifications 헤더 다음 첫 See more div'),
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
       '//div[normalize-space(text())="Delivery"]/parent::div',
       '//div[contains(text(),"Delivery by") or contains(text(),"FREE Delivery") or contains(text(),"Free Delivery")][1]',
       'parent div 의 .text = "Delivery by 4 May, Mon" — child label "Delivery" + sibling "by 4 May, Mon" 합성'),
      ('Flipkart','detail',d,'detailed_review_content',
       '//span[@class="css-1jxf684" and not(normalize-space(text())="more")]',
       '//div[@dir="auto"]/span[string-length(normalize-space(text()))>5 and not(normalize-space(text())="more")]',
       '리뷰 페이지 navigate 후. body 는 div[dir=auto] > span.css-1jxf684. "more" expand 버튼 제외. count_of_reviews 만큼 수집 (최대 20) — fpkt/detail.py 가 &page=2,3 누적'),
      ('Flipkart','detail',d,'retailer_sku_name_similar',
       '//a[contains(@href,"hl_lid=")]//div[contains(@style,"text-overflow: ellipsis") and (contains(text(),"GB") or contains(text(),"TB")) and not(contains(text(),"₹"))]',
       '//a[contains(@href,"productRecommendation")]//div[contains(@style,"text-overflow: ellipsis") and contains(text(),"GB")]',
       'ERD: similar products 제품명만. similar/recommendation anchor 는 href 에 hl_lid= 포함 (recommendation marker). ellipsis div 중 GB 포함 + ₹ 제외'),
      ('Flipkart','detail',d,'sku',
       '//div[normalize-space(text())="Model Name"]/following-sibling::div[1]',
       '//h1[1]',
       'ERD: All details > Specifications > General > Model Name 바로 아래 텍스트. expand_specifications click 후 spec 영역에 노출 — fallback h1 전체 (post-process)');
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
   '//div[contains(text(),"GB ROM") or contains(text(),"TB ROM")][1]',
   '//div[contains(text(),"GB RAM") and contains(text(),"ROM")][1]',
   'ERD: Product highlights 최상단 "4 GB RAM | 64 GB ROM" → siel_log.parse_hhp_storage 가 ROM 앞 단위 추출 ("64 GB")'),
  ('Flipkart','detail','hhp','hhp_color',
   '//div[normalize-space(text())="Color"]/following-sibling::div[1]',
   '//div[normalize-space(text())="Selected Color:"]/following::div[1]',
   'ERD: Specifications > General > Color 바로 아래 div. expand_specifications click 후 spec 에 노출');

-- TV 전용 (modern Flipkart DOM: <div>label:</div><div>value</div> 형제 패턴, td/tr 아님)
INSERT INTO dx_siel_xpath_selectors
  (site_account,page_type,domain,data_field,xpath_primary,fallback_xpath,notes)
VALUES
  ('Flipkart','detail','tv','savings',
   '(//div[contains(text(),"%") and string-length(normalize-space(text()))<=5 and not(ancestor::a[contains(@href,"/p/")])])[1]',
   '//div[contains(text(),"% off")]',
   'modern Flipkart detail: "X%" (off 없음). HHP 패턴 동일. siel_log.parse_savings 가 trailing off 제거'),
  ('Flipkart','detail','tv','model_year',
   '//div[normalize-space(text())="Launch Year:" or normalize-space(text())="Launch Year"]/following-sibling::div[1]',
   '//td[normalize-space(text())="Launch Year"]/following-sibling::td[1]',
   'highlights spec (콜론 포함) + deep spec (콜론 없음 — See more 후 표기) union'),
  ('Flipkart','detail','tv','screen_size',
   '//div[normalize-space(text())="Display Size:" or normalize-space(text())="Display Size"]/following-sibling::div[1]',
   '//td[normalize-space(text())="Display Size"]/following-sibling::td[1]',
   'highlights spec (콜론 포함) + deep spec (콜론 없음) union'),
  ('Flipkart','detail','tv','estimated_annual_electricity_use',
   '//div[normalize-space(text())="Power Consumption" or normalize-space(text())="Annual Energy Consumption" or normalize-space(text())="Energy Consumption" or normalize-space(text())="Power Consumption:" or normalize-space(text())="Annual Energy Consumption:"]/following-sibling::div[1]',
   '//td[normalize-space(text())="Power Consumption"]/following-sibling::td[1]',
   'deep spec (Power Features 그룹 — See more 후 lazy load). 라벨 콜론 없음 (highlights 와 다름). value 단위 (Standby W vs kWh/Year) 사이트별 의미 mismatch 가능 — raw 그대로 저장 후 분석 단계 분기');

-- REF 전용 (Flipkart) — ERD v1: 가격 3종 Main Page 로 통합. REF 는 savings 자체 ERD 에 정의 없음. detail 엔 spec 2종만.
-- TV 패턴 따라 콜론 + 콜론없음 union (highlights + deep spec). "Type" 라벨은 도어타입이라 제외 — "Refrigerator Type" 만 사용.
-- ref_capacity 는 Specifications 헤딩 이후로 scope — icon/highlights 영역의 stray "Capacity" div (예: Samsung 301L 이 4.4 별점값 매치) 차단.
INSERT INTO dx_siel_xpath_selectors
  (site_account,page_type,domain,data_field,xpath_primary,fallback_xpath,notes)
VALUES
  ('Flipkart','detail','ref','ref_refrigerator_type',
   '//div[normalize-space(text())="Refrigerator Type:" or normalize-space(text())="Refrigerator Type"]/following-sibling::div[1]',
   '//td[normalize-space(text())="Refrigerator Type"]/following-sibling::td[1]',
   'TV 패턴: highlights (콜론) + deep spec (콜론 없음) union. "Top Freezer Refrigerator" / "Side-by-Side" 등. 라벨 unique 라 stray 위험 낮음'),
  ('Flipkart','detail','ref','ref_capacity',
   '(//div[normalize-space(text())="Specifications"]/following::div[normalize-space(text())="Capacity:" or normalize-space(text())="Capacity"])[1]/following-sibling::div[1]',
   '//td[normalize-space(text())="Capacity"]/following-sibling::td[1]',
   'Specifications 헤딩 이후 first Capacity div 의 sibling. icon/highlights 영역의 stray Capacity 차단 (Samsung 301L 4.4 별점 stray 케이스 방지). expand 실패 product 는 valid null');

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
