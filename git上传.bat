@echo off
chcp 65001 >nul
cd /d "%~dp0"
git config core.quotepath false >nul 2>&1

echo ==========================================
echo    Upload to GitHub - wechat-agent project
echo ==========================================
echo.
echo Current changes:
git status --short
echo.
set /p msg=Commit message, blank = update: 
if "%msg%"=="" set msg=update
echo.
echo [1/3] staging ...
git add .
echo [2/3] committing ...
git commit -m "%msg%"
echo [3/3] pushing ...
git push
echo.
echo Done. Press any key to close.
pause >nul
