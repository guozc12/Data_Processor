@echo off
setlocal
set "WORKER=%LOCALAPPDATA%\CSVDateSync\sync_csv.ps1"
if not exist "%WORKER%" (
    echo Please run install_csv_sync.bat first.
    pause
    exit /b 1
)
echo Starting one manual sync, please wait...
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%WORKER%"
echo.
echo Last 20 sync log lines:
powershell.exe -NoLogo -NoProfile -Command "Get-Content -LiteralPath (Join-Path $env:LOCALAPPDATA 'CSVDateSync\sync.log') -Tail 20"
echo.
pause
