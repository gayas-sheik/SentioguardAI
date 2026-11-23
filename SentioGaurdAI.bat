
@echo off
title SentioGuardAI - Application Launcher

cd /d "%~dp0"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-SentioguardAI.ps1"

exit /b
