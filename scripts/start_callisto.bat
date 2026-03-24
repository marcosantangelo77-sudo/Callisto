@echo off
REM ──────────────────────────────────────────────────────
REM Callisto Start — launches the watchdog + API system
REM ──────────────────────────────────────────────────────
REM
REM This starts the watchdog, which starts and monitors the API.
REM The watchdog handles:
REM   - Starting the API
REM   - Health checking every 15s
REM   - Auto-restart on crash
REM   - Signal file restart on code changes
REM
REM Can be run from Task Scheduler, startup folder, or manually.
REM Safe to run multiple times — the watchdog enforces single instance.

title Callisto System

cd /d "%~dp0\.."

echo.
echo ============================================
echo   Callisto Autonomous System
echo ============================================
echo.

REM --- 1. Ollama ---
tasklist /FI "IMAGENAME eq ollama.exe" 2>NUL | find /I "ollama.exe" >NUL
if %ERRORLEVEL%==0 (
    echo [OK] Ollama already running
) else (
    echo [..] Starting Ollama...
    if exist "%LOCALAPPDATA%\Programs\Ollama\ollama.exe" (
        start "" /B "%LOCALAPPDATA%\Programs\Ollama\ollama.exe" serve >NUL 2>&1
        timeout /t 3 /nobreak >NUL
        echo [OK] Ollama started
    ) else (
        where ollama >NUL 2>&1
        if %ERRORLEVEL%==0 (
            start "" /B ollama serve >NUL 2>&1
            timeout /t 3 /nobreak >NUL
            echo [OK] Ollama started
        ) else (
            echo [!!] Ollama not found
        )
    )
)

REM --- 2. SearXNG (Docker) ---
where docker >NUL 2>&1
if %ERRORLEVEL%==0 (
    docker ps --filter "name=callisto-searxng" --format "{{.Status}}" 2>NUL | find "Up" >NUL
    if %ERRORLEVEL%==0 (
        echo [OK] SearXNG already running
    ) else (
        echo [..] Starting SearXNG...
        docker compose -f docker-compose.searxng.yml up -d >NUL 2>&1
        timeout /t 5 /nobreak >NUL
        echo [OK] SearXNG started
    )
) else (
    echo [--] Docker not installed, SearXNG unavailable
)

REM --- 3. Check if watchdog already running ---
REM The Python watchdog enforces single instance via PID file,
REM but we also check here to avoid opening duplicate windows.
if exist "memory\watchdog.pid" (
    set /p WD_PID=<memory\watchdog.pid
    tasklist /FI "PID eq %WD_PID%" /FO CSV /NH 2>NUL | find "python" >NUL
    if %ERRORLEVEL%==0 (
        echo [OK] Watchdog already running (PID %WD_PID%)
        echo.
        echo Callisto is already running. The watchdog will keep it alive.
        echo Close this window - it is safe to do so.
        timeout /t 5
        exit /b 0
    )
)

echo.
echo [..] Starting Callisto Watchdog...
echo     The watchdog will start and monitor the API.
echo     API:     http://localhost:8420
echo     Health:  http://localhost:8420/health
echo.
echo     This window runs the watchdog. Closing it stops monitoring.
echo     (Use Task Scheduler for headless 24/7 operation)
echo     ─────────────────────────────────────────────────
echo.

REM --- 4. Run watchdog (bat loop wraps Python watchdog for double resilience) ---
call "%~dp0watchdog.bat"
