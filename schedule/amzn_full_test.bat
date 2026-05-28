@echo off
setlocal EnableExtensions

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0amzn_full_test.ps1" %*
exit /b %ERRORLEVEL%
