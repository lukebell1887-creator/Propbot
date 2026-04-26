# ======================================================================
#  SETUP_VPS_AUTOSTART.ps1
#  Run ONCE on the VPS (as Administrator) to make PropBot restart itself
#  at every Windows boot, survive network glitches, and never sleep.
#
#    RDP  into VPS
#    Open PowerShell AS ADMIN
#    cd C:\PropBot
#    .\SETUP_VPS_AUTOSTART.ps1
# ======================================================================

$ErrorActionPreference = "Stop"
$ROOT   = "C:\PropBot"
$BATCH  = Join-Path $ROOT "start_live.bat"
# NOTE: task name kept as 'PropBot_v15_live' for backward compatibility
# (so existing VPS installs don't end up with two duplicate scheduled tasks).
# It runs the v30 bot via start_live.bat.
$TASK   = "PropBot_v15_live"
$LOGS   = Join-Path $ROOT "logs"

Write-Host "============================================================"
Write-Host "  PropBot v30  --  VPS autostart setup"
Write-Host "  (task name kept as 'PropBot_v15_live' for compatibility;"
Write-Host "   it launches start_live.bat which now runs v30)"
Write-Host "============================================================`n"

# ----- 0. sanity --------------------------------------------------------
if (-not (Test-Path $BATCH))    { throw "start_live.bat not found at $BATCH" }
if (-not (Test-Path (Join-Path $ROOT ".venv\Scripts\python.exe"))) {
    throw "venv missing. Run BOOTSTRAP_VPS.ps1 first."
}
if (-not (Test-Path $LOGS))     { New-Item -ItemType Directory -Path $LOGS | Out-Null }

# ----- 1. prevent VPS sleeping -----------------------------------------
Write-Host "[1/4] Preventing VPS from sleeping..."
powercfg /change standby-timeout-ac 0
powercfg /change monitor-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /change disk-timeout-ac   0
Write-Host "       sleep/hibernate disabled on AC power.`n"

# ----- 2. register the Task Scheduler entry ----------------------------
Write-Host "[2/4] Registering scheduled task '$TASK'..."
# delete if already exists so script is idempotent
# (cmd /c used so stderr is truly swallowed when task does not yet exist)
cmd /c "schtasks /Delete /TN $TASK /F >nul 2>&1"

$xml = @"
<?xml version='1.0' encoding='UTF-16'?>
<Task version='1.2' xmlns='http://schemas.microsoft.com/windows/2004/02/mit/task'>
  <RegistrationInfo>
    <Description>PropBot v15 live engine (auto-start at boot, restart-on-crash loop in start_live.bat)</Description>
  </RegistrationInfo>
  <Triggers>
    <BootTrigger>
      <Enabled>true</Enabled>
      <Delay>PT1M</Delay>
    </BootTrigger>
    <LogonTrigger>
      <Enabled>true</Enabled>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id='Author'>
      <RunLevel>HighestAvailable</RunLevel>
      <LogonType>InteractiveToken</LogonType>
      <UserId>$env:USERNAME</UserId>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>9999</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context='Author'>
    <Exec>
      <Command>cmd.exe</Command>
      <Arguments>/c "$BATCH"</Arguments>
      <WorkingDirectory>$ROOT</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@

$xmlPath = Join-Path $env:TEMP "propbot_task.xml"
$xml | Out-File -FilePath $xmlPath -Encoding Unicode
$create = cmd /c "schtasks /Create /TN `"$TASK`" /XML `"$xmlPath`" /F 2>&1"
Remove-Item $xmlPath -ErrorAction SilentlyContinue
$verify = cmd /c "schtasks /Query /TN `"$TASK`" 2>&1"
if ($LASTEXITCODE -eq 0) {
    Write-Host "       task '$TASK' registered (boot + logon triggers, restart-on-fail x9999)." -ForegroundColor Green
} else {
    Write-Host "       TASK REGISTRATION FAILED -- schtasks said:" -ForegroundColor Red
    $create | ForEach-Object { "           $_" }
    throw "Task '$TASK' was NOT registered. See error above."
}
Write-Host ""

# ----- 3. open firewall port for EA bridge (loopback only - safer) -----
Write-Host "[3/4] Firewall rule for 127.0.0.1:5555 (EA bridge)..."
New-NetFirewallRule -Name "PropBot_SHF_Bridge" `
                    -DisplayName "PropBot SHF_Bridge (5555)" `
                    -Direction Inbound `
                    -LocalPort 5555 -Protocol TCP `
                    -Action Allow -RemoteAddress 127.0.0.1 `
                    -ErrorAction SilentlyContinue | Out-Null
Write-Host "       port 5555 allowed on loopback.`n"

# ----- 4. summary ------------------------------------------------------
Write-Host "============================================================"
Write-Host "  ALL DONE"
Write-Host "============================================================"
Write-Host "  Task name      : $TASK"
Write-Host "  Triggers       : AT BOOT (1 min delay) + AT LOGON"
Write-Host "  Batch file     : $BATCH"
Write-Host "  Log directory  : $LOGS\live_YYYY-MM-DD.log"
Write-Host ""
Write-Host "USEFUL COMMANDS:"
Write-Host "  Start NOW       : schtasks /Run /TN $TASK"
Write-Host "  Stop            : schtasks /End /TN $TASK"
Write-Host "  Tail latest log : Get-Content logs\live_(Get-Date -f yyyy-MM-dd).log -Wait -Tail 40"
Write-Host "  Kill python     : taskkill /F /IM python.exe"
Write-Host ""
Write-Host "============================================================"
