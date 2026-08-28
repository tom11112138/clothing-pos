@echo off
setlocal
cd /d "%~dp0backend"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0backend\backup_postgres.ps1"
pause
