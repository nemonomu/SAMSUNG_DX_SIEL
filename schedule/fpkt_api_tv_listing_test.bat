@echo off
setlocal EnableExtensions
chcp 65001 >nul

cd /d "%~dp0.."

if "%FPKT_API_DIR%"=="" set "FPKT_API_DIR=%CD%\..\siel_logs\api"
if exist "C:\siel\logs\api\main_har.txt" set "FPKT_API_DIR=C:\siel\logs\api"
if "%FPKT_API_TIMEOUT%"=="" set "FPKT_API_TIMEOUT=90"
if "%FPKT_LISTING_RETRIES%"=="" set "FPKT_LISTING_RETRIES=2"
if "%FPKT_LISTING_RETRY_DELAY%"=="" set "FPKT_LISTING_RETRY_DELAY=5"

python -u fpkt_api\listing_test.py --product tv --api-dir "%FPKT_API_DIR%" --timeout "%FPKT_API_TIMEOUT%" --retries "%FPKT_LISTING_RETRIES%" --retry-delay "%FPKT_LISTING_RETRY_DELAY%" --insecure %*
exit /b %ERRORLEVEL%
