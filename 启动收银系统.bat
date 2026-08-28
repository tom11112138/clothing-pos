@echo off
title Clothing POS System
cd /d "%~dp0backend"

echo ============================================
echo        Clothing POS System - Starting
echo ============================================
echo.

REM ---- 1. Check Python ----
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found.
    echo Please install Python 3.9+ from https://www.python.org/downloads/
    echo and check "Add Python to PATH" during install, then run this again.
    echo.
    pause
    exit /b
)

REM ---- 2. Install dependencies (first run is slow, later runs skip fast) ----
echo Checking / installing dependencies (first run may take a while)...
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo [ERROR] Failed to install dependencies. Check your network and retry.
    echo.
    pause
    exit /b
)

REM ---- 3. Get LAN IP (so other cashier machines can connect) ----
set "LANIP="
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /c:"IPv4"') do (
    if not defined LANIP set "LANIP=%%a"
)
set "LANIP=%LANIP: =%"

echo.
echo ============================================
echo   Open one of these URLs in your browser:
echo.
echo     This PC:        http://127.0.0.1:8000
if defined LANIP echo     Other machines: http://%LANIP%:8000
echo.
echo   Close this window to stop the system.
echo ============================================
echo.

REM ---- 4. Start the server ----
python -m uvicorn main:app --host 0.0.0.0 --port 8000

pause
