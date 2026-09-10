@echo off
title MewType Fan Site (port 9001)
cd /d "%~dp0"

REM check python
where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Please install Python 3.8+
    pause
    exit /b 1
)

REM kill old process on port 9001
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :9001 ^| findstr LISTENING') do (
    echo [INFO] Killing old process on port 9001, PID %%a
    taskkill /PID %%a /F >nul 2>&1
)

set PORT=9001

echo ===============================================
echo   MewType Fan Site
echo   Local : http://localhost:9001/
echo   LAN   : http://YOUR-IP:9001/  (same WiFi)
echo   DB    : %~dp0yumemita-data.db
echo.
echo   Browser opens in 2s. Keep this window OPEN.
echo   Close this window or press Ctrl+C to STOP.
echo ===============================================
echo.

REM open browser after 2s (detached, does not affect server)
start "" /min cmd /c "timeout /t 2 /nobreak >nul & start "" http://localhost:9001/"

REM run server in FOREGROUND (window open = service alive)
python -X utf8 server.py

echo.
echo [INFO] Server stopped.
pause
