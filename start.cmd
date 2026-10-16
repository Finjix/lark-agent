@echo off
cd /d "%~dp0"
if not exist node_modules call npm ci || goto :fail
call npm run build || goto :fail
node dist\main.js %*
if errorlevel 1 goto :fail
exit /b 0
:fail
pause
exit /b 1
