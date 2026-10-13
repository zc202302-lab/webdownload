@echo off
cd /d "%~dp0"
.venv\Scripts\python.exe -m playwright install chromium --no-shell
pause
