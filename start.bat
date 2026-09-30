@echo off
cd /d "%~dp0"
echo ai-workbench 启动中...
echo 浏览器将打开 http://127.0.0.1:8010
start "" http://127.0.0.1:8010
".venv\Scripts\python.exe" app.py
pause
