@echo off
chcp 65001 >nul
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0Launch-CwsCodex.ps1"
if errorlevel 1 pause
