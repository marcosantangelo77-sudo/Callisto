# Callisto Watchdog
# Monitors all services and restarts them if they crash.
# Run with: powershell -ExecutionPolicy Bypass -File scripts\watchdog.ps1
#
# Checks every 60 seconds:
#   - Ollama process alive
#   - Callisto API responding on :8420
#   - SearXNG responding on :8888 (if Docker available)

$CallistoRoot = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $CallistoRoot "logs"
$CheckInterval = 60  # seconds
$WatchdogLog = Join-Path $LogDir "watchdog.log"

if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
}

function Log($msg) {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "[$ts] $msg"
    Write-Host $line
    Add-Content -Path $WatchdogLog -Value $line
}

function Test-Endpoint($url) {
    try {
        $response = Invoke-WebRequest -Uri $url -TimeoutSec 5 -UseBasicParsing -ErrorAction Stop
        return $response.StatusCode -eq 200
    } catch {
        return $false
    }
}

Log "Watchdog started — monitoring Callisto services"
Log "Check interval: ${CheckInterval}s"
Log "Log: $WatchdogLog"

while ($true) {
    # --- Check Ollama ---
    $ollamaProc = Get-Process -Name "ollama" -ErrorAction SilentlyContinue
    if (-not $ollamaProc) {
        Log "ALERT: Ollama not running — restarting"
        $ollamaPath = "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe"
        if (Test-Path $ollamaPath) {
            Start-Process -FilePath $ollamaPath -ArgumentList "serve" -WindowStyle Hidden
            Start-Sleep -Seconds 5
            $check = Get-Process -Name "ollama" -ErrorAction SilentlyContinue
            if ($check) { Log "OK: Ollama restarted" }
            else { Log "FAIL: Ollama restart failed" }
        } else {
            Log "FAIL: Ollama binary not found"
        }
    }

    # --- Check Callisto API ---
    if (-not (Test-Endpoint "http://localhost:8420/health")) {
        Log "ALERT: Callisto API not responding — restarting"
        $ts = Get-Date -Format "yyyy-MM-dd_HH-mm-ss"
        $apiLog = Join-Path $LogDir "api_restart_$ts.log"

        $pythonCmd = "python"
        $venvPython = Join-Path $CallistoRoot "venv\Scripts\python.exe"
        if (Test-Path $venvPython) { $pythonCmd = $venvPython }

        # Bind to loopback unless CALLISTO_BIND_HOST overrides it
        $bindHost = if ($env:CALLISTO_BIND_HOST) { $env:CALLISTO_BIND_HOST } else { "127.0.0.1" }
        Start-Process -FilePath $pythonCmd `
            -ArgumentList "-u", "-m", "uvicorn", "api:app", "--host", $bindHost, "--port", "8420" `
            -WorkingDirectory $CallistoRoot `
            -RedirectStandardOutput $apiLog `
            -RedirectStandardError (Join-Path $LogDir "api_error_restart_$ts.log") `
            -WindowStyle Hidden

        Start-Sleep -Seconds 5
        if (Test-Endpoint "http://localhost:8420/health") {
            Log "OK: Callisto API restarted"
        } else {
            Log "FAIL: Callisto API restart failed — check $apiLog"
        }
    }

    # --- Check SearXNG ---
    $dockerAvailable = Get-Command docker -ErrorAction SilentlyContinue
    if ($dockerAvailable) {
        if (-not (Test-Endpoint "http://localhost:8888/healthz")) {
            $container = docker ps -a --filter "name=callisto-searxng" --format "{{.Status}}" 2>$null
            if ($container -and $container -notmatch "Up") {
                Log "ALERT: SearXNG container stopped — restarting"
                docker start callisto-searxng 2>&1 | Out-Null
                Start-Sleep -Seconds 5
                if (Test-Endpoint "http://localhost:8888/healthz") {
                    Log "OK: SearXNG restarted"
                } else {
                    Log "FAIL: SearXNG restart failed"
                }
            }
        }
    }

    Start-Sleep -Seconds $CheckInterval
}
