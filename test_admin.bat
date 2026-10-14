@echo off
cd /d "%~dp0"
echo [1/3] 关闭残留微信进程...
taskkill /f /im WeChat.exe >nul 2>&1
taskkill /f /im WeChatAppEx.exe >nul 2>&1
timeout /t 2 /nobreak >nul
echo [2/3] 添加 Defender 排除项(保险)...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Add-MpPreference -ExclusionPath 'C:\Users\lenovo\wechat-agent\.venv\Lib\site-packages\wcferry' -ErrorAction SilentlyContinue; Add-MpPreference -ExclusionPath 'C:\Program Files\Tencent\WeChat' -ErrorAction SilentlyContinue"
echo [3/3] 运行注入测试...
.venv\Scripts\python.exe test_wcf.py > test_wcf.log 2>&1
echo 测试结束
