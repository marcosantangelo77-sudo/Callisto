# Install Callisto Auto-Start (Windows Task Scheduler)
# MUST be run as Administrator.
# Creates two scheduled tasks:
#   1. "Callisto Startup" — runs start-callisto.ps1 on user logon
#   2. "Callisto Watchdog" — runs watchdog.ps1 on user logon
#
# Run with: powershell -ExecutionPolicy Bypass -File scripts\install-autostart.ps1

$CallistoRoot = Split-Path -Parent $PSScriptRoot
$ScriptsDir = Join-Path $CallistoRoot "scripts"
$User = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

# Check admin
$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "ERROR: This script must be run as Administrator." -ForegroundColor Red
    Write-Host "Right-click PowerShell -> Run as Administrator, then re-run." -ForegroundColor Yellow
    exit 1
}

Write-Host "=== Installing Callisto Auto-Start ===" -ForegroundColor Cyan
Write-Host "User: $User"
Write-Host "Root: $CallistoRoot"
Write-Host ""

# --- Task 1: Startup ---
$startupAction = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-ExecutionPolicy Bypass -WindowStyle Hidden -File `"$ScriptsDir\start-callisto.ps1`"" `
    -WorkingDirectory $CallistoRoot

$startupTrigger = New-ScheduledTaskTrigger -AtLogOn -User $User

$startupSettings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::FromMinutes(5))

Register-ScheduledTask `
    -TaskName "Callisto Startup" `
    -Action $startupAction `
    -Trigger $startupTrigger `
    -Settings $startupSettings `
    -Description "Start Callisto services (Ollama, SearXNG, API) on login" `
    -Force | Out-Null

Write-Host "[OK] 'Callisto Startup' task registered" -ForegroundColor Green

# --- Task 2: Watchdog ---
$watchdogAction = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-ExecutionPolicy Bypass -WindowStyle Hidden -File `"$ScriptsDir\watchdog.ps1`"" `
    -WorkingDirectory $CallistoRoot

$watchdogTrigger = New-ScheduledTaskTrigger -AtLogOn -User $User

$watchdogSettings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval ([TimeSpan]::FromMinutes(1)) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask `
    -TaskName "Callisto Watchdog" `
    -Action $watchdogAction `
    -Trigger $watchdogTrigger `
    -Settings $watchdogSettings `
    -Description "Monitor Callisto services and restart on failure" `
    -Force | Out-Null

Write-Host "[OK] 'Callisto Watchdog' task registered" -ForegroundColor Green

Write-Host ""
Write-Host "=== Auto-Start Installed ===" -ForegroundColor Cyan
Write-Host "Both tasks will run at login." -ForegroundColor White
Write-Host "To remove: Unregister-ScheduledTask -TaskName 'Callisto Startup'" -ForegroundColor Gray
Write-Host "To remove: Unregister-ScheduledTask -TaskName 'Callisto Watchdog'" -ForegroundColor Gray
