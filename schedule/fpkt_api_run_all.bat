@echo off
setlocal EnableExtensions

cd /d "%~dp0.."

set "RUN_MODE=%~1"
if "%RUN_MODE%"=="" set "RUN_MODE=dry-run"
set "MODE_OK="
if /I "%RUN_MODE%"=="dry-run" set "MODE_OK=1"
if /I "%RUN_MODE%"=="insert" set "MODE_OK=1"
if /I "%RUN_MODE%"=="no-db" set "MODE_OK=1"
if not defined MODE_OK (
  echo [fpkt_api_run_all] unsupported mode: %RUN_MODE%
  echo [fpkt_api_run_all] use dry-run, insert, or no-db
  exit /b 2
)

set "DEFAULT_API_DIR=%CD%\..\siel_logs\api"
if exist "C:\siel\logs\api\main_har.txt" set "DEFAULT_API_DIR=C:\siel\logs\api"
if "%FPKT_API_DIR%"=="" set "FPKT_API_DIR=%DEFAULT_API_DIR%"

if "%FPKT_PRODUCTS%"=="" set "FPKT_PRODUCTS=tv hhp ref ldy"
if "%FPKT_MAIN_TARGET%"=="" set "FPKT_MAIN_TARGET=300"
if "%FPKT_BSR_TARGET%"=="" set "FPKT_BSR_TARGET=100"
if "%FPKT_MAX_PAGES_MAIN%"=="" set "FPKT_MAX_PAGES_MAIN=30"
if "%FPKT_MAX_PAGES_BSR%"=="" set "FPKT_MAX_PAGES_BSR=15"
if "%FPKT_MAX_DETAIL%"=="" set "FPKT_MAX_DETAIL=0"
if "%FPKT_REVIEW_PAGES%"=="" set "FPKT_REVIEW_PAGES=2"
if "%FPKT_MAX_REVIEWS_PER_PRODUCT%"=="" set "FPKT_MAX_REVIEWS_PER_PRODUCT=20"

set "DB_FLAGS="
set "USE_REAL_BATCH="
if /I "%RUN_MODE%"=="dry-run" set "DB_FLAGS=%DB_FLAGS% --db-insert --db-dry-run"
if /I "%RUN_MODE%"=="insert" (
  set "DB_FLAGS=%DB_FLAGS% --db-insert"
  set "USE_REAL_BATCH=1"
)
if /I "%RUN_MODE%"=="no-db" set "DB_FLAGS=%DB_FLAGS%"
if /I "%FPKT_REAL_BATCH_ID%"=="1" set "USE_REAL_BATCH=1"
if "%USE_REAL_BATCH%"=="1" set "DB_FLAGS=%DB_FLAGS% --real-batch-id"
if /I "%FPKT_ALLOW_QA_INSERT%"=="1" set "DB_FLAGS=%DB_FLAGS% --allow-qa-insert"
if /I "%FPKT_INSECURE_SSL%"=="1" set "DB_FLAGS=%DB_FLAGS% --insecure"

echo [fpkt_api_run_all] mode=%RUN_MODE%
echo [fpkt_api_run_all] api_dir=%FPKT_API_DIR%
echo [fpkt_api_run_all] products=%FPKT_PRODUCTS%

for %%P in (%FPKT_PRODUCTS%) do (
  echo [fpkt_api_run_all] start %%P
  python fpkt_api\ops_test.py ^
    --api-dir "%FPKT_API_DIR%" ^
    --product %%P ^
    --main-target %FPKT_MAIN_TARGET% ^
    --bsr-target %FPKT_BSR_TARGET% ^
    --max-pages-main %FPKT_MAX_PAGES_MAIN% ^
    --max-pages-bsr %FPKT_MAX_PAGES_BSR% ^
    --max-detail %FPKT_MAX_DETAIL% ^
    --review-pages %FPKT_REVIEW_PAGES% ^
    --max-reviews-per-product %FPKT_MAX_REVIEWS_PER_PRODUCT% ^
    %DB_FLAGS%
  if errorlevel 1 (
    echo [fpkt_api_run_all] failed %%P
    exit /b 1
  )
  echo [fpkt_api_run_all] done %%P
)

echo [fpkt_api_run_all] all done
exit /b 0
