@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul

cd /d "%~dp0.."

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
if "%FPKT_MAX_DETAIL%"=="" set "FPKT_MAX_DETAIL=0"
if "%FPKT_DETAIL_RETRIES%"=="" set "FPKT_DETAIL_RETRIES=2"
if "%FPKT_REVIEW_PAGES%"=="" set "FPKT_REVIEW_PAGES=2"
if "%FPKT_REVIEW_SHORT_MAX_PAGES%"=="" set "FPKT_REVIEW_SHORT_MAX_PAGES=5"
if "%FPKT_REVIEW_RETRIES%"=="" set "FPKT_REVIEW_RETRIES=2"
if "%FPKT_MAX_REVIEWS_PER_PRODUCT%"=="" set "FPKT_MAX_REVIEWS_PER_PRODUCT=20"
if "%FPKT_INSERT_MAX_N%"=="" set "FPKT_INSERT_MAX_N=0"
if "%FPKT_LOCK_STALE_HOURS%"=="" set "FPKT_LOCK_STALE_HOURS=18"
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
echo [fpkt_api_run_all] review_pages=%FPKT_REVIEW_PAGES% review_short_max_pages=%FPKT_REVIEW_SHORT_MAX_PAGES%
>> "%RUN_LOG%" echo [fpkt_api_run_all] review_pages=%FPKT_REVIEW_PAGES% review_short_max_pages=%FPKT_REVIEW_SHORT_MAX_PAGES%
echo [fpkt_api_run_all] email_report=%FPKT_EMAIL_REPORT%
>> "%RUN_LOG%" echo [fpkt_api_run_all] email_report=%FPKT_EMAIL_REPORT%

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
if /I "%RUN_MODE%"=="dry-run" set "DB_FLAGS=!DB_FLAGS! --db-insert --db-dry-run"
if /I "%RUN_MODE%"=="insert" set "DB_FLAGS=!DB_FLAGS! --db-insert --real-batch-id"
if /I "%FPKT_REAL_BATCH_ID%"=="1" set "DB_FLAGS=!DB_FLAGS! --real-batch-id"
if /I "%FPKT_ALLOW_QA_INSERT%"=="1" set "DB_FLAGS=!DB_FLAGS! --allow-qa-insert"
if /I "%FPKT_INSECURE_SSL%"=="1" set "DB_FLAGS=!DB_FLAGS! --insecure"
if /I "%FPKT_EMAIL_REPORT%"=="1" set "EMAIL_FLAGS=!EMAIL_FLAGS! --email-report"

for %%P in (%FPKT_PRODUCTS%) do (
  set "PRODUCT_LOG=%RUN_DIR%\%%P_console.log"
  echo [fpkt_api_run_all] start %%P
  >> "%RUN_LOG%" echo [fpkt_api_run_all] start %%P
  echo [fpkt_api_run_all] log=!PRODUCT_LOG!
  >> "%RUN_LOG%" echo [fpkt_api_run_all] log=!PRODUCT_LOG!
  python fpkt_api\ops_test.py ^
    --api-dir "%FPKT_API_DIR%" ^
    --product %%P ^
    --main-target %FPKT_MAIN_TARGET% ^
    --bsr-target %FPKT_BSR_TARGET% ^
    --max-pages-main %FPKT_MAX_PAGES_MAIN% ^
    --max-pages-bsr %FPKT_MAX_PAGES_BSR% ^
    --max-detail %FPKT_MAX_DETAIL% ^
    --detail-retries %FPKT_DETAIL_RETRIES% ^
    --review-pages %FPKT_REVIEW_PAGES% ^
    --review-short-max-pages %FPKT_REVIEW_SHORT_MAX_PAGES% ^
    --review-retries %FPKT_REVIEW_RETRIES% ^
    --max-reviews-per-product %FPKT_MAX_REVIEWS_PER_PRODUCT% ^
    --insert-max-n %FPKT_INSERT_MAX_N% ^
    !DB_FLAGS! ^
    !EMAIL_FLAGS! > "!PRODUCT_LOG!" 2>&1
  set "PRODUCT_CODE=!ERRORLEVEL!"
  type "!PRODUCT_LOG!"
  type "!PRODUCT_LOG!" >> "%RUN_LOG%"
  if not "!PRODUCT_CODE!"=="0" (
    echo [fpkt_api_run_all] failed %%P exit=!PRODUCT_CODE!
    >> "%RUN_LOG%" echo [fpkt_api_run_all] failed %%P exit=!PRODUCT_CODE!
    set "EXIT_CODE=!PRODUCT_CODE!"
    goto fail
  )
  echo [fpkt_api_run_all] done %%P
  >> "%RUN_LOG%" echo [fpkt_api_run_all] done %%P
)

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
echo [fpkt_api_run_all] failed exit=%EXIT_CODE%
>> "%RUN_LOG%" echo [fpkt_api_run_all] failed exit=%EXIT_CODE%
echo [fpkt_api_run_all] result_folder=%RUN_DIR%
>> "%RUN_LOG%" echo [fpkt_api_run_all] result_folder=%RUN_DIR%
exit /b %EXIT_CODE%
