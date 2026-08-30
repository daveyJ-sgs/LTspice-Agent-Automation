@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-SystemBuilder.ps1" %*
exit /b %ERRORLEVEL%
