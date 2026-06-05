@echo off
setlocal
set "FPKT_PRODUCTS=ref"
call "%~dp0fpkt_api_run_all.bat" %*
exit /b %ERRORLEVEL%
