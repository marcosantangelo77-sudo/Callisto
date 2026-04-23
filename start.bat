@echo off
title Callisto
cd /d "%~dp0"

echo.
echo === Callisto Startup ===
echo Root: %cd%
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
        docker ps --filter "name=callisto-searxng" --format "{{.Status}}" 2>NUL | find "Up" >NUL
        if %ERRORLEVEL%==0 (
            echo [OK] SearXNG started on port 8888
        ) else (
            echo [--] SearXNG unavailable, using Brave Search fallback
        )
    )
) else (
    echo [--] Docker not installed, using Brave Search fallback
)

REM --- 3. Kill any existing Callisto on port 8420 and wait for port release ---
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8420 " ^| findstr "LISTENING"') do (
    echo [..] Killing existing process on port 8420 (PID %%a^)
    taskkill /F /PID %%a >NUL 2>&1
)
REM Wait until port 8420 is actually free (Windows socket TIME_WAIT)
set RETRIES=0
:wait_port
netstat -ano | findstr ":8420 " | findstr "LISTENING" >NUL 2>&1
if %ERRORLEVEL%==0 (
    set /a RETRIES+=1
    if %RETRIES% GEQ 15 (
        echo [!!] Port 8420 still in use after 15s — force continuing
        goto port_ready
    )
    echo [..] Waiting for port 8420 to free... (%RETRIES%s^)
    timeout /t 1 /nobreak >NUL
    goto wait_port
)
:port_ready

echo.
echo === Starting Callisto API ===
echo     API:     http://localhost:8420
echo     Health:  http://localhost:8420/health
echo.
echo     This window must stay open. Close it to stop Callisto.
echo     -------------------------------------------------------
echo.

REM --- 4. Run API in foreground (keeps window open) ---
if exist "venv\Scripts\python.exe" (
    venv\Scripts\python.exe -u -m uvicorn api:app --host 0.0.0.0 --port 8420
) else (
    python -u -m uvicorn api:app --host 0.0.0.0 --port 8420
)

REM If we get here, the API exited
echo.
echo [!!] Callisto API stopped.
pause
