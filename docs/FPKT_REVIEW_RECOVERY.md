# Flipkart 리뷰 누락 복구 (2026-10-02)

상품의 평점 링크를 클릭해 리뷰 패널을 연 뒤, 동일 PID의 필터 없는 리뷰 페이지로
이동한다. 패널은 `slot-list-container` 밖에 생기므로 패널 선택자를 별도로 사용한다.
`/ratings-reviews-details-page` 주소에 직접 접속하지 않는다.

## 배포 파일

- `fpkt/detail.py`
- `fpkt/review_fields.py`
- `fpkt/recover_reviews.py`
- `sql/dx_siel_xpath_selectors.sql`
- `sql/manual/dx_siel_fpkt_review_panel.sql`

RDP의 `C:\siel` 아래 같은 상대 경로에 반영한다. 실행 중인 수집 프로세스가 끝난 뒤
코드와 SQL을 함께 적용한다. 기존 `config.py`와 설치된 수집용 Python 환경을 사용한다.
스키마는 바꾸지 않으며, 마이그레이션은 Flipkart 상세 선택자 두 종류만 갱신한다.

```powershell
Set-Location C:\siel; python -B apply_sql.py sql/manual/dx_siel_fpkt_review_panel.sql
```

## 이번 배치 대상 확인 (사이트/DB 접속 없음)

```powershell
Set-Location C:\siel; python -B fpkt/recover_reviews.py --input fpkt/logs/siel_flipkart_tv_run_20261002100802.jsonl --product tv --batch-id f_20261002_043802 --plan
```

제공된 원본에서는 상세 상품 311개, 리뷰 본문 누락 311개, 평점·평점 수·리뷰 수
누락 각각 10개다. 목록에 이미 있는 리뷰 수를 우선하므로 상세 JSONL에 필드가 없는
301개 상품을 리뷰 수 누락으로 잘못 판단하지 않는다.

## 한 상품 먼저 수집 (DB 변경 없음)

```powershell
Set-Location C:\siel; python -B fpkt/recover_reviews.py --input fpkt/logs/siel_flipkart_tv_run_20261002100802.jsonl --product tv --batch-id f_20261002_043802 --output fpkt/logs/tv_review_recovery_20261002.jsonl --limit 1
```

`pending=none`은 네 필드의 누락이 해소됐다는 뜻이다. 리뷰 수가 명시적으로 0이면
본문 없음은 정상으로 처리한다. 리뷰 본문은 기존 수집 정책대로 최대 20개다.
`pending`이 남으면 실패 항목을 확인한다. 실제 데이터가 없는 경우에도 0을 추정해서
채우지 않으므로 일부 상품은 계속 누락 상태일 수 있다.

## 전체 복구 및 기존 DB 행의 NULL 필드만 반영

아래 명령은 위에서 만든 결과 파일을 재사용한다. 완료한 상품은 재수집하지 않고,
미완료 상품만 다시 시도한다. 첫 실행부터 전체 복구하려면 `--resume`을 빼고 새 출력
파일을 지정한다.

```powershell
Set-Location C:\siel; python -B fpkt/recover_reviews.py --input fpkt/logs/siel_flipkart_tv_run_20261002100802.jsonl --product tv --batch-id f_20261002_043802 --output fpkt/logs/tv_review_recovery_20261002.jsonl --resume --apply
```

- `retail_com`: 평점, 평점 수, 리뷰 수, 리뷰 본문만 갱신.
- `product_list`: 테이블에 존재하는 평점, 평점 수, 리뷰 수만 갱신.
- batch_id + item(PID) + Flipkart + SIEL + 제품군으로 범위를 제한.
- 현재 DB 값이 NULL 또는 공백인 셀만 갱신. `0`, `0.0`과 기존 본문은 보존.
- 상품별 짧은 트랜잭션으로 처리하고, 변경 전 값과 변경값을 기록한 뒤 커밋 결과 기록.
- 원본 JSONL, 가격, 스펙, 순위, 최초 수집 시각, 배치 ID는 변경하지 않음.
- 신규 행 INSERT 없음. 대상 배치에 기존 retail 행이 없으면 수집 전에 중단.
- 복구 값은 재수집 시점의 값이며, 과거 시점의 값을 재현하는 것은 아님.

결과 JSONL은 **복구 기록 파일**이다. `insert_test_retail_com.py`의 입력으로 사용하지 않는다.
중단 후 같은 `--resume --apply` 명령을 실행하면 기존 DB 값은 유지하면서 이어서 처리한다.
입력 파일 해시와 배치가 다른 복구 파일은 재사용을 거부한다. 출력 파일을 덮어쓰지 않으며,
기존 파일을 쓰려면 `--resume`이 필요하다.

종료 코드 0: 요청 범위의 누락 없음/DB 반영 오류 없음. 종료 코드 2: 누락 또는 DB 반영
오류 남음. 마지막 summary의 `unresolved_products`, `db_errors`, `updated_cells`를 확인한다.
`--limit` 실행은 그 제한 범위에 대한 결과다. DB 값이 다른 작업에서 먼저 채워졌다면
해당 셀은 보존하므로 갱신 수는 수집 성공 수와 다를 수 있다.

## 검증

### 리뷰 수집 시간 개선

정규 수집과 복구 수집에 같은 최적화가 적용된다. 리뷰 페이지에 먼저 직접 이동하며,
본문 로딩이나 이동이 실패한 경우에만 기존 상품 페이지 재접속을 한 번 수행한다.
리뷰 페이지의 고정 3초 대기는 필요한 데이터가 나타나면 끝나는 대기로 대체했다.
본문 10개가 스크롤 후에도 안정적이고 같은 상품의 다음 페이지 링크가 표시되면
다음 페이지로 진행한다. 조건이 불명확하면 기존의 추가 로딩 확인을 유지한다.
최대 20개, 중복 본문 제외, 최대 3페이지 정책은 유지한다.

복구 진행 줄의 `elapsed`는 해당 상품의 수집·대기·DB 처리 시간을 포함한다.
`status=resumed`는 캐시 재사용이므로 수집 속도 비교에서 제외한다. 실사이트에서
같은 상품 10개의 기존 로그와 새 수집의 시간 및 본문 수를 비교해야 실제 단축 폭을
확인할 수 있다. 오프라인 테스트만으로 실사이트 성능 향상을 보장하지 않는다.

실행 중 교체할 때는 기존 복구를 Ctrl+C로 중단하고 PowerShell 프롬프트가 돌아온 뒤
현재 브랜치에서 `git pull --ff-only`하고 기존 출력 파일에 `--resume --apply`로 재개한다.
저장된 완료 상품은 재수집하지 않으며, 누락 상품과 아직 수집하지 않은 상품을 처리한다.
SQL 선택자는 이번 시간 개선에서 바뀌지 않았다.

```powershell
python -B -m unittest discover -s tests -p test_fpkt_review_recovery.py -v
```

로컬 오프라인 테스트는 패널 진입 순서, 잘못된 PID/항목별 리뷰 배제, 기존 값 보존,
정확한 배치 한정 업데이트와 재시작 처리를 확인한다. 실제 RDP Chrome/DB 복구 실행은
배포 후 위 한 상품 명령으로 확인한다.
