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

-- 데이터 보존 — DROP 금지. INSERT 는 ON CONFLICT 로 update.

CREATE TABLE IF NOT EXISTS dx_siel_xpath_selectors (
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

CREATE INDEX IF NOT EXISTS idx_dx_siel_xpath_lookup ON dx_siel_xpath_selectors
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
       './/div[@aria-hidden and starts-with(@aria-hidden, "M.R.P:")]//span[@class="a-offscreen"]',
       NULL,
       'M.R.P. 가격 (할인 전) — 5/9 정밀화. variant carousel sub-card 의 strike-through 결함 (B0G3X99DLF realme P4X 사례) 회피. 정상 메인 가격은 항상 div[@aria-hidden="M.R.P: ..."] 안. variant/sub-element 는 그 외부 → 매치 X = OSP 정상 NULL (할인 없음). fallback 제거 (broad data-a-strike 가 결함 root cause)'),
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
       'e.g. "2K+ bought in past month"')
    ON CONFLICT (site_account, page_type, domain, data_field) DO UPDATE SET
      xpath_primary  = EXCLUDED.xpath_primary,
      fallback_xpath = EXCLUDED.fallback_xpath,
      notes          = EXCLUDED.notes,
      is_active      = TRUE,
      updated_at     = NOW();
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
       'href attr — BSR 의 ASIN 은 url 에서 추출 가능')
    ON CONFLICT (site_account, page_type, domain, data_field) DO UPDATE SET
      xpath_primary  = EXCLUDED.xpath_primary,
      fallback_xpath = EXCLUDED.fallback_xpath,
      notes          = EXCLUDED.notes,
      is_active      = TRUE,
      updated_at     = NOW();
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
   '"Trade-in and save" / "With Exchange Up to ..."')
    ON CONFLICT (site_account, page_type, domain, data_field) DO UPDATE SET
      xpath_primary  = EXCLUDED.xpath_primary,
      fallback_xpath = EXCLUDED.fallback_xpath,
      notes          = EXCLUDED.notes,
      is_active      = TRUE,
      updated_at     = NOW();

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
   '//table//tr[.//th[normalize-space()="Model Number" or normalize-space()="Part Number" or normalize-space()="Item Model Number" or normalize-space()="Item model number" or normalize-space()="Item Part Number" or normalize-space()="Item part number"]]/td',
   'TV: Manufacturer Part Number primary. fallback union (Model Number | Part Number | Item Model Number | Item model number | Item Part Number | Item part number) — 5/8 진단 (SANSUI Model Number, Midea Part Number) + 5/9 진단 (LDY spam ND-95/IKERIA005/MWM-96055 — Item part number 라벨)'),
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
   NULL, NULL)
    ON CONFLICT (site_account, page_type, domain, data_field) DO UPDATE SET
      xpath_primary  = EXCLUDED.xpath_primary,
      fallback_xpath = EXCLUDED.fallback_xpath,
      notes          = EXCLUDED.notes,
      is_active      = TRUE,
      updated_at     = NOW();

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
   '//table//tr[.//th[normalize-space()="Model Number" or normalize-space()="Part Number" or normalize-space()="Item Model Number" or normalize-space()="Item model number" or normalize-space()="Item Part Number" or normalize-space()="Item part number"]]/td',
   'REF: Manufacturer Part Number primary. fallback union (Model Number | Part Number | Item Model Number | Item model number | Item Part Number | Item part number) — 5/8 진단 (Midea Part Number) + 5/9 진단 (LDY spam Item part number 라벨 — REF 도 같은 패턴 가능성)'),
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
   'e.g. "300L"')
    ON CONFLICT (site_account, page_type, domain, data_field) DO UPDATE SET
      xpath_primary  = EXCLUDED.xpath_primary,
      fallback_xpath = EXCLUDED.fallback_xpath,
      notes          = EXCLUDED.notes,
      is_active      = TRUE,
      updated_at     = NOW();

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
   '//table//tr[.//th[normalize-space()="Model Number" or normalize-space()="Part Number" or normalize-space()="Item Model Number" or normalize-space()="Item model number" or normalize-space()="Item Part Number" or normalize-space()="Item part number"]]/td',
   'LDY: Manufacturer Part Number primary. fallback union (Model Number | Part Number | Item Model Number | Item model number | Item Part Number | Item part number) — 5/8 진단 + 5/9 spam 진단 (3 표본 ND-95/IKERIA005/MWM-96055 모두 "Item part number" 라벨 사용 → 추가). 정상 SKU 형식, 데이터 채움 가치 있음'),
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
   'union: table | detailBullets | poExpander. fallback 에 라벨 variation. e.g. "8kg"')
    ON CONFLICT (site_account, page_type, domain, data_field) DO UPDATE SET
      xpath_primary  = EXCLUDED.xpath_primary,
      fallback_xpath = EXCLUDED.fallback_xpath,
      notes          = EXCLUDED.notes,
      is_active      = TRUE,
      updated_at     = NOW();

-- =============================================================================
-- AMAZON × DETAIL — listing 컬럼 4개 (4 도메인 공통)
-- bsr-only 카드의 listing 컬럼이 비어있을 때 detail page 에서 fallback 추출.
-- insert_test_retail_com.merge 가 primary (main/bsr) 비어있으면 detail 으로 fallback.
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
      ('Amazon','detail',d,'retailer_sku_name',
       '//*[@id="productTitle"]',
       '//h1[@id="title"]//span[normalize-space(text())]',
       'detail page 제품명 — bsr 카드 listing 컬럼 비어있을 때 fallback'),
      ('Amazon','detail',d,'final_sku_price',
       '(//div[@id="corePriceDisplay_desktop_feature_div"]//span[@id="apex-pricetopay-accessibility-label"])[1]',
       '(//div[@id="corePriceDisplay_desktop_feature_div"]//span[@class="a-price" and @data-a-color="base"]//span[@class="a-offscreen"])[1] | (//div[@id="centerCol"]//span[@class="a-price" and @data-a-color="base"]//span[@class="a-offscreen"])[1] | (//*[@id="outOfStock"]//span[contains(@class,"a-color-price")])[1] | (//*[@id="fod-cx-message-with-learn-more"]/span)[1]',
       '신 layout: apex-pricetopay-accessibility-label text (₹X.00 with N percent savings) — siel_log.parse_amzn_apex_price 로 ₹X 만 추출. fallback union: 구 a-offscreen layout | outOfStock div ("Currently unavailable.") | fod-cx-message ("No featured offers available") — main selector 와 동일 위치, detail HTML 4 sample 검증. variant carousel 회피 위해 corePriceDisplay scope [1] 한정'),
      ('Amazon','detail',d,'original_sku_price',
       '(//div[@id="corePriceDisplay_desktop_feature_div"]//span[@data-a-strike="true"]//span[@class="a-offscreen"])[1]',
       NULL,
       'M.R.P. (strike-through) — corePriceDisplay scope 한정. 5/9 진단: centerCol union 제거 (variant carousel sub-card / sponsored sub-section 의 strike-through 가 잘못 매치되어 unavailable/no_featured 카드에도 OSP 채워지는 결함 root cause). page 에 노출 안 되는 OSP = selector 결함 → 정밀 scope 만. 할인 없는 product / unavailable / no_featured = OSP NULL 자연.'),
      ('Amazon','detail',d,'discount_type',
       '//*[@id="dealBadgeSupportingText"] | //*[@id="dealBadge_feature_div"]//span[contains(@class,"a-badge-text")] | //*[contains(@id,"DEAL_") and contains(@id,"-label")]//span[contains(@class,"a-badge-text")]',
       NULL,
       '신 layout: dealBadgeSupportingText (outer) — Selenium .text 가 visible inner span 모두 concat. timer 미발동 deal: "Limited time deal" / timer active: "Ends in HH:MM:SS" (시간 포함). screen reader labels (aok-offscreen/aok-hidden) 은 dealBadgeSupportingText 외부 sibling 이라 noise 없음 검증. 위치: main product apex_desktop 영역, 페이지당 1개. 구 layout (a-badge-text) 와 union. fallback NULL: corePrice savingsPercentage 는 위치 다른 영역 (할인%) 부적합')
    ON CONFLICT (site_account, page_type, domain, data_field) DO UPDATE SET
      xpath_primary  = EXCLUDED.xpath_primary,
      fallback_xpath = EXCLUDED.fallback_xpath,
      notes          = EXCLUDED.notes,
      is_active      = TRUE,
      updated_at     = NOW();
  END LOOP;
END $$;

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
       './/a[contains(@href,"/p/")]//div[string-length(normalize-space(text()))>10 and not(contains(text(),"Choice")) and not(contains(text(),"Bestseller")) and not(contains(text(),"Flipkart Assured"))][1]',
       './/a[contains(@href,"/p/")]/@title',
       '제품명 — 길이 10 이상 div, badge text ("Flipkart''s Choice" / "Bestseller" / "Flipkart Assured") 제외'),
      ('Flipkart','main',d,'discount_type',
       './/div[contains(@class,"HZ0E6r")]',
       NULL,
       '사용자 정책 (5/9 #2): cls "HZ0E6r Rm9_cy" deal badge innermost div 매치 (main 페이지 91 element 검증). 신규 deal type (Summer Deal / Anniversary Deal 등) 자동 수집. listing.py 의 discount_type 분기에서 Bank Offer + Exchange offer 영역 ("Upto" / "₹X" / "on Exchange") + 재고 표지 ("Only X left") 제외 + 길이 < 50 + ", " 합침. detail page 는 본 cls 미사용 layout 다수 — detail selector 별도 (단어 list).'),
      ('Flipkart','main',d,'sku_popularity',
       './/a[contains(@href,"spotlightTagId=default_BestsellerId")] | .//img[contains(@src,"/fa_")] | .//*[contains(text(),"Flipkart''s Choice") or contains(text(),"Flipkart Choice")]',
       NULL,
       'Bestseller (anchor href spotlightTagId) / Flipkart Assured (img /fa_*.png) / Flipkart''s Choice (text). 코드가 attr/text 검사 후 라벨 합침'),
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
       'ERD reference R56: Main Page 별점. 카드 안 "X.X" 패턴 (length=3, 가운데 dot, 유효 숫자). siel_log.parse_star_rating 후처리')
    ON CONFLICT (site_account, page_type, domain, data_field) DO UPDATE SET
      xpath_primary  = EXCLUDED.xpath_primary,
      fallback_xpath = EXCLUDED.fallback_xpath,
      notes          = EXCLUDED.notes,
      is_active      = TRUE,
      updated_at     = NOW();
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
       'M.R.P. — first ₹ div 의 직접 sibling 첫 ₹ (없으면 null = 할인 없는 product)')
    ON CONFLICT (site_account, page_type, domain, data_field) DO UPDATE SET
      xpath_primary  = EXCLUDED.xpath_primary,
      fallback_xpath = EXCLUDED.fallback_xpath,
      notes          = EXCLUDED.notes,
      is_active      = TRUE,
      updated_at     = NOW();
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
       'ERD: Main Page (HHP+TV). e.g. "21% off"')
    ON CONFLICT (site_account, page_type, domain, data_field) DO UPDATE SET
      xpath_primary  = EXCLUDED.xpath_primary,
      fallback_xpath = EXCLUDED.fallback_xpath,
      notes          = EXCLUDED.notes,
      is_active      = TRUE,
      updated_at     = NOW();
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
       'href attr')
    ON CONFLICT (site_account, page_type, domain, data_field) DO UPDATE SET
      xpath_primary  = EXCLUDED.xpath_primary,
      fallback_xpath = EXCLUDED.fallback_xpath,
      notes          = EXCLUDED.notes,
      is_active      = TRUE,
      updated_at     = NOW();
  END LOOP;
END $$;

-- =============================================================================
-- FLIPKART × BSR — DATA selectors 보강 (사용자 요청, 2026-05-03)
-- ERD v1 은 bsr=순위만 시드했으나, bsr only URL (main 300 에 없는 URL) 의 데이터가
-- 100% NULL 로 떨어짐. main selectors 를 page_type='bsr' 로 카피해 데이터 채움.
-- Flipkart bsr 페이지 (?sort=popularity) = default search 와 동일 카드 dom (검증됨).
-- main row 변경 시 다음 apply_sql 에서 bsr 도 자동 동기.
-- =============================================================================

INSERT INTO dx_siel_xpath_selectors
  (site_account, page_type, domain, data_field, xpath_primary, fallback_xpath, is_active, notes)
SELECT site_account,
       'bsr' AS page_type,
       domain,
       data_field,
       xpath_primary,
       fallback_xpath,
       is_active,
       COALESCE(notes,'') || ' [auto-copied from main 2026-05-03]' AS notes
FROM dx_siel_xpath_selectors
WHERE site_account = 'Flipkart'
  AND page_type    = 'main'
  AND domain       IN ('hhp','tv','ref','ldy')
  AND data_field   NOT IN ('base_container','product_url')
  AND is_active    = TRUE
ON CONFLICT (site_account, page_type, domain, data_field) DO UPDATE SET
  xpath_primary  = EXCLUDED.xpath_primary,
  fallback_xpath = EXCLUDED.fallback_xpath,
  notes          = EXCLUDED.notes,
  is_active      = TRUE,
  updated_at     = NOW();

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
      -- count_of_star_ratings / count_of_reviews 는 Main Page (reference 시트 r51/r49). detail 정의 X.
      ('Flipkart','detail',d,'star_rating',
       '(//*[starts-with(@id,"productRating_")] | //div[@dir="auto" and contains(@style,"inter_bold") and string-length(normalize-space(text()))=3 and substring(normalize-space(text()),2,1)="."])[1]',
       '//*[starts-with(@id,"productRating_")]/div',
       'union: (1) productRating_ ID prefix (옛 layout) + (2) inter_bold style 의 dir=auto div 텍스트 X.X 형식 (신 layout, 사용자 제공 element). DOM 첫 매치 = 메인 제품 별점 (Similar Products 카드 별점들보다 위)'),
      ('Flipkart','detail',d,'delivery_availability',
       '//div[normalize-space(text())="Delivery" or normalize-space(text())="Delivery by"]/parent::div',
       '//div[contains(text(),"Delivery by") or contains(text(),"FREE Delivery") or contains(text(),"Free Delivery")][1]',
       'parent div 의 .text — product 따라 라벨 다름: HHP "Delivery"+sibling "by ..." / TV/REF "Delivery by"+sibling "Tuesday, ...". 둘 다 합쳐 "Delivery by ..." 형식'),
      ('Flipkart','detail',d,'detailed_review_content',
       '//span[@class="css-1jxf684" and not(normalize-space(text())="more")]',
       '//div[@dir="auto"]/span[string-length(normalize-space(text()))>5 and not(normalize-space(text())="more")]',
       '리뷰 페이지 navigate 후. body 는 div[dir=auto] > span.css-1jxf684. "more" expand 버튼 제외. count_of_reviews 만큼 수집 (최대 20) — fpkt/detail.py 가 &page=2,3 누적'),
      ('Flipkart','detail',d,'retailer_sku_name_similar',
       '//a[contains(@href,"hl_lid=") and contains(@href,"cHJvZHVjdFJlY29tbWVuZGF0aW9uL3NpbWlsYXI")]//div[contains(@style,"text-overflow: ellipsis") and string-length(normalize-space(text()))>10 and not(contains(text(),"₹"))]',
       '//a[contains(@href,"hl_lid=")]//div[contains(@style,"text-overflow: ellipsis") and string-length(normalize-space(text()))>10 and not(contains(text(),"₹"))]',
       'Similar Products 카드. base64 marker cHJvZHVjdFJlY29tbWVuZGF0aW9uL3NpbWlsYXI = "productRecommendation/similar" — Similar 컨테이너에만 존재 (Frequently bought / Sponsored 등 다른 reco 캐러셀 제외). 제품명 div = ellipsis + 길이>10 + ₹제외 (가격/할인/Hot Deal 제외). fallback: hl_lid= 만 (base64 변경 시).'),
      ('Flipkart','detail',d,'sku',
       '//div[normalize-space(text())="Model Name"]/following-sibling::div[1]',
       '//h1[1]',
       'ERD: All details > Specifications > General > Model Name 바로 아래 텍스트. expand_specifications click 후 spec 영역에 노출 — fallback h1 전체 (post-process)'),
      -- main page NULL 시 fallback 용 detail page selector (사용자 요청, 2026-05-06).
      -- 5/6 anti-bot detection 으로 main listing 100% NULL → detail page 에서 회수.
      -- 가격 dom 단정: cls "v1zwn20" + font="default-fk-font-m" 의 ₹ 시작 div = main 가격
      -- (HHP/REF/LDY saved spec html 4 도메인 검증). banner/EMI/deal 영역 (cls v1zwn29/css-146c3p1)
      -- 자동 회피.
      ('Flipkart','detail',d,'final_sku_price',
       '(//div[contains(@class,"v1zwn20") and starts-with(normalize-space(text()),"₹")])[1]',
       '(//div[starts-with(normalize-space(text()),"₹") and not(starts-with(normalize-space(text()),"+₹"))])[1]',
       'cls v1zwn20 매치가 main 가격 (banner/EMI 회피). fallback: 첫 ₹ 시작 div + Protect Fee 회피'),
      ('Flipkart','detail',d,'original_sku_price',
       '(//div[contains(concat(" ",@class," ")," v1zwn21 ") and contains(@style,"line-through")])[1]',
       NULL,
       'detail page strikethrough original price (사용자 5/8 직접 검증). cls v1zwn21 + line-through style. div text 가 ₹ 없는 숫자만 ("10,999") — detail.py 분기에서 ₹ prefix post-process. final_sku_price 의 cls v1zwn20 와 substring 차이 (21!=20) 라 매치 충돌 X. anti-bot main NULL 시 detail fallback 효과'),
      ('Flipkart','detail',d,'discount_type',
       '//*[contains(text(),"Hot Deal") or contains(text(),"Hot deal") or contains(text(),"Super Deals") or contains(text(),"Saver Deal") or contains(text(),"SALE PE SALE") or contains(text(),"Lowest Price Live") or contains(text(),"Limited time") or contains(text(),"Limited Time") or contains(text(),"Big Saving") or contains(text(),"Coupon") or contains(text(),"Special Price") or contains(text(),"Deal of") or contains(text(),"Early bird") or contains(text(),"Early Bird")]',
       NULL,
       '사용자 검수 (5/10): detail page 단어 list. Bank Offer/Bank offer 제외 + Saver Deal/SALE PE SALE/Lowest Price Live 추가. "Trending" 제거 — similar products / recently viewed section 의 product image 좌상단 badge 만 확인 (deal badge 위치 X), false positive 비율 94-99% (HHP 80/85, REF 88/89, TV 254/311). 시차 변동 가능성 있어도 noise 우선 회피. detail.py 분기에서 multi-match 합침 + Bank Offer/재고/Exchange 영역 필터.'),
      ('Flipkart','detail',d,'sku_popularity',
       '//*[contains(text(),"Bestseller") or contains(text(),"Flipkart''s Choice") or contains(text(),"Flipkart Choice")] | //img[contains(@src,"/fa_")]',
       NULL,
       'detail page Bestseller marker = div text node (HHP/REF/LDY saved html 일관 검증). main page 와 다름 (anchor href 아님). 단일 마커만 추출 — main page 의 union 합침 로직과 형식 일관')
    ON CONFLICT (site_account, page_type, domain, data_field) DO UPDATE SET
      xpath_primary  = EXCLUDED.xpath_primary,
      fallback_xpath = EXCLUDED.fallback_xpath,
      notes          = EXCLUDED.notes,
      is_active      = TRUE,
      updated_at     = NOW();
  END LOOP;
END $$;

-- 정정 (2026-05-09): savings 4도메인 공통 detail selector revert. 사용자 정책:
-- LDY/REF savings 수집 대상 X (ERD HHP+TV main 만 정의). 5/8 commit 1d4404b 의
-- 4도메인 공통 LOOP 적용은 정책 위반.
-- 이미 db 에 INSERT 된 LDY/REF detail savings entry 비활성화 (UPDATE is_active=FALSE).
-- HHP/TV detail savings 는 도메인별 INSERT 로 별도 등록 (TV 는 sql:703 기존, HHP 신규 추가).
UPDATE dx_siel_xpath_selectors SET is_active = FALSE, updated_at = NOW()
 WHERE site_account = 'Flipkart' AND page_type = 'detail'
   AND domain IN ('ldy', 'ref') AND data_field = 'savings';

-- HHP detail savings 도메인별 추가 (TV 와 같은 패턴 — sql:703 참조)
INSERT INTO dx_siel_xpath_selectors
  (site_account,page_type,domain,data_field,xpath_primary,fallback_xpath,notes)
VALUES
  ('Flipkart','detail','hhp','savings',
   '(//div[contains(text(),"%") and string-length(normalize-space(text()))<=5 and not(ancestor::a[contains(@href,"/p/")])])[1]',
   '//div[contains(text(),"% off")]',
   'HHP detail % deal 표기. ERD: Main Page 정의 (HHP+TV). main NULL 시 detail fallback 회복. TV 와 같은 패턴 (sql:703)')
ON CONFLICT (site_account, page_type, domain, data_field) DO UPDATE SET
  xpath_primary  = EXCLUDED.xpath_primary,
  fallback_xpath = EXCLUDED.fallback_xpath,
  notes          = EXCLUDED.notes,
  is_active      = TRUE,
  updated_at     = NOW();

-- HHP 전용 (Flipkart) — ERD v1: 가격 3종 (final/original/savings) 은 Main Page 로 통합. detail 엔 trade_in/storage/color 만.
-- 추가 (2026-05-03): HHP sku 만 Model Number override (공통 'Model Name' → HHP 는 시리즈명이라 dup 발생, Model Number 가 진짜 unique 코드).
--                    TV/REF/LDY 는 Specifications General 에 'Model Number' 라벨 자체가 없어 공통 'Model Name' 유지.
INSERT INTO dx_siel_xpath_selectors
  (site_account,page_type,domain,data_field,xpath_primary,fallback_xpath,notes)
VALUES
  ('Flipkart','detail','hhp','sku',
   '//div[normalize-space(text())="Model Number"]/following-sibling::div[1]',
   '//h1[1]',
   'HHP 전용 override (2026-05-03): Specifications > General > Model Number (예: V2510). 공통 selector 의 Model Name 은 HHP 만 시리즈명 ("T4 Pro 5G") 이라 variant 미반영 dup. TV/REF/LDY 는 Model Number 라벨 자체 부재 → 공통 Model Name 유지.'),
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
   'ERD: Specifications > General > Color 바로 아래 div. expand_specifications click 후 spec 에 노출')
    ON CONFLICT (site_account, page_type, domain, data_field) DO UPDATE SET
      xpath_primary  = EXCLUDED.xpath_primary,
      fallback_xpath = EXCLUDED.fallback_xpath,
      notes          = EXCLUDED.notes,
      is_active      = TRUE,
      updated_at     = NOW();

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
   '//div[normalize-space(text())="Display Size"]/following-sibling::div[1]',
   '//div[normalize-space(text())="Display Size:"]/following-sibling::div[1] | (//div[contains(text(),"inch") and contains(text(),"cm") and string-length(normalize-space(text()))<30 and not(ancestor::h1) and not(ancestor::head) and not(ancestor::a)])[1]',
   'ERD: Specifications > Display Size 라벨 다음 div = "109 cm (43 inch)". fallback 1: deep spec colon 라벨 (See more 후 표기 — 값 "43" 만 추출). fallback 2: 30자 이하 inch+cm 패턴 (제품명 매치 회피)'),
  ('Flipkart','detail','tv','estimated_annual_electricity_use',
   '//div[normalize-space(text())="Power Consumption" or normalize-space(text())="Annual Energy Consumption" or normalize-space(text())="Energy Consumption" or normalize-space(text())="Power Consumption:" or normalize-space(text())="Annual Energy Consumption:"]/following-sibling::div[1]',
   '//td[normalize-space(text())="Power Consumption"]/following-sibling::td[1]',
   'deep spec (Power Features 그룹 — See more 후 lazy load). 라벨 콜론 없음 (highlights 와 다름). value 단위 (Standby W vs kWh/Year) 사이트별 의미 mismatch 가능 — raw 그대로 저장 후 분석 단계 분기')
    ON CONFLICT (site_account, page_type, domain, data_field) DO UPDATE SET
      xpath_primary  = EXCLUDED.xpath_primary,
      fallback_xpath = EXCLUDED.fallback_xpath,
      notes          = EXCLUDED.notes,
      is_active      = TRUE,
      updated_at     = NOW();

-- REF 전용 (Flipkart) — ERD v1: 가격 3종 Main Page 로 통합. REF 는 savings 자체 ERD 에 정의 없음. detail 엔 spec 2종만.
-- HHP hhp_color 와 동일 sibling 패턴 (ERD 의 Specifications 표 라벨 → sibling 값 div).
-- ref_refrigerator_type: 라벨 unique → HHP simple sibling 그대로.
-- ref_capacity: "Capacity" 가 icon (Capacity.png) / mini-spec 영역에도 노출 → Specifications 헤딩 scope 필수 (HHP 와 다른 점).
INSERT INTO dx_siel_xpath_selectors
  (site_account,page_type,domain,data_field,xpath_primary,fallback_xpath,notes)
VALUES
  ('Flipkart','detail','ref','ref_refrigerator_type',
   '//div[normalize-space(text())="Refrigerator Type"]/following-sibling::div[1]',
   '//div[normalize-space(text())="Refrigerator Type:"]/following::div[1]',
   'ERD: Specifications > Refrigerator Type 카테고리. HHP hhp_color 와 동일 simple sibling. fallback 은 highlights 콜론 라벨'),
  ('Flipkart','detail','ref','ref_capacity',
   '(//div[normalize-space(text())="Specifications"]/following::div[normalize-space(text())="Capacity"])[1]/following-sibling::div[1][contains(text(),"L")]',
   '//div[normalize-space(text())="Capacity:"]/following::div[1][contains(text(),"L")]',
   'ERD: Specifications > General > Capacity. value 가 "L" 단위 포함 조건 추가 — 별점 (4.2) 같은 noisy stray 매치 차단')
    ON CONFLICT (site_account, page_type, domain, data_field) DO UPDATE SET
      xpath_primary  = EXCLUDED.xpath_primary,
      fallback_xpath = EXCLUDED.fallback_xpath,
      notes          = EXCLUDED.notes,
      is_active      = TRUE,
      updated_at     = NOW();

-- LDY 전용 (modern Flipkart DOM: <div>label</div><div>value</div> 형제 패턴, 콜론 없음 — TV 와 다름)
INSERT INTO dx_siel_xpath_selectors
  (site_account,page_type,domain,data_field,xpath_primary,fallback_xpath,notes)
VALUES
  ('Flipkart','detail','ldy','ldy_loading_type',
   '//div[normalize-space(text())="Function Type" or normalize-space(text())="Loading Type"]/following-sibling::div[1]',
   '//td[normalize-space(text())="Function Type" or normalize-space(text())="Loading Type"]/following-sibling::td[1]',
   'modern Flipkart spec: <div>Function Type</div><div>VALUE</div>. 콜론 없음 (TV 는 콜론 있음). fallback td 보존'),
  ('Flipkart','detail','ldy','ldy_capacity',
   '//div[normalize-space(text())="Washing Capacity" or normalize-space(text())="Capacity"]/following-sibling::div[1][contains(text(),"kg")]',
   '//td[normalize-space(text())="Washing Capacity" or normalize-space(text())="Capacity"]/following-sibling::td[1][contains(text(),"kg")]',
   'LDY spec 라벨 product 따라 "Washing Capacity" 또는 "Capacity". 단위 "kg" 필터로 REF "L" 과 자연 분리. user inspect: WTT60UNX → 라벨 "Capacity"')
    ON CONFLICT (site_account, page_type, domain, data_field) DO UPDATE SET
      xpath_primary  = EXCLUDED.xpath_primary,
      fallback_xpath = EXCLUDED.fallback_xpath,
      notes          = EXCLUDED.notes,
      is_active      = TRUE,
      updated_at     = NOW();

-- =============================================================================
-- 확인 쿼리
-- =============================================================================
-- SELECT page_type, domain, COUNT(*) AS n
--   FROM dx_siel_xpath_selectors
--  WHERE site_account = 'Amazon' AND is_active
--  GROUP BY page_type, domain
--  ORDER BY page_type, domain;
