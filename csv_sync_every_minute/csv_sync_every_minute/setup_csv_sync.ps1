# Installs/replaces CSVDateSync for the current Windows user.
# Daily repeat trigger every minute + logon trigger + immediate start.
# No admin rights or stored passwords intended: interactive user only.
$ErrorActionPreference = 'Stop'
$taskName = 'CSVDateSync'
$appDir = Join-Path $env:LOCALAPPDATA 'CSVDateSync'
$packageDir = Split-Path -Parent $MyInvocation.MyCommand.Path

try {
    New-Item -ItemType Directory -Path $appDir -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $packageDir 'sync_csv.ps1') -Destination (Join-Path $appDir 'sync_csv.ps1') -Force
    Copy-Item -LiteralPath (Join-Path $packageDir 'run_hidden.vbs') -Destination (Join-Path $appDir 'run_hidden.vbs') -Force

    $userName = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    $escUser = [System.Security.SecurityElement]::Escape($userName)
    $wscriptPath = Join-Path $env:WINDIR 'System32\wscript.exe'
    $escCommand = [System.Security.SecurityElement]::Escape($wscriptPath)
    $escArguments = [System.Security.SecurityElement]::Escape(('//B //Nologo "{0}"' -f (Join-Path $appDir 'run_hidden.vbs')))
    $escWorkingDir = [System.Security.SecurityElement]::Escape($appDir)
    # Schedule the first repeat for the next whole minute, not at midnight in the past.
    $nextMinute = (Get-Date).AddMinutes(1)
    $nextMinute = $nextMinute.AddSeconds(-$nextMinute.Second).AddMilliseconds(-$nextMinute.Millisecond)
    $startBoundary = $nextMinute.ToString("yyyy-MM-dd'T'HH:mm:ss")

    $taskXml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Copy only CSV from TU/e Strontium Images (2026-10-10 through current date) to D:\Data_Processor\data. Every minute and at sign-in.</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <Repetition>
        <Interval>PT1M</Interval>
        <Duration>P1D</Duration>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <StartBoundary>$startBoundary</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>
    </CalendarTrigger>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>$escUser</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>$escUser</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT12H</ExecutionTimeLimit>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>$escCommand</Command>
      <Arguments>$escArguments</Arguments>
      <WorkingDirectory>$escWorkingDir</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@

    $taskXml = $taskXml.TrimStart()
    # -Force updates an older hourly CSVDateSync task if present.
    Register-ScheduledTask -TaskName $taskName -Xml $taskXml -Force | Out-Null
    Start-ScheduledTask -TaskName $taskName

    Write-Host ''
    Write-Host 'SUCCESS: Installed/updated Windows Scheduled Task CSVDateSync' -ForegroundColor Green
    Write-Host ('User: ' + $userName)
    Write-Host ('Scheduled: every minute, first scheduled trigger ' + $startBoundary)
    Write-Host 'Also runs at sign-in and has been started once now.'
    Write-Host ('Program folder: ' + $appDir)
    Write-Host ('Log file: ' + (Join-Path $appDir 'sync.log'))
    Write-Host 'Network access: the Windows user must be signed in and must have access to the UNC share.'
} catch {
    Write-Host ('INSTALL FAILED: ' + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
