# Copies CSV files only, keeping YYYY-MM-DD\scan...\subfolder structure.
# Network path and destination are hard-coded as requested.
$ErrorActionPreference = 'Stop'
$sourceRoot = '\\store03.tue.nl\ap_kat1_raw_lab_data\Strontium\Images'
$targetRoot = 'D:\Data_Processor\data'
$firstDay = [datetime]::ParseExact('2026-10-10', 'yyyy-MM-dd', [System.Globalization.CultureInfo]::InvariantCulture)
$lastDay = (Get-Date).Date
$programDir = Join-Path $env:LOCALAPPDATA 'CSVDateSync'
$logFile = Join-Path $programDir 'sync.log'
New-Item -ItemType Directory -Path $programDir -Force | Out-Null

function Write-SyncLog([string]$message) {
    Add-Content -LiteralPath $logFile -Encoding UTF8 -Value ('{0}  {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $message)
}

# Mutex protects against overlapping copies, even if run_once.bat is used.
$mutex = New-Object System.Threading.Mutex($false, 'Local\CSVDateSync_CopyLock')
$lockAcquired = $false
try {
    try {
        $lockAcquired = $mutex.WaitOne(0)
    } catch [System.Threading.AbandonedMutexException] {
        $lockAcquired = $true
    }
    if (-not $lockAcquired) { exit 0 }

    # Rotate the log before each run; failures should never break the copy.
    try {
        if ((Test-Path -LiteralPath $logFile) -and ((Get-Item -LiteralPath $logFile).Length -gt 5MB)) {
            Move-Item -LiteralPath $logFile -Destination (Join-Path $programDir 'sync.previous.log') -Force
        }
    } catch { }

    $timer = [System.Diagnostics.Stopwatch]::StartNew()
    $failures = 0
    $processed = 0
    $sourceReady = $false
    Write-SyncLog ('BEGIN: {0} -> {1}, {2} through {3}' -f $sourceRoot, $targetRoot, $firstDay.ToString('yyyy-MM-dd'), $lastDay.ToString('yyyy-MM-dd'))

    # The server or VPN may be temporarily unavailable after login.
    for ($tryNum = 1; $tryNum -le 3; $tryNum++) {
        if (Test-Path -LiteralPath $sourceRoot -PathType Container) {
            $sourceReady = $true
            break
        }
        if ($tryNum -lt 3) { Start-Sleep -Seconds 5 }
    }

    if (-not $sourceReady) {
        Write-SyncLog ('ERROR: network share unavailable or access denied: ' + $sourceRoot)
        $failures++
    } else {
        New-Item -ItemType Directory -Path $targetRoot -Force | Out-Null
        # Enumerate only first-level date directories, not all images/files.
        $dateFolders = Get-ChildItem -LiteralPath $sourceRoot -Directory -ErrorAction Stop
        foreach ($dir in $dateFolders) {
            if ($dir.Name -cnotmatch '^\d{4}-\d{2}-\d{2}$') { continue }
            $day = [datetime]::MinValue
            $validDate = [datetime]::TryParseExact(
                $dir.Name, 'yyyy-MM-dd',
                [System.Globalization.CultureInfo]::InvariantCulture,
                [System.Globalization.DateTimeStyles]::None,
                [ref]$day
            )
            if (-not $validDate) { continue }
            if ($day -lt $firstDay -or $day -gt $lastDay) { continue }

            $processed++
            $dst = Join-Path $targetRoot $dir.Name
            # /S copies subdirectories containing CSVs; no empty dirs.
            # /XJ prevents reparse-point loops. /R:1 /W:1 avoid long retry delays.
            # Never use /MIR or /PURGE (which delete destination data).
            $output = (& "$env:WINDIR\System32\robocopy.exe" $dir.FullName $dst '*.csv' /S /R:1 /W:1 /XJ /NP /NFL /NDL /NJH /NJS 2>&1 | Out-String)
            $code = $LASTEXITCODE
            if ($code -ge 8) {
                $failures++
                Write-SyncLog ('ERROR: {0}, robocopy exit code {1}; {2}' -f $dir.Name, $code, $output.Trim())
            }
        }
    }
    $timer.Stop()
    Write-SyncLog ('END: checked {0} date folder(s), errors {1}, elapsed {2:N1}s' -f $processed, $failures, $timer.Elapsed.TotalSeconds)
    if ($failures -gt 0) { exit 1 }
    exit 0
} catch {
    Write-SyncLog ('FATAL: ' + $_.Exception.Message)
    exit 1
} finally {
    if ($lockAcquired) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
