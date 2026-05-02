@echo off
REM First-time setup for a fresh VM clone.
REM Runs Cox/AccuTrade/iPacket logins (you do 2FA), then starts the worker service.

echo === Step 1/3: Cox / vAuto login ===
echo IMPORTANT: when 2FA prompt appears, do 2FA + check "Trust this device"
echo The script will auto-detect when you reach the dashboard.
echo.
"C:\Program Files\Python311\python.exe" C:\worker\vauto_login.py
if errorlevel 1 goto :err

echo.
echo === Step 2/3: AccuTrade login ===
"C:\Program Files\Python311\python.exe" C:\worker\accutrade_login.py
if errorlevel 1 goto :err

echo.
echo === Step 3/3: iPacket login ===
"C:\Program Files\Python311\python.exe" C:\worker\ipacket_login.py
if errorlevel 1 goto :err

echo.
echo === Starting EWWorker service + setting auto-start on boot ===
"C:\Tools\nssm.exe" set EWWorker Start SERVICE_AUTO_START
"C:\Tools\nssm.exe" start EWWorker
echo.
echo === ALL DONE — this VM is now an active worker ===
pause
exit /b 0

:err
echo === SETUP FAILED — check the error above ===
pause
exit /b 1
