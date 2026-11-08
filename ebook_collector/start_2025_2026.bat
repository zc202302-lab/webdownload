@echo off
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
if not exist .venv\Scripts\python.exe (
 call setup.bat
 if errorlevel 1 exit /b 1
)
.venv\Scripts\python.exe download_2025_2026.py
pause
