@echo off
setlocal EnableExtensions
cd /d "%~dp0.."
rem Install/check only. Never calls a collector, DB loader, or email sender.
python fpkt_api\check_capture.py --install %*
exit /b %ERRORLEVEL%
