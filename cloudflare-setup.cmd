@echo off
setlocal
chcp 65001 >nul
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0invitation-console.ps1" -ConfigureCloudflare %*
set "setupExit=%errorlevel%"
echo.
pause
exit /b %setupExit%
