@echo off
setlocal
cd /d "%~dp0"
echo This will delete all MsupaaEscrow local users, orders, disputes and reports.
set /p confirm=Type YES to continue: 
if /I not "%confirm%"=="YES" (
  echo Cancelled.
  pause
  exit /b 0
)
if exist "instance\msupaaescrow.db" del /q "instance\msupaaescrow.db"
echo Database reset. It will be recreated when the app starts.
pause
