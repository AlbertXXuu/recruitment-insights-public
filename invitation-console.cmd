@echo off
setlocal
chcp 65001 >nul
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0invitation-console.ps1" %*
set "invitationConsoleExit=%ERRORLEVEL%"
if not "%invitationConsoleExit%"=="0" pause
exit /b %invitationConsoleExit%
