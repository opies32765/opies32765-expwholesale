@echo off
REM =====================================================================
REM  install_ewworker_service.bat
REM  Configures the EWWorker NSSM service on a Playwright-based VM worker.
REM  Idempotent: safe to re-run after VM clone (will re-apply settings).
REM
REM  Prereqs on the VM:
REM    - Python 3.11 installed at "C:\Program Files\Python311\python.exe"
REM    - NSSM 2.24 at C:\Tools\nssm.exe
REM    - Local Windows user "worker-1" with password "Sedecrem3"
REM    - C:\worker created (worker_main.py will be dropped in by deploy)
REM
REM  Usage:  Right-click  -> Run as administrator
REM          (or run from an elevated cmd prompt)
REM
REM  Per memory project_ew_chrome_shepherd.md: service must run as the
REM  auto-logged-in user (NOT LocalSystem) so Chrome can render in the
REM  active Session 1 desktop for screenshots.
REM =====================================================================

setlocal
set NSSM=C:\Tools\nssm.exe
set SVC=EWWorker
set PYEXE=C:\Program Files\Python311\python.exe
set WORKDIR=C:\worker
set LOGDIR=C:\worker\logs
set LOGFILE=C:\worker\logs\worker_service.log
set RUNUSER=.\worker-1
set RUNPASS=Sedecrem3
REM CLONE NOTE: change WORKER_ID per VM clone (vm-worker-1, vm-worker-2, ...)
set WORKER_ID=vm-worker-1

echo === Verifying prereqs ===
if not exist "%NSSM%" ( echo ERROR: nssm.exe missing at %NSSM% & exit /b 1 )
if not exist "%PYEXE%" ( echo ERROR: python.exe missing at %PYEXE% & exit /b 1 )
if not exist "%WORKDIR%" mkdir "%WORKDIR%"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"

echo === Removing any existing %SVC% service (idempotent) ===
"%NSSM%" stop %SVC% >nul 2>&1
"%NSSM%" remove %SVC% confirm >nul 2>&1

echo === Installing %SVC% ===
"%NSSM%" install %SVC% "%PYEXE%" "-u %WORKDIR%\worker_main.py"
if errorlevel 1 ( echo NSSM install failed & exit /b 1 )

echo === Configuring %SVC% ===
"%NSSM%" set %SVC% AppDirectory "%WORKDIR%"
"%NSSM%" set %SVC% DisplayName "EW Worker (Playwright)"
"%NSSM%" set %SVC% Description "Experience Wholesale VM-based Playwright worker"
"%NSSM%" set %SVC% Start SERVICE_AUTO_START

REM --- Logging with rotation ---
"%NSSM%" set %SVC% AppStdout "%LOGFILE%"
"%NSSM%" set %SVC% AppStderr "%LOGFILE%"
"%NSSM%" set %SVC% AppRotateFiles 1
"%NSSM%" set %SVC% AppRotateOnline 1
"%NSSM%" set %SVC% AppRotateBytes 10485760

REM --- Auto-restart on crash, 5-second delay ---
"%NSSM%" set %SVC% AppExit Default Restart
"%NSSM%" set %SVC% AppRestartDelay 5000

REM --- Per-VM unique worker id (change me on each clone) ---
"%NSSM%" set %SVC% AppEnvironmentExtra WORKER_ID=%WORKER_ID%

REM --- Run as worker-1 so Chrome can paint in Session 1 ---
"%NSSM%" set %SVC% ObjectName %RUNUSER% %RUNPASS%
if errorlevel 1 (
    echo WARN: ObjectName set failed. Ensure worker-1 has "Log on as a service".
    echo       Run:  secedit /export /cfg C:\sec.cfg ^&^& notepad C:\sec.cfg
    echo       Append worker-1 to SeServiceLogonRight then secedit /configure /db secedit.sdb /cfg C:\sec.cfg
)

echo === Bonus hardening (idempotent) ===
REM Auto-login so Session 1 desktop is always live for Chrome rendering
reg add "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon" /v AutoAdminLogon /t REG_SZ /d 1 /f >nul
reg add "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon" /v DefaultUserName /t REG_SZ /d worker-1 /f >nul
reg add "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon" /v DefaultPassword /t REG_SZ /d %RUNPASS% /f >nul

REM Disable Windows Update (worker doesn't need it; reboots break sessions)
sc config wuauserv start= disabled >nul
sc stop wuauserv >nul 2>&1

echo === Final config (selected params) ===
for %%P in (Application AppParameters AppDirectory AppStdout AppStderr AppRotateFiles AppRotateOnline AppRotateBytes AppRestartDelay AppEnvironmentExtra ObjectName Start) do (
    "%NSSM%" get %SVC% %%P
)
"%NSSM%" get %SVC% AppExit Default

echo.
echo === Done. Service is configured but NOT started. ===
echo To start once worker_main.py is in place:
echo    nssm start %SVC%
echo.
endlocal
