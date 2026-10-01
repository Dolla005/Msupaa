@echo off
setlocal
cd /d "%~dp0"

echo.
echo ========================================
echo       MsupaaEscrow - Windows Start
echo ========================================
echo.

where py >nul 2>nul
if errorlevel 1 (
  echo Python launcher ^(py^) was not found.
  echo Install Python 3.10 or newer, then run this file again.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo [1/3] Creating virtual environment...
  py -m venv .venv
  if errorlevel 1 goto :fail
) else (
  echo [1/3] Virtual environment already exists.
)

echo [2/3] Installing required packages...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :fail

echo [3/3] Starting MsupaaEscrow...
echo.
echo Open this address in your browser:
echo     http://127.0.0.1:5000
echo.
echo Press Ctrl+C to stop the server.
echo.
".venv\Scripts\python.exe" app.py
exit /b %errorlevel%

:fail
echo.
echo Setup failed. Check the error shown above.
pause
exit /b 1
