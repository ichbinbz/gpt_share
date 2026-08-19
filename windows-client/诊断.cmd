@echo off
chcp 65001 >nul
title CWS Codex Diagnostics
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0Diagnose-CwsCodex.ps1"
echo.
pause
