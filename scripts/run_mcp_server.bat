@echo off
REM Launch the Callisto MCP stdio server.
REM Any MCP-aware client (Claude Code --mcp-config, Claude Desktop, etc.) can
REM point at this script to talk to the live Callisto HTTP API as MCP tools.

setlocal
if "%CALLISTO_API_URL%"=="" set "CALLISTO_API_URL=http://localhost:8420"

set "SCRIPT_DIR=%~dp0"
set "REPO_DIR=%SCRIPT_DIR%.."

python "%REPO_DIR%\tools\callisto_mcp_server.py" %*
