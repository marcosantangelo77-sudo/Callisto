@echo off
REM ──────────────────────────────────────────
REM Callisto Watchdog — keeps the system alive
REM ──────────────────────────────────────────
REM
REM This script runs the Python watchdog which:
REM   - Health-checks the API every 15 seconds
REM   - Restarts the API on crash with full error logging
REM   - Handles restart_requested signal file
REM   - Never gives up (exponential backoff, not surrender)
REM
REM If the Python watchdog itself crashes, this bat loop restarts IT.
REM Run this via Windows Task Scheduler at login, or manually.

title Callisto Watchdog

cd /d "%~dp0\.."

:loop
echo [%date% %time%] Starting Callisto Watchdog (Python)...

REM Use venv python if available
if exist "venv\Scripts\python.exe" (
    venv\Scripts\python.exe scripts\watchdog.py
) else (
    python scripts\watchdog.py
)

echo [%date% %time%] Watchdog exited with code %ERRORLEVEL%. Restarting in 10s...
timeout /t 10 /nobreak
goto loop
