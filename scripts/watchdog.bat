@echo off
REM ──────────────────────────────────────────
REM Callisto Watchdog — keeps the system alive
REM ──────────────────────────────────────────
REM
REM This script runs BOTH processes:
REM   1. api.py — main Callisto system (restarted on crash)
REM   2. sentinel.py — Layer 3 watchdog (runs in background)
REM
REM Run this via Windows Task Scheduler at boot,
REM or manually with: scripts\watchdog.bat

title Callisto Watchdog

cd /d "%~dp0\.."

REM Kill any existing sentinel process to avoid duplicates
echo [%date% %time%] Checking for existing sentinel processes...
for /f "tokens=2" %%a in ('tasklist /fi "WINDOWTITLE eq Callisto Sentinel" /fo list 2^>nul ^| findstr "PID:"') do (
    echo [%date% %time%] Killing existing sentinel PID %%a
    taskkill /pid %%a /f >nul 2>&1
)

REM Start sentinel as a background process (Layer 3)
echo [%date% %time%] Starting Sentinel watchdog (Layer 3)...
start "Callisto Sentinel" /min python scripts/sentinel.py

REM Give sentinel a moment to initialize
timeout /t 3 /nobreak >nul

:loop
echo [%date% %time%] Starting Callisto API...
python api.py
echo [%date% %time%] Callisto exited with code %ERRORLEVEL%. Restarting in 15s...
timeout /t 15 /nobreak
goto loop
