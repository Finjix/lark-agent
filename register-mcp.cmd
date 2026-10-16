@echo off
rem One-click registration of the lark_agent MCP server in Codex's config.toml (%USERPROFILE%\.codex by default).
rem   register-mcp.cmd          register or update
rem   register-mcp.cmd remove   undo (only the marked block is removed)
setlocal
cd /d "%~dp0"
if not exist node_modules call npm ci || goto :fail
call npm run build || goto :fail
node dist\main.js register-mcp %*
if errorlevel 1 goto :fail
echo.
echo Done. Restart Codex Desktop so it loads the change. The tools work while start.cmd is running.
call :hold
exit /b 0

:fail
echo.
echo Failed. See the message above.
call :hold
exit /b 1

:hold
rem Keep the window open only when the script was started by double-click.
echo %cmdcmdline% | "%SystemRoot%\System32\find.exe" /i "%~0" >nul && pause
exit /b 0
