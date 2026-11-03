<#
.SYNOPSIS
  Start the BATS UI server (and its scheduler) for unattended paper trading, or install it as a Windows task.

.DESCRIPTION
  Run from anywhere; the repo is the folder above this script, and the server runs with the repo's .venv.

    powershell -ExecutionPolicy Bypass -File scripts\start_bats.ps1              # start now, in the foreground
    powershell -ExecutionPolicy Bypass -File scripts\start_bats.ps1 -Install     # register the "BATS server" task
    powershell -ExecutionPolicy Bypass -File scripts\start_bats.ps1 -Uninstall   # remove the task

  A start:
    - exits at once (code 0) when something already listens on the port, so a second start is harmless,
    - runs the read-only setup check (python -m extensions.scripts.check_setup) into
      <data home>\logs\server_<yyyyMMdd_HHmmss>.check.log,
    - runs python -m ui.server --port <Port> in the foreground, its output in server_<stamp>.log and
      server_<stamp>.err.log beside it; logs older than 30 days are deleted,
    - exits with the server's exit code, so Task Scheduler sees a crash as a failure and restarts it.

  The task ("BATS server") runs this script hidden, as the current user, only while that user is logged on (no
  password is stored): at logon and on weekdays at 08:30, waking the computer for the 08:30 start when Windows
  allows wake timers. It restarts every 5 minutes up to 5 times after a failure, has no run-time limit, starts
  as soon as possible after a missed start, and ignores a new start while it runs.

  The data home is TRADINGAGENTS_UI_HOME when set, else %USERPROFILE%\.tradingagents. Nothing here sends an
  order: the scheduler does that only in tradebot auto mode with paper auto-send on (see docs\go-live-runbook.md).
#>
[CmdletBinding()]
param(
    [switch]$Install,
    [switch]$Uninstall,
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"
$TaskName = "BATS server"
$LogKeepDays = 30
$Repo = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Repo ".venv\Scripts\python.exe"
$Script = Join-Path $PSScriptRoot "start_bats.ps1"
if ($env:TRADINGAGENTS_UI_HOME) { $DataHome = $env:TRADINGAGENTS_UI_HOME } else { $DataHome = Join-Path $env:USERPROFILE ".tradingagents" }
$LogDir = Join-Path $DataHome "logs"

function Install-BatsTask {
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -WorkingDirectory $Repo `
        -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Script`" -Port $Port"
    $atLogon = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
    $weekdays = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At "08:30"
    $settings = New-ScheduledTaskSettingsSet `
        -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 5) `
        -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -StartWhenAvailable -WakeToRun `
        -MultipleInstances IgnoreNew `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger @($atLogon, $weekdays) -Settings $settings `
        -Principal $principal -Description "BATS paper trading UI server and scheduler ($Repo)" -Force | Out-Null
    Write-Output "registered task '$TaskName': at logon and weekdays 08:30 (wakes the PC), restart 5x every 5 min"
    Write-Output "it runs only while $env:USERNAME is logged on; check it with: Get-ScheduledTask -TaskName '$TaskName'"
}

if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Output "removed task '$TaskName' (if it existed)"
    exit 0
}
if (-not (Test-Path $Python)) {
    Write-Error "no virtual environment at $Python; create it in $Repo first (see ui\README.md)"
    exit 2
}
if ($Install) {
    Install-BatsTask
    exit 0
}

# Already running: a second start (logon and 08:30 on the same morning) is not an error.
$listening = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($listening) {
    Write-Output "port $Port is already in use (process $($listening[0].OwningProcess)); BATS is running, nothing to do"
    exit 0
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
Get-ChildItem -Path $LogDir -Filter "server_*" -ErrorAction SilentlyContinue |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-$LogKeepDays) } |
    Remove-Item -Force -Confirm:$false -ErrorAction SilentlyContinue
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$log = Join-Path $LogDir "server_$stamp.log"
$errLog = Join-Path $LogDir "server_$stamp.err.log"
$checkLog = Join-Path $LogDir "server_$stamp.check.log"

Set-Location $Repo
$env:PYTHONUNBUFFERED = "1"
$env:PYTHONIOENCODING = "utf-8"

$check = Start-Process -FilePath $Python -ArgumentList "-m", "extensions.scripts.check_setup" -WorkingDirectory $Repo `
    -RedirectStandardOutput $checkLog -RedirectStandardError "$checkLog.err" -NoNewWindow -Wait -PassThru
if ($check.ExitCode -ne 0) {
    Write-Output "setup check found a FAIL (see $checkLog); starting the server anyway so the UI shows it"
}

Write-Output "starting BATS on http://127.0.0.1:$Port (log: $log)"
$server = Start-Process -FilePath $Python -ArgumentList "-m", "ui.server", "--port", "$Port" -WorkingDirectory $Repo `
    -RedirectStandardOutput $log -RedirectStandardError $errLog -NoNewWindow -Wait -PassThru
Write-Output "server exited with code $($server.ExitCode) at $(Get-Date -Format o)"
exit $server.ExitCode
