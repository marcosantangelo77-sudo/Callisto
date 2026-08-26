# Callisto Startup Script
# Launches all services for 24/7 autonomous operation.
# Run with: powershell -ExecutionPolicy Bypass -File scripts\start-callisto.ps1
#
# Services started:
#   1. Ollama (if not already running)
#   2. SearXNG Docker container (if Docker is available)
#   3. Callisto API server (uvicorn on port 8420)

$CallistoRoot = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $CallistoRoot "logs"
$Timestamp = Get-Date -Format "yyyy-MM-dd_HH-mm-ss"

# Create logs directory
if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
}

Write-Host "=== Callisto Startup ===" -ForegroundColor Cyan
Write-Host "Root: $CallistoRoot"
Write-Host "Time: $Timestamp"
Write-Host ""

# --- 1. Ollama ---
$ollamaProcess = Get-Process -Name "ollama" -ErrorAction SilentlyContinue
if ($ollamaProcess) {
    Write-Host "[OK] Ollama already running (PID: $($ollamaProcess[0].Id))" -ForegroundColor Green
} else {
    Write-Host "[..] Starting Ollama..." -ForegroundColor Yellow
    $ollamaPath = (Get-Command ollama -ErrorAction SilentlyContinue).Source
    if (-not $ollamaPath) {
        $ollamaPath = "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe"
    }
    if (Test-Path $ollamaPath) {
        Start-Process -FilePath $ollamaPath -ArgumentList "serve" -WindowStyle Hidden
        Start-Sleep -Seconds 3
        $check = Get-Process -Name "ollama" -ErrorAction SilentlyContinue
        if ($check) {
            Write-Host "[OK] Ollama started" -ForegroundColor Green
        } else {
            Write-Host "[!!] Ollama failed to start" -ForegroundColor Red
        }
    } else {
        Write-Host "[!!] Ollama not found at $ollamaPath" -ForegroundColor Red
    }
}

# --- 2. SearXNG (Docker) ---
$dockerAvailable = Get-Command docker -ErrorAction SilentlyContinue
if ($dockerAvailable) {
    $container = docker ps --filter "name=callisto-searxng" --format "{{.Status}}" 2>$null
    if ($container) {
        Write-Host "[OK] SearXNG already running ($container)" -ForegroundColor Green
    } else {
        Write-Host "[..] Starting SearXNG Docker container..." -ForegroundColor Yellow
        $composeFile = Join-Path $CallistoRoot "docker-compose.searxng.yml"
        docker compose -f $composeFile up -d 2>&1 | Out-Null
        Start-Sleep -Seconds 5
        $check = docker ps --filter "name=callisto-searxng" --format "{{.Status}}" 2>$null
        if ($check) {
            Write-Host "[OK] SearXNG started on port 8888" -ForegroundColor Green
        } else {
            Write-Host "[!!] SearXNG failed to start (Docker issue?)" -ForegroundColor Red
            Write-Host "     Brave Search API will be used as fallback" -ForegroundColor Yellow
        }
    }
} else {
    Write-Host "[--] Docker not installed — SearXNG unavailable, using Brave Search API" -ForegroundColor Yellow
}

# --- 3. Callisto API ---
Write-Host "[..] Starting Callisto API on port 8420..." -ForegroundColor Yellow
$apiLog = Join-Path $LogDir "api_$Timestamp.log"

# Activate venv if it exists
$venvActivate = Join-Path $CallistoRoot "venv\Scripts\Activate.ps1"
$pythonCmd = "python"
if (Test-Path $venvActivate) {
    $pythonCmd = Join-Path $CallistoRoot "venv\Scripts\python.exe"
}

# Bind to loopback unless CALLISTO_BIND_HOST overrides it
$bindHost = if ($env:CALLISTO_BIND_HOST) { $env:CALLISTO_BIND_HOST } else { "127.0.0.1" }
$apiProcess = Start-Process -FilePath $pythonCmd `
    -ArgumentList "-u", "-m", "uvicorn", "api:app", "--host", $bindHost, "--port", "8420" `
    -WorkingDirectory $CallistoRoot `
    -RedirectStandardOutput $apiLog `
    -RedirectStandardError (Join-Path $LogDir "api_error_$Timestamp.log") `
    -WindowStyle Hidden `
    -PassThru

Start-Sleep -Seconds 3

if (-not $apiProcess.HasExited) {
    Write-Host "[OK] Callisto API started (PID: $($apiProcess.Id))" -ForegroundColor Green
    Write-Host "     Log: $apiLog" -ForegroundColor Gray
} else {
    Write-Host "[!!] Callisto API exited immediately — check $apiLog" -ForegroundColor Red
}

Write-Host ""
Write-Host "=== Callisto Ready ===" -ForegroundColor Cyan
Write-Host "API:     http://localhost:8420" -ForegroundColor White
Write-Host "Health:  http://localhost:8420/health" -ForegroundColor White
Write-Host "SearXNG: http://localhost:8888" -ForegroundColor White
Write-Host ""
