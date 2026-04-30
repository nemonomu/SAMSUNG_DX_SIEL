@echo off
chcp 65001 >nul
REM 한 번만 실행. git pull 시 sql/*.sql 자동 적용 활성화.
REM
REM 1) post-merge hook (.githooks/post-merge): git pull 이 실제 fetch+merge 일 때 자동 SQL 적용
REM 2) git alias 'up': "Already up to date" 케이스도 SQL 적용 — `git up` 으로 한 방에

git config core.hooksPath .githooks
if errorlevel 1 (
    echo [setup_hooks] git config core.hooksPath 실패. cwd 가 repo root 인지 확인.
    exit /b 1
)
echo [setup_hooks] OK. core.hooksPath = .githooks

git config alias.up "!git pull && python apply_sql.py"
if errorlevel 1 (
    echo [setup_hooks] git alias up 등록 실패.
    exit /b 1
)
echo [setup_hooks] OK. git alias 'up' 등록됨.
echo.
echo 사용법:
echo   git up      ^<- 권장. pull + apply_sql 한 방에 (Already up to date 도 SQL 재적용)
echo   git pull    ^<- 변경 fetch+merge 일 때만 hook 동작
echo.
echo 첫 적용은 수동으로:
echo   python apply_sql.py
