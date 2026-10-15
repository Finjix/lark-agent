@echo off
setlocal
cd /d "%~dp0"
"%~dp0runtime\node\node.exe" "%~dp0dist\main.js" %*
if errorlevel 1 pause
