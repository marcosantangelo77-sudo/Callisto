@echo off
REM ──────────────────────────────────────────────────
REM Callisto Always-On Setup — prevents Windows from
REM sleeping, locking, or throttling background processes
REM ──────────────────────────────────────────────────
REM
REM Run this ONCE as Administrator:
REM   Right-click -> Run as administrator
REM
REM REQUIRES REBOOT after running (Modern Standby override)
REM

echo === Callisto Always-On Configuration ===
echo.

REM 1. Disable sleep on AC power (0 = never)
echo [1/9] Disabling sleep...
powercfg /change standby-timeout-ac 0
powercfg /change standby-timeout-dc 0

REM 2. Disable hibernate
echo [2/9] Disabling hibernate...
powercfg /change hibernate-timeout-ac 0
powercfg /hibernate off

REM 3. Disable monitor timeout (0 = never)
echo [3/9] Disabling monitor timeout...
powercfg /change monitor-timeout-ac 0
powercfg /change monitor-timeout-dc 0

REM 4. Disable lock screen
echo [4/9] Disabling automatic lock screen...
reg add "HKLM\SOFTWARE\Policies\Microsoft\Windows\Personalization" /v NoLockScreen /t REG_DWORD /d 1 /f >nul 2>&1

REM 5. Disable "require sign-in after sleep"
echo [5/9] Disabling sign-in requirement...
reg add "HKLM\SOFTWARE\Policies\Microsoft\Power\PowerSettings\0e796bdb-100d-47d6-a2d5-f7d2daa51f51" /v ACSettingIndex /t REG_DWORD /d 0 /f >nul 2>&1
reg add "HKLM\SOFTWARE\Policies\Microsoft\Power\PowerSettings\0e796bdb-100d-47d6-a2d5-f7d2daa51f51" /v DCSettingIndex /t REG_DWORD /d 0 /f >nul 2>&1
powercfg /SETACVALUEINDEX SCHEME_CURRENT SUB_NONE CONSOLELOCK 0 >nul 2>&1
powercfg /SETDCVALUEINDEX SCHEME_CURRENT SUB_NONE CONSOLELOCK 0 >nul 2>&1

REM 6. Disable Modern Standby / Connected Standby (CRITICAL)
REM    Without this, Windows can throttle background processes
echo [6/9] Disabling Modern Standby (requires reboot)...
reg add "HKLM\SYSTEM\CurrentControlSet\Control\Power" /v PlatformAoAcOverride /t REG_DWORD /d 0 /f >nul 2>&1

REM 7. Disable USB selective suspend + network adapter power management
echo [7/9] Disabling USB/network power throttling...
powercfg /SETACVALUEINDEX SCHEME_CURRENT 2a737441-1930-4402-8d77-b2bebba308a3 48e6b7a6-50f5-4782-a5d4-53bb8f07e226 0 >nul 2>&1
powercfg /SETACVALUEINDEX SCHEME_CURRENT 19cbb8fa-5279-450e-9fac-8a3d5fedd0c1 12bbebe6-58d6-4636-95bb-3217ef867c1a 0 >nul 2>&1

REM 8. Apply all power changes
echo [8/9] Applying power scheme...
powercfg /SETACTIVE SCHEME_CURRENT >nul 2>&1

REM 9. Create scheduled task — auto-restart on crash, runs at boot
echo [9/9] Creating Callisto scheduled task...
set CALLISTO_DIR=%~dp0..

(
echo ^<?xml version="1.0" encoding="UTF-16"?^>
echo ^<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"^>
echo   ^<RegistrationInfo^>
echo     ^<Description^>Callisto API — auto-restarts on failure^</Description^>
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
echo       ^<Command^>%CALLISTO_DIR%\scripts\watchdog.bat^</Command^>
echo       ^<WorkingDirectory^>%CALLISTO_DIR%^</WorkingDirectory^>
echo     ^</Exec^>
echo   ^</Actions^>
echo ^</Task^>
) > "%TEMP%\callisto_task.xml"

schtasks /Create /TN "Callisto Watchdog" /XML "%TEMP%\callisto_task.xml" /F >nul 2>&1
if errorlevel 1 (
    echo     WARNING: Scheduled task creation failed. Run as admin.
) else (
    echo     Scheduled task created: Callisto Watchdog (runs at login, restarts on crash)
)
del "%TEMP%\callisto_task.xml" >nul 2>&1

echo.
echo === Configuration Complete ===
echo.
echo Your system will now:
echo   - Never sleep, hibernate, or turn off the monitor
echo   - Never lock the screen or require sign-in
echo   - Never throttle background processes (Modern Standby disabled)
echo   - Auto-start Callisto watchdog at login
echo   - Auto-restart Callisto within 30s if it crashes (up to 999 times)
echo.
echo IMPORTANT: Reboot required for Modern Standby override to take effect.
echo.
echo To UNDO: scripts\undo_always_on.bat
echo.
pause
