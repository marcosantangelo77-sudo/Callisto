@echo off
REM Usage: ask.bat "your question here"
cd /d "%~dp0"
powershell -ExecutionPolicy Bypass -File scripts\ask.ps1 %*
