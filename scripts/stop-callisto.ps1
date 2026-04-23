# Callisto Shutdown Script
# Gracefully stops all Callisto services.
# Run with: powershell -ExecutionPolicy Bypass -File scripts\stop-callisto.ps1

$CallistoRoot = Split-Path -Parent $PSScriptRoot

Write-Host "=== Callisto Shutdown ===" -ForegroundColor Yellow

# Stop Callisto API (uvicorn)
$uvicorn = Get-Process -Name "python*" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match "uvicorn.*api:app" }
if ($uvicorn) {
    Write-Host "[..] Stopping Callisto API (PID: $($uvicorn.Id))..." -ForegroundColor Yellow
    Stop-Process -Id $uvicorn.Id -Force
    Write-Host "[OK] API stopped" -ForegroundColor Green
} else {
    Write-Host "[--] Callisto API not running" -ForegroundColor Gray
}

# Stop watchdog if running
$watchdog = Get-Process -Name "powershell*" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match "watchdog" }
if ($watchdog) {
    Write-Host "[..] Stopping watchdog..." -ForegroundColor Yellow
    Stop-Process -Id $watchdog.Id -Force
    Write-Host "[OK] Watchdog stopped" -ForegroundColor Green
}

# Stop SearXNG container
$dockerAvailable = Get-Command docker -ErrorAction SilentlyContinue
if ($dockerAvailable) {
    $container = docker ps --filter "name=callisto-searxng" --format "{{.Names}}" 2>$null
    if ($container) {
        Write-Host "[..] Stopping SearXNG..." -ForegroundColor Yellow
        docker stop callisto-searxng 2>&1 | Out-Null
        Write-Host "[OK] SearXNG stopped" -ForegroundColor Green
    } else {
        Write-Host "[--] SearXNG not running" -ForegroundColor Gray
    }
}

Write-Host ""
Write-Host "=== Callisto Stopped ===" -ForegroundColor Yellow
Write-Host "Note: Ollama left running (shared resource)" -ForegroundColor Gray
