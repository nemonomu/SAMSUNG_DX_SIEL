@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul

cd /d "%~dp0.."

rem Auto-deploy: pull latest, then re-run the updated bat in a fresh process
rem (a bat must not modify itself mid-run). Pull failure is non-fatal - the run
rem continues with local code. Set FPKT_SKIP_GIT_PULL=1 to disable.
if /I not "%FPKT_SKIP_GIT_PULL%"=="1" if not "%FPKT_GIT_PULLED%"=="1" (
  echo [fpkt_api_run_all] git pull --ff-only
  git pull --ff-only
  if errorlevel 1 echo [fpkt_api_run_all] git pull failed - continuing with local code
  set "FPKT_GIT_PULLED=1"
  cmd.exe /d /c ""%~f0" %*"
  exit /b !ERRORLEVEL!
)

set "RUN_MODE=%~1"
if "%RUN_MODE%"=="" set "RUN_MODE=insert"

set "MODE_OK="
if /I "%RUN_MODE%"=="insert" set "MODE_OK=1"
if /I "%RUN_MODE%"=="dry-run" set "MODE_OK=1"
if /I "%RUN_MODE%"=="no-db" set "MODE_OK=1"
if /I "%RUN_MODE%"=="check" set "MODE_OK=1"
if not defined MODE_OK (
  echo [fpkt_api_run_all] unsupported mode: %RUN_MODE%
  echo [fpkt_api_run_all] use insert, dry-run, no-db, or check
  exit /b 2
)

for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss_fff"') do set "RUN_STAMP=%%I"
set "RUN_DIR=%CD%\fpkt_api\test_output\ops_all_%RUN_STAMP%"
set "RUN_LOG=%RUN_DIR%\run_console.log"
mkdir "%RUN_DIR%" >nul 2>nul

echo [fpkt_api_run_all] repo=%CD%
>> "%RUN_LOG%" echo [fpkt_api_run_all] repo=%CD%
echo [fpkt_api_run_all] mode=%RUN_MODE%
>> "%RUN_LOG%" echo [fpkt_api_run_all] mode=%RUN_MODE%
echo [fpkt_api_run_all] output_dir=%RUN_DIR%
>> "%RUN_LOG%" echo [fpkt_api_run_all] output_dir=%RUN_DIR%

set "DEFAULT_API_DIR=%CD%\..\siel_logs\api"
if exist "C:\siel\logs\api\main_har.txt" set "DEFAULT_API_DIR=C:\siel\logs\api"
if "%FPKT_API_DIR%"=="" set "FPKT_API_DIR=%DEFAULT_API_DIR%"

if "%FPKT_PRODUCTS%"=="" set "FPKT_PRODUCTS=tv hhp ref ldy"
if "%FPKT_MAIN_TARGET%"=="" set "FPKT_MAIN_TARGET=300"
if "%FPKT_BSR_TARGET%"=="" set "FPKT_BSR_TARGET=100"
if "%FPKT_MAX_PAGES_MAIN%"=="" set "FPKT_MAX_PAGES_MAIN=30"
if "%FPKT_MAX_PAGES_BSR%"=="" set "FPKT_MAX_PAGES_BSR=15"
if "%FPKT_API_TIMEOUT%"=="" set "FPKT_API_TIMEOUT=90"
if "%FPKT_LISTING_RETRIES%"=="" set "FPKT_LISTING_RETRIES=3"
if "%FPKT_LISTING_RETRY_DELAY%"=="" set "FPKT_LISTING_RETRY_DELAY=10"
if "%FPKT_MAX_DETAIL%"=="" set "FPKT_MAX_DETAIL=0"
if "%FPKT_DETAIL_RETRIES%"=="" set "FPKT_DETAIL_RETRIES=2"
if "%FPKT_REVIEW_PAGES%"=="" set "FPKT_REVIEW_PAGES=2"
if "%FPKT_REVIEW_SHORT_MAX_PAGES%"=="" set "FPKT_REVIEW_SHORT_MAX_PAGES=5"
if "%FPKT_REVIEW_RETRIES%"=="" set "FPKT_REVIEW_RETRIES=2"
if "%FPKT_MAX_REVIEWS_PER_PRODUCT%"=="" set "FPKT_MAX_REVIEWS_PER_PRODUCT=20"
if "%FPKT_INSERT_MAX_N%"=="" set "FPKT_INSERT_MAX_N=0"
if "%FPKT_LOCK_STALE_HOURS%"=="" set "FPKT_LOCK_STALE_HOURS=18"
if "%FPKT_INSECURE_SSL%"=="" set "FPKT_INSECURE_SSL=1"
if "%FPKT_CONTINUE_ON_PRODUCT_FAIL%"=="" set "FPKT_CONTINUE_ON_PRODUCT_FAIL=1"
if "%FPKT_PRODUCT_RETRY_AFTER_ALL%"=="" set "FPKT_PRODUCT_RETRY_AFTER_ALL=1"
if "%FPKT_PRODUCT_RETRY_DELAY%"=="" set "FPKT_PRODUCT_RETRY_DELAY=0"
if "%FPKT_EMAIL_REPORT%"=="" (
  set "FPKT_EMAIL_REPORT=0"
  if /I "%RUN_MODE%"=="insert" set "FPKT_EMAIL_REPORT=1"
)

echo [fpkt_api_run_all] api_dir=%FPKT_API_DIR%
>> "%RUN_LOG%" echo [fpkt_api_run_all] api_dir=%FPKT_API_DIR%
echo [fpkt_api_run_all] products=%FPKT_PRODUCTS%
>> "%RUN_LOG%" echo [fpkt_api_run_all] products=%FPKT_PRODUCTS%
echo [fpkt_api_run_all] main_target=%FPKT_MAIN_TARGET% bsr_target=%FPKT_BSR_TARGET% max_detail=%FPKT_MAX_DETAIL%
>> "%RUN_LOG%" echo [fpkt_api_run_all] main_target=%FPKT_MAIN_TARGET% bsr_target=%FPKT_BSR_TARGET% max_detail=%FPKT_MAX_DETAIL%
echo [fpkt_api_run_all] api_timeout=%FPKT_API_TIMEOUT% listing_retries=%FPKT_LISTING_RETRIES% listing_retry_delay=%FPKT_LISTING_RETRY_DELAY%
>> "%RUN_LOG%" echo [fpkt_api_run_all] api_timeout=%FPKT_API_TIMEOUT% listing_retries=%FPKT_LISTING_RETRIES% listing_retry_delay=%FPKT_LISTING_RETRY_DELAY%
echo [fpkt_api_run_all] review_pages=%FPKT_REVIEW_PAGES% review_short_max_pages=%FPKT_REVIEW_SHORT_MAX_PAGES%
>> "%RUN_LOG%" echo [fpkt_api_run_all] review_pages=%FPKT_REVIEW_PAGES% review_short_max_pages=%FPKT_REVIEW_SHORT_MAX_PAGES%
echo [fpkt_api_run_all] email_report=%FPKT_EMAIL_REPORT%
>> "%RUN_LOG%" echo [fpkt_api_run_all] email_report=%FPKT_EMAIL_REPORT%
echo [fpkt_api_run_all] insecure_ssl=%FPKT_INSECURE_SSL%
>> "%RUN_LOG%" echo [fpkt_api_run_all] insecure_ssl=%FPKT_INSECURE_SSL%
echo [fpkt_api_run_all] continue_on_product_fail=%FPKT_CONTINUE_ON_PRODUCT_FAIL%
>> "%RUN_LOG%" echo [fpkt_api_run_all] continue_on_product_fail=%FPKT_CONTINUE_ON_PRODUCT_FAIL%
echo [fpkt_api_run_all] product_retry_after_all=%FPKT_PRODUCT_RETRY_AFTER_ALL% product_retry_delay=%FPKT_PRODUCT_RETRY_DELAY%
>> "%RUN_LOG%" echo [fpkt_api_run_all] product_retry_after_all=%FPKT_PRODUCT_RETRY_AFTER_ALL% product_retry_delay=%FPKT_PRODUCT_RETRY_DELAY%

git status --short --untracked-files=no > "%RUN_DIR%\git_status_before.txt" 2>&1

python -m py_compile fpkt_api\ops_test.py insert_test_retail_com.py > "%RUN_DIR%\py_compile.txt" 2>&1
if errorlevel 1 (
  echo [fpkt_api_run_all] py_compile failed
  >> "%RUN_LOG%" echo [fpkt_api_run_all] py_compile failed
  type "%RUN_DIR%\py_compile.txt"
  type "%RUN_DIR%\py_compile.txt" >> "%RUN_LOG%"
  set "EXIT_CODE=1"
  goto fail
)
echo [fpkt_api_run_all] py_compile ok
>> "%RUN_LOG%" echo [fpkt_api_run_all] py_compile ok

if /I "%RUN_MODE%"=="check" (
  echo [fpkt_api_run_all] check mode complete
  >> "%RUN_LOG%" echo [fpkt_api_run_all] check mode complete
  exit /b 0
)

if not exist "%FPKT_API_DIR%\main_har.txt" if not exist "%FPKT_API_DIR%\main_page2_page1_har.txt" (
  echo [fpkt_api_run_all] missing listing HAR in %FPKT_API_DIR%
  >> "%RUN_LOG%" echo [fpkt_api_run_all] missing listing HAR in %FPKT_API_DIR%
  exit /b 4
)
if not "%FPKT_MAX_DETAIL%"=="-1" (
  if not exist "%FPKT_API_DIR%\detail_curl.txt" (
    echo [fpkt_api_run_all] missing detail_curl.txt in %FPKT_API_DIR%
    >> "%RUN_LOG%" echo [fpkt_api_run_all] missing detail_curl.txt in %FPKT_API_DIR%
    exit /b 4
  )
  if not "%FPKT_REVIEW_PAGES%"=="0" if not exist "%FPKT_API_DIR%\review_curl.txt" (
    echo [fpkt_api_run_all] missing review_curl.txt in %FPKT_API_DIR%
    >> "%RUN_LOG%" echo [fpkt_api_run_all] missing review_curl.txt in %FPKT_API_DIR%
    exit /b 4
  )
)

set "LOCK_DIR=%CD%\.fpkt_api_run_all.lock"
if /I not "%FPKT_SKIP_LOCK%"=="1" (
  if exist "%LOCK_DIR%\info.txt" (
    powershell -NoProfile -Command "$p='%LOCK_DIR%'; $h=[double]'%FPKT_LOCK_STALE_HOURS%'; if ((Get-Date) - (Get-Item -LiteralPath $p).LastWriteTime -gt [TimeSpan]::FromHours($h)) { Remove-Item -LiteralPath $p -Recurse -Force }" >nul 2>nul
  )
  mkdir "%LOCK_DIR%" >nul 2>nul
  if errorlevel 1 (
    echo [fpkt_api_run_all] another run seems active: %LOCK_DIR%
    >> "%RUN_LOG%" echo [fpkt_api_run_all] another run seems active: %LOCK_DIR%
    echo [fpkt_api_run_all] remove the lock only after confirming no scheduler run is active
    >> "%RUN_LOG%" echo [fpkt_api_run_all] remove the lock only after confirming no scheduler run is active
    exit /b 3
  )
  set "LOCK_ACQUIRED=1"
  > "%LOCK_DIR%\info.txt" echo started=%DATE% %TIME%
  >> "%LOCK_DIR%\info.txt" echo run_dir=%RUN_DIR%
)

set "DB_FLAGS="
set "EMAIL_FLAGS="
set "FIRST_PASS_EMAIL_FLAGS="
set "FAILED_PRODUCTS="
set "FAILED_PRODUCT_NAMES="
if /I "%RUN_MODE%"=="dry-run" set "DB_FLAGS=!DB_FLAGS! --db-insert --db-dry-run"
if /I "%RUN_MODE%"=="insert" set "DB_FLAGS=!DB_FLAGS! --db-insert --real-batch-id"
if /I "%FPKT_REAL_BATCH_ID%"=="1" set "DB_FLAGS=!DB_FLAGS! --real-batch-id"
if /I "%FPKT_ALLOW_QA_INSERT%"=="1" set "DB_FLAGS=!DB_FLAGS! --allow-qa-insert"
if /I "%FPKT_INSECURE_SSL%"=="1" set "DB_FLAGS=!DB_FLAGS! --insecure"
if /I "%FPKT_EMAIL_REPORT%"=="1" set "EMAIL_FLAGS=!EMAIL_FLAGS! --email-report"
set "FIRST_PASS_EMAIL_FLAGS=!EMAIL_FLAGS!"
if /I "%FPKT_EMAIL_REPORT%"=="1" if /I "%FPKT_CONTINUE_ON_PRODUCT_FAIL%"=="1" if /I "%FPKT_PRODUCT_RETRY_AFTER_ALL%"=="1" set "FIRST_PASS_EMAIL_FLAGS=!FIRST_PASS_EMAIL_FLAGS! --email-report-success-only"

for %%P in (%FPKT_PRODUCTS%) do (
  set "PRODUCT_LOG=%RUN_DIR%\%%P_console.log"
  echo [fpkt_api_run_all] start %%P
  >> "%RUN_LOG%" echo [fpkt_api_run_all] start %%P
  echo [fpkt_api_run_all] log=!PRODUCT_LOG!
  >> "%RUN_LOG%" echo [fpkt_api_run_all] log=!PRODUCT_LOG!
  set "PRODUCT_CMD=set FPKT_API_TIMEOUT=%FPKT_API_TIMEOUT%&& python -u fpkt_api\ops_test.py --api-dir %FPKT_API_DIR% --product %%P --main-target %FPKT_MAIN_TARGET% --bsr-target %FPKT_BSR_TARGET% --max-pages-main %FPKT_MAX_PAGES_MAIN% --max-pages-bsr %FPKT_MAX_PAGES_BSR% --listing-retries %FPKT_LISTING_RETRIES% --listing-retry-delay %FPKT_LISTING_RETRY_DELAY% --api-timeout %FPKT_API_TIMEOUT% --max-detail %FPKT_MAX_DETAIL% --detail-retries %FPKT_DETAIL_RETRIES% --review-pages %FPKT_REVIEW_PAGES% --review-short-max-pages %FPKT_REVIEW_SHORT_MAX_PAGES% --review-retries %FPKT_REVIEW_RETRIES% --max-reviews-per-product %FPKT_MAX_REVIEWS_PER_PRODUCT% --insert-max-n %FPKT_INSERT_MAX_N% !DB_FLAGS! !FIRST_PASS_EMAIL_FLAGS!"
  set "FPKT_PRODUCT_CMD=!PRODUCT_CMD!"
  set "FPKT_PRODUCT_LOG=!PRODUCT_LOG!"
  set "FPKT_RUN_LOG=%RUN_LOG%"
  powershell -NoProfile -ExecutionPolicy Bypass -Command "$cmd=$env:FPKT_PRODUCT_CMD; $productLog=$env:FPKT_PRODUCT_LOG; $runLog=$env:FPKT_RUN_LOG; if (Test-Path -LiteralPath $productLog) { Remove-Item -LiteralPath $productLog -Force }; & cmd.exe /d /c $cmd 2>&1 | Tee-Object -FilePath $productLog | Tee-Object -FilePath $runLog -Append; exit $LASTEXITCODE"
  set "PRODUCT_CODE=!ERRORLEVEL!"
  if not "!PRODUCT_CODE!"=="0" (
    echo [fpkt_api_run_all] failed %%P exit=!PRODUCT_CODE!
    >> "%RUN_LOG%" echo [fpkt_api_run_all] failed %%P exit=!PRODUCT_CODE!
    if not defined EXIT_CODE set "EXIT_CODE=!PRODUCT_CODE!"
    set "FAILED_PRODUCTS=!FAILED_PRODUCTS! %%P(!PRODUCT_CODE!)"
    set "FAILED_PRODUCT_NAMES=!FAILED_PRODUCT_NAMES! %%P"
    if /I not "%FPKT_CONTINUE_ON_PRODUCT_FAIL%"=="1" goto fail
  ) else (
    echo [fpkt_api_run_all] done %%P
    >> "%RUN_LOG%" echo [fpkt_api_run_all] done %%P
  )
)

if defined FAILED_PRODUCT_NAMES if /I "%FPKT_PRODUCT_RETRY_AFTER_ALL%"=="1" (
  echo [fpkt_api_run_all] retry_after_all products=!FAILED_PRODUCT_NAMES!
  >> "%RUN_LOG%" echo [fpkt_api_run_all] retry_after_all products=!FAILED_PRODUCT_NAMES!
  if not "%FPKT_PRODUCT_RETRY_DELAY%"=="0" powershell -NoProfile -Command "Start-Sleep -Seconds %FPKT_PRODUCT_RETRY_DELAY%"
  set "FIRST_PASS_FAILED_PRODUCTS=!FAILED_PRODUCTS!"
  set "RETRY_PRODUCTS=!FAILED_PRODUCT_NAMES!"
  set "FAILED_PRODUCTS="
  set "FAILED_PRODUCT_NAMES="
  set "EXIT_CODE="
  for %%P in (!RETRY_PRODUCTS!) do (
    set "PRODUCT_LOG=%RUN_DIR%\%%P_retry_console.log"
    echo [fpkt_api_run_all] retry %%P
    >> "%RUN_LOG%" echo [fpkt_api_run_all] retry %%P
    echo [fpkt_api_run_all] log=!PRODUCT_LOG!
    >> "%RUN_LOG%" echo [fpkt_api_run_all] log=!PRODUCT_LOG!
    set "PRODUCT_CMD=set FPKT_API_TIMEOUT=%FPKT_API_TIMEOUT%&& python -u fpkt_api\ops_test.py --api-dir %FPKT_API_DIR% --product %%P --main-target %FPKT_MAIN_TARGET% --bsr-target %FPKT_BSR_TARGET% --max-pages-main %FPKT_MAX_PAGES_MAIN% --max-pages-bsr %FPKT_MAX_PAGES_BSR% --listing-retries %FPKT_LISTING_RETRIES% --listing-retry-delay %FPKT_LISTING_RETRY_DELAY% --api-timeout %FPKT_API_TIMEOUT% --max-detail %FPKT_MAX_DETAIL% --detail-retries %FPKT_DETAIL_RETRIES% --review-pages %FPKT_REVIEW_PAGES% --review-short-max-pages %FPKT_REVIEW_SHORT_MAX_PAGES% --review-retries %FPKT_REVIEW_RETRIES% --max-reviews-per-product %FPKT_MAX_REVIEWS_PER_PRODUCT% --insert-max-n %FPKT_INSERT_MAX_N% !DB_FLAGS! !EMAIL_FLAGS!"
    set "FPKT_PRODUCT_CMD=!PRODUCT_CMD!"
    set "FPKT_PRODUCT_LOG=!PRODUCT_LOG!"
    set "FPKT_RUN_LOG=%RUN_LOG%"
    powershell -NoProfile -ExecutionPolicy Bypass -Command "$cmd=$env:FPKT_PRODUCT_CMD; $productLog=$env:FPKT_PRODUCT_LOG; $runLog=$env:FPKT_RUN_LOG; if (Test-Path -LiteralPath $productLog) { Remove-Item -LiteralPath $productLog -Force }; & cmd.exe /d /c $cmd 2>&1 | Tee-Object -FilePath $productLog | Tee-Object -FilePath $runLog -Append; exit $LASTEXITCODE"
    set "PRODUCT_CODE=!ERRORLEVEL!"
    if not "!PRODUCT_CODE!"=="0" (
      echo [fpkt_api_run_all] retry_failed %%P exit=!PRODUCT_CODE!
      >> "%RUN_LOG%" echo [fpkt_api_run_all] retry_failed %%P exit=!PRODUCT_CODE!
      if not defined EXIT_CODE set "EXIT_CODE=!PRODUCT_CODE!"
      set "FAILED_PRODUCTS=!FAILED_PRODUCTS! %%P(!PRODUCT_CODE!)"
      set "FAILED_PRODUCT_NAMES=!FAILED_PRODUCT_NAMES! %%P"
    ) else (
      echo [fpkt_api_run_all] recovered %%P
      >> "%RUN_LOG%" echo [fpkt_api_run_all] recovered %%P
    )
  )
  echo [fpkt_api_run_all] first_pass_failed_products=!FIRST_PASS_FAILED_PRODUCTS!
  >> "%RUN_LOG%" echo [fpkt_api_run_all] first_pass_failed_products=!FIRST_PASS_FAILED_PRODUCTS!
)

if defined FAILED_PRODUCTS goto fail

git status --short --untracked-files=no > "%RUN_DIR%\git_status_after.txt" 2>&1

if defined LOCK_ACQUIRED rmdir /s /q "%LOCK_DIR%" >nul 2>nul
echo [fpkt_api_run_all] all done
>> "%RUN_LOG%" echo [fpkt_api_run_all] all done
echo [fpkt_api_run_all] result_folder=%RUN_DIR%
>> "%RUN_LOG%" echo [fpkt_api_run_all] result_folder=%RUN_DIR%
exit /b 0

:fail
if not defined EXIT_CODE set "EXIT_CODE=1"
git status --short --untracked-files=no > "%RUN_DIR%\git_status_after.txt" 2>&1
if defined LOCK_ACQUIRED rmdir /s /q "%LOCK_DIR%" >nul 2>nul
if defined FAILED_PRODUCTS echo [fpkt_api_run_all] failed_products=%FAILED_PRODUCTS%
if defined FAILED_PRODUCTS >> "%RUN_LOG%" echo [fpkt_api_run_all] failed_products=%FAILED_PRODUCTS%
echo [fpkt_api_run_all] failed exit=%EXIT_CODE%
>> "%RUN_LOG%" echo [fpkt_api_run_all] failed exit=%EXIT_CODE%
echo [fpkt_api_run_all] result_folder=%RUN_DIR%
>> "%RUN_LOG%" echo [fpkt_api_run_all] result_folder=%RUN_DIR%
exit /b %EXIT_CODE%
