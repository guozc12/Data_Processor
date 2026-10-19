@echo off
setlocal
cd /d "%~dp0"
echo Installing/updating CSVDateSync: sync every 1 minute and at Windows sign-in...
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_csv_sync.ps1"
if errorlevel 1 (
    echo.
    echo Installation FAILED. Please review the error shown above.
    echo You may need permission to register a task for this Windows account.
) else (
    echo.
    echo Installation complete. The task runs in the background.
    echo You can delete this extracted ZIP folder after installation.
)
echo.
pause
