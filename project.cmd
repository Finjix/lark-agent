@echo off
setlocal
cd /d "%~dp0"
set "PATH=%~dp0runtime\node;%PATH%"
"%~dp0runtime\node\node.exe" "%~dp0runtime\node\node_modules\npm\bin\npm-cli.js" %*
