@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0publish_launcher.ps1" %*
exit /b %errorlevel%
