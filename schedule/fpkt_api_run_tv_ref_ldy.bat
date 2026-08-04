@echo off
setlocal
set "FPKT_PRODUCTS=tv ref ldy"
call "%~dp0fpkt_api_run_all.bat" %*
exit /b %ERRORLEVEL%
