@echo off
cd /d "%~dp0.."
python fpkt\run.py --product tv ref ldy --stages main bsr detail %*
exit /b %ERRORLEVEL%
