@echo off
chcp 65001 >nul
title CWS Codex Employee Installer
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0Install-CwsCodex.ps1"
if errorlevel 1 (
  echo.
  echo Installation failed. Please send the error above to your administrator.
  pause
)
