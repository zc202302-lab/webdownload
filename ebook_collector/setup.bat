@echo off
setlocal
cd /d "%~dp0"
echo Checking Python...
py -3 -c "import sys; assert sys.version_info >= (3,10)" >nul 2>&1
if not errorlevel 1 (
 set "EBOOK_PY=py -3"
 goto found
)
python -c "import sys; assert sys.version_info >= (3,10)" >nul 2>&1
if not errorlevel 1 (
 set "EBOOK_PY=python"
 goto found
)
echo Python 3.10 or newer was not found.
echo Install Python and enable Add Python to PATH, then reopen this file.
pause
exit /b 1
:found
%EBOOK_PY% --version
 echo Creating isolated environment...
%EBOOK_PY% -m venv .venv > setup.log 2>&1
if errorlevel 1 goto failed
 echo Installing packages. This may take several minutes...
.venv\Scripts\python.exe -m pip install -r requirements.txt >> setup.log 2>&1
if errorlevel 1 goto failed
 echo Browser: start.bat will use installed Edge or Chrome.
 echo Setup completed. Run start.bat next.
pause
exit /b 0
:failed
echo Setup failed. Details follow:
type setup.log
echo Please send setup.log for diagnosis.
pause
exit /b 1
