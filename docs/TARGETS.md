# SIEL 모니터링 대상 (ERD v1 기준)

> 원본: `c:\Users\gomguard\Desktop\dx_siel_erd_v1.xlsx` (sheet: dataset, reference)
> 시장: 인도 (₹). 사이트: Amazon.In, Flipkart.

## 1. 대상 매트릭스

| 제품군 | 약자 | Amazon.In | Flipkart |
|---|---|---|---|
| 핸드폰 | HHP | O | O |
| TV     | TV  | O | O |
| 냉장고 | REF | O | O |
| 세탁기 | LDY | O | O |

**`LDY` = Laundry**. 세탁기는 ERD enum 기준 LDY 사용. WM/세탁기 아님.

## 2. 페이지 / row 수 목표

| 페이지 | 의미 | Amazon 목표 | Flipkart 목표 |
|---|---|---|---|
| BSR Page     | Best Seller 순위 (best seller URL) | 1·2 page **모두 끝까지 스크롤** | popularity 정렬, **100개** |
| Main Page    | 검색 결과 (default 정렬)            | Featured 정렬, **300개**, 노출 순서대로 main_rank 1~300 | Relevance 정렬, **300개**, 노출 순서대로 main_rank 1~300 |
| Product Page | 개별 상품 상세                       | listing 에서 받은 url 단위 | listing 에서 받은 url 단위 |

## 3. URL 템플릿

### 3.1 Amazon.In BSR

| 제품군 | URL (page 1) |
|---|---|
| HHP | https://www.amazon.in/gp/bestsellers/electronics/1805560031/ |
| TV  | https://www.amazon.in/gp/bestsellers/electronics/1389396031/ |
| REF | https://www.amazon.in/gp/bestsellers/kitchen/1380365031/ |
| LDY | https://www.amazon.in/gp/bestsellers/kitchen/1380373031/ |

각 카테고리 page 2: `?ie=UTF8&pg=2` 추가. 1·2 page 모두 **끝까지 스크롤** 필요 (lazy render).

### 3.2 Amazon.In Main (검색)

정리 룰 합의됨 (Flipkart 와 동일): 휘발 param (`crid` / `qid` / `xpid` / `sprefix` / `ref=...`) 제거. ERD 갱신 대기.

| 제품군 | URL (page 1) | page 2+ |
|---|---|---|
| HHP | `https://www.amazon.in/s?k=smartphone&i=electronics` | `&page={n}` |
| TV  | `https://www.amazon.in/s?k=tv&i=electronics`         | `&page={n}` |
| REF | `https://www.amazon.in/s?k=refrigerator`              | `&page={n}` |
| LDY | `https://www.amazon.in/s?k=washing+machine`           | `&page={n}` |

300개 채울 때까지 page 넘김. 노출 순서대로 main_rank 1~300.

### 3.2.1 Amazon.In BSR (별도 — `ref=zg_bs_*` 보존)

Amazon BSR 의 `ref=zg_bs_nav_*` / `ref=zg_bs_pg_2_*` 는 navigation token 일 가능성 → 일단 ERD raw 유지. 첫 run 에서 빼고도 결과 동일한지 실험 후 정리 여부 결정.

(BSR URL 자체는 §3.1 참조)

### 3.3 Flipkart BSR (popularity 정렬) — 통일 룰 적용

`as-show=off` 통일, 공백 인코딩 `+` 통일. 실 동작 차이는 첫 run 에서 검증.

| 제품군 | URL (page 1) |
|---|---|
| HHP | `https://www.flipkart.com/search?q=smartphone&otracker=search&otracker1=search&marketplace=FLIPKART&as-show=off&as=off&sort=popularity` |
| TV  | `https://www.flipkart.com/search?q=tv&otracker=search&otracker1=search&marketplace=FLIPKART&as-show=off&as=off&sort=popularity` |
| REF | `https://www.flipkart.com/search?q=refrigerator&otracker=search&otracker1=search&marketplace=FLIPKART&as-show=off&as=off&sort=popularity` |
| LDY | `https://www.flipkart.com/search?q=washing+machine&otracker=search&otracker1=search&marketplace=FLIPKART&as-show=off&as=off&sort=popularity` |

`&page=2` 추가하며 100개 채울 때까지.

### 3.4 Flipkart Main (relevance 정렬) — 통일 룰 적용

| 제품군 | URL (page 1) |
|---|---|
| HHP | `https://www.flipkart.com/search?q=smartphone&otracker=search&otracker1=search&marketplace=FLIPKART&as-show=off&as=off` |
| TV  | `https://www.flipkart.com/search?q=tv&otracker=search&otracker1=search&marketplace=FLIPKART&as-show=off&as=off&sort=relevance` |
| REF | `https://www.flipkart.com/search?q=refrigerator&otracker=search&otracker1=search&marketplace=FLIPKART&as-show=off&as=off` |
| LDY | `https://www.flipkart.com/search?q=washing+machine&otracker=search&otracker1=search&marketplace=FLIPKART&as-show=off&as=off` |

`&page=2` 추가하며 300개 채울 때까지. 노출 순서대로 main_rank 1~300.

## 4. 수집 컬럼 매트릭스

### 4.1 공통 (모든 제품군) — 단, HHP Flipkart 는 가격을 Product Page 에서 수집

| 컬럼 | Amazon.In | Flipkart |
|---|---|---|
| bsr_rank                          | BSR | BSR |
| main_rank                         | Main | Main |
| retailer_sku_name                 | Main | Main |
| final_sku_price                   | Main | Main (TV/REF/LDY) / Product (HHP) |
| original_sku_price                | Main | Main (TV/REF/LDY) / Product (HHP) |
| discount_type                     | Main | Main |
| sku_popularity                    | Main | Main |
| sku_status                        | Main | Main |
| number_of_units_purchased_past_month | Main | (X) |
| available_quantity_for_purchase   | (X) | Main |
| delivery_availability             | Product | Product |
| fastest_delivery                  | Product | (X) |
| inventory_status                  | Product | (X) |
| star_rating                       | Product | Product |
| count_of_star_ratings             | Product | Product |
| count_of_reviews                  | (X) | Product |
| summarized_review_content         | Product | (X) |
| detailed_review_content           | Product | Product |
| retailer_sku_name_similar         | Product | Product |
| item / sku                        | Product | Product |

### 4.2 제품군별 전용

| 컬럼 | 제품군 | Amazon.In | Flipkart |
|---|---|---|---|
| hhp_storage                       | HHP | Product | Product |
| hhp_color                         | HHP | Product | Product |
| trade_in                          | HHP | Product | Product |
| screen_size                       | TV  | Product | Product |
| model_year                        | TV  | Product | Product |
| estimated_annual_electricity_use  | TV  | Product | Product |
| sku_assurance                     | TV  | Product | (X) |
| savings (할인률)                  | TV/HHP | (X) | Product |
| ref_refrigerator_type             | REF | Product | Product |
| ref_capacity                      | REF | Product | Product |
| ldy_loading_type                  | LDY | Product | Product |
| ldy_capacity                      | LDY | Product | Product |

## 5. 인터랙션 필요 항목

| 사이트 | 위치 | 필요 동작 |
|---|---|---|
| Amazon | BSR Page | 1·2 page 모두 페이지 **끝까지 스크롤** (lazy render) |
| Amazon | Product Page detailed_review_content | 페이지 하단 끝까지 스크롤 후 리뷰 카드별로 본문 모음 |
| Amazon | Product Page hhp_storage / hhp_color / sku 등 | "Additional details" / "Item details" 섹션 **expand 클릭** |
| Flipkart | Product Page Specifications 텍스트 (전 제품군 spec 컬럼) | "Specifications" 버튼 클릭 → All details 열림 (xpath: `//*[@id="slot-list-container"]/.../div[15]/.../div[1]/div/div[2]/div/div/div/div`) |
| Flipkart | Product Page detailed_review_content | "Show all reviews" 클릭 → review page 진입 (**Buy now 안 누르게 주의**) → 점진 스크롤로 20개 |

## 6. 추출 후처리 규칙

| 컬럼 | 사이트 | 후처리 |
|---|---|---|
| bsr_rank                | 양쪽 | `#` 제거 후 숫자만 |
| count_of_star_ratings   | Amazon | "481 global ratings" → "481" |
| count_of_star_ratings   | Flipkart | "Ratings" 앞 숫자만 |
| count_of_reviews        | Flipkart | "Reviews" 앞 숫자만 |
| star_rating             | Amazon | "4.2 out of 5" → "4.2" |
| delivery_availability   | Amazon | 끝에 "Details" 텍스트 제외 |
| fastest_delivery        | Amazon | 앞 "Or" 제외 + 끝 "Details" 제외 |
| inventory_status        | Amazon | 없음 / 공백 → null |
| sku_assurance           | Amazon TV | "Fulfilled" → "Amazon Fulfilled" 로 저장 |
| sku_popularity          | Flipkart | Bestseller / Flipkart Assured 둘 다 잡히면 "Bestseller, Flipkart Assured" 로 저장 |
| trade_in                | Flipkart HHP | 두 xpath 결과 공백 1개 두고 합치기 |

## 7. 일정

- ERD v1 확정: 2026-04-29
- 결과 DB 스키마 작성: TBD (사용자 결과 테이블 정보 도착 후)
- meta DB seed: TBD
- listing/detail/run 코드 작성: TBD
- 본 법인 batch 첫 트리거: TBD

## 8. 의심 4건 — 사용자 회신 결과 (2026-04-29)

1. **xpath 중복 (retailer_sku_name vs sku_status)**: ERD 갱신으로 해소. `retailer_sku_name` xpath 가 `...div[2]/div[1]/div[2]` 로 수정됨 (sku_status 는 `div[1]`).
2. **HHP Flipkart Product 의 가격**: 검증용 아님. **HHP 의 가격 수집 위치는 Product Page** 가 정상. Sheet1 의 final_sku_price/original_sku_price 제품군이 `공통` → `tv,ref,ldy` 로 변경. HHP 은 Main Page 가격 수집 안 함.
3. **xpath GUID (30393de8-..., DEAL_{ASIN}-label, customer_review-{REVIEWID})**: 상대경로화 가능. 운산 표준 `meta_selectors` 가 이미 패턴 박음 — `data_field='base_container'` row = anchor xpath, 나머지는 `.` 시작 상대 xpath. CSV 확인 결과 SIEL × HHP × Amazon × Main 까지 이미 robust 형태로 입력됨. ERD 의 raw xpath 는 사양서일 뿐, INSERT 시 변환 필요.
4. **Flipkart REF URL 의 휘발 param**: 정리 템플릿화 가능. `q` / `sid` / `sort` / `page` 만 남기고 `requestId` / `xpid` / `crid` / `qid` / `otracker*` / `as-pos` / `suggestionId` / `as-backfill` 등 휘발 param 제거. REF 만 `sid=j9e%2Cabm%2Chzg` (카테고리 강제) 보존 필요.
