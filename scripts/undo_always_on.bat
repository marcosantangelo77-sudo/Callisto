@echo off
REM Undo Callisto always-on settings — restore Windows defaults
REM Run as Administrator

echo === Restoring Windows Default Power Settings ===
echo.

powercfg /change standby-timeout-ac 30
powercfg /change standby-timeout-dc 15
powercfg /change hibernate-timeout-ac 60
powercfg /change monitor-timeout-ac 10
powercfg /change monitor-timeout-dc 5
powercfg /hibernate on

reg delete "HKLM\SOFTWARE\Policies\Microsoft\Windows\Personalization" /v NoLockScreen /f >nul 2>&1
reg delete "HKLM\SOFTWARE\Policies\Microsoft\Power\PowerSettings\0e796bdb-100d-47d6-a2d5-f7d2daa51f51" /f >nul 2>&1
reg delete "HKLM\SYSTEM\CurrentControlSet\Control\Power" /v PlatformAoAcOverride /f >nul 2>&1

powercfg /SETACVALUEINDEX SCHEME_CURRENT SUB_NONE CONSOLELOCK 1 >nul 2>&1
powercfg /SETDCVALUEINDEX SCHEME_CURRENT SUB_NONE CONSOLELOCK 1 >nul 2>&1
powercfg /SETACTIVE SCHEME_CURRENT >nul 2>&1

schtasks /delete /tn "Callisto Watchdog" /f >nul 2>&1

echo Done. Windows defaults restored.
echo Reboot recommended to re-enable Modern Standby.
pause
