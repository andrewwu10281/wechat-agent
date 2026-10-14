@echo off
:: 官方后端方案，无需管理员权限
chcp 65001 >nul
cd /d "%~dp0"
.venv\Scripts\python.exe bot.py
pause
