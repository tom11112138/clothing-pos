@echo off
setlocal
cd /d "%~dp0backend"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0backend\start_pos.ps1"
pause
