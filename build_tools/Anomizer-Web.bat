@echo off
setlocal
cd /d "%~dp0"
anomizer-web.exe %*
if errorlevel 1 pause
endlocal
