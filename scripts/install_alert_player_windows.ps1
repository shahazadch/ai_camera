# Start scripts\alert_player.py automatically at login on Windows (no window).
#
#   powershell -ExecutionPolicy Bypass -File scripts\install_alert_player_windows.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\install_alert_player_windows.ps1 -Uninstall
#
# Uses ALERT_PLAYER_KEY / SENTINEL_API from the environment if set.
# Log: %USERPROFILE%\.sentinelvision\alert_player.log
param([switch]$Uninstall)

$ErrorActionPreference = "Stop"
$TaskName = "SentinelVision Alert Player"

if ($Uninstall) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Alert player removed."
    exit 0
}

# pythonw runs without a console window; the "pyw" launcher is the fallback.
$python = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
$prefix = @()
if (-not $python) {
    $python = (Get-Command pyw.exe -ErrorAction SilentlyContinue).Source
    $prefix = @("-3")
}
if (-not $python) {
    Write-Error "Python 3 not found. Install it from python.org and tick 'Add Python to PATH'."
}

$root = Split-Path -Parent $PSScriptRoot
$player = Join-Path $root "scripts\alert_player.py"
$log = Join-Path $env:USERPROFILE ".sentinelvision\alert_player.log"
$api = if ($env:SENTINEL_API) { $env:SENTINEL_API } else { "http://localhost:8000/api/v1" }
$key = if ($env:ALERT_PLAYER_KEY) { $env:ALERT_PLAYER_KEY } else { "alert-dev-key-change-me" }

$arguments = ($prefix + @("`"$player`"", "--api", "`"$api`"", "--key", "`"$key`"", "--log", "`"$log`"")) -join " "
$action = New-ScheduledTaskAction -Execute $python -Argument $arguments -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
# Keep running for days, restart if it ever stops, run on battery too.
$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "Plays SentinelVision camera rule sounds and announcements." -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

Write-Host "Alert player installed; it starts at login. Log: $log"
