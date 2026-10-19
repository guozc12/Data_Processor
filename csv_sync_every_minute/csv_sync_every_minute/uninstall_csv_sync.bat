@echo off
setlocal
echo Deleting Scheduled Task CSVDateSync (copied data is NOT deleted)...
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "Unregister-ScheduledTask -TaskName 'CSVDateSync' -Confirm:$false -ErrorAction Stop"
if errorlevel 1 (
    echo Failed to remove the scheduled task; review the error above.
) else (
    echo Automatic sync was disabled. Local data and logs were left untouched.
)
echo.
pause
