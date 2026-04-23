@echo off
REM ──────────────────────────────────────────────────
REM Install Callisto Watchdog as Windows Scheduled Task
REM ──────────────────────────────────────────────────
REM
REM Run this ONCE as Administrator:
REM   Right-click -> Run as administrator
REM
REM Creates a scheduled task that:
REM   - Runs at user login
REM   - Restarts on failure (every 30 seconds, up to 999 times)
REM   - Never times out
REM   - Runs whether user is logged in or not

echo === Installing Callisto Watchdog Task ===
echo.

set CALLISTO_DIR=%~dp0..
set PYTHON_EXE=%CALLISTO_DIR%\venv\Scripts\pythonw.exe
if not exist "%PYTHON_EXE%" (
    set PYTHON_EXE=pythonw.exe
)
set WATCHDOG_SCRIPT=%CALLISTO_DIR%\scripts\watchdog.py

echo Callisto Dir: %CALLISTO_DIR%
echo Python:       %PYTHON_EXE%
echo Watchdog:     %WATCHDOG_SCRIPT%
echo.

REM Create the XML task definition
(
echo ^<?xml version="1.0" encoding="UTF-16"?^>
echo ^<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"^>
echo   ^<RegistrationInfo^>
echo     ^<Description^>Callisto Watchdog — monitors and restarts the API server^</Description^>
echo   ^</RegistrationInfo^>
echo   ^<Triggers^>
echo     ^<LogonTrigger^>
echo       ^<Enabled^>true^</Enabled^>
echo     ^</LogonTrigger^>
echo   ^</Triggers^>
echo   ^<Principals^>
echo     ^<Principal id="Author"^>
echo       ^<LogonType^>InteractiveToken^</LogonType^>
echo       ^<RunLevel^>HighestAvailable^</RunLevel^>
echo     ^</Principal^>
echo   ^</Principals^>
echo   ^<Settings^>
echo     ^<MultipleInstancesPolicy^>IgnoreNew^</MultipleInstancesPolicy^>
echo     ^<DisallowStartIfOnBatteries^>false^</DisallowStartIfOnBatteries^>
echo     ^<StopIfGoingOnBatteries^>false^</StopIfGoingOnBatteries^>
echo     ^<ExecutionTimeLimit^>PT0S^</ExecutionTimeLimit^>
echo     ^<RestartOnFailure^>
echo       ^<Interval^>PT30S^</Interval^>
echo       ^<Count^>999^</Count^>
echo     ^</RestartOnFailure^>
echo     ^<Enabled^>true^</Enabled^>
echo   ^</Settings^>
echo   ^<Actions Context="Author"^>
echo     ^<Exec^>
echo       ^<Command^>%PYTHON_EXE%^</Command^>
echo       ^<Arguments^>%WATCHDOG_SCRIPT%^</Arguments^>
echo       ^<WorkingDirectory^>%CALLISTO_DIR%^</WorkingDirectory^>
echo     ^</Exec^>
echo   ^</Actions^>
echo ^</Task^>
) > "%TEMP%\callisto_watchdog_task.xml"

schtasks /Create /TN "Callisto Watchdog" /XML "%TEMP%\callisto_watchdog_task.xml" /F
if errorlevel 1 (
    echo.
    echo [FAIL] Scheduled task creation failed.
    echo        Make sure to run this script as Administrator.
) else (
    echo.
    echo [OK] Scheduled task "Callisto Watchdog" created successfully.
    echo.
    echo The watchdog will:
    echo   - Start automatically at login
    echo   - Restart the API within 30 seconds of any crash
    echo   - Restart ITSELF within 30 seconds if it crashes
    echo   - Log everything to logs\watchdog.log
    echo.
    echo To start now: schtasks /Run /TN "Callisto Watchdog"
    echo To stop:      schtasks /End /TN "Callisto Watchdog"
    echo To remove:    schtasks /Delete /TN "Callisto Watchdog" /F
)

del "%TEMP%\callisto_watchdog_task.xml" >nul 2>&1

echo.
pause
