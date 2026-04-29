@echo off
REM 한 번만 실행. git pull 시 sql/*.sql 자동 적용 활성화.
REM 내부 동작: git config core.hooksPath .githooks
REM 그 후 .githooks/post-merge 가 git pull 마다 실행되어 apply_sql.py 호출.

git config core.hooksPath .githooks
if errorlevel 1 (
    echo [setup_hooks] git config 실행 실패. cwd 가 repo root 인지 확인.
    exit /b 1
)
echo [setup_hooks] OK. core.hooksPath = .githooks 설정됨.
echo [setup_hooks] 이제 git pull 마다 sql/*.sql 자동 적용됩니다.
echo.
echo 첫 적용은 수동으로:
echo   python apply_sql.py
