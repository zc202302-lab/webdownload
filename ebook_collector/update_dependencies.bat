@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe goto missing
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto failed
echo Dependencies updated. Run start.bat next.
pause
exit /b 0
:missing
echo Run setup.bat first.
pause
exit /b 1
:failed
echo Update failed. Please send the error message.
pause
exit /b 1
