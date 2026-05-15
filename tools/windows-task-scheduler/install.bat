@echo off
:: Copyright (c) 2026 Jiawen ^<Jiawen.Cc@outlook.com^>
:: Licensed under the Elastic License 2.0 — see LICENSE in the project root.
::
:: Install Compass Gmail background sync on Windows via Task Scheduler.
:: Run from the Compass project root: tools\windows-task-scheduler\install.bat
::
:: No admin required (task runs under your own user).
:: First fire: 5 minutes after your next logon, then every 4 hours.

setlocal enabledelayedexpansion
cd /d "%~dp0\..\.."
set "PROJECT_ROOT=%CD%"
set "TEMPLATE=%PROJECT_ROOT%\tools\windows-task-scheduler\CompassGmailSync.xml.template"
set "RENDERED=%TEMP%\CompassGmailSync.xml"

:: Detect venv python
if exist "%PROJECT_ROOT%\.venv\Scripts\python.exe" (
    set "PYTHON=%PROJECT_ROOT%\.venv\Scripts\python.exe"
) else if exist "%PROJECT_ROOT%\.venv-quick\Scripts\python.exe" (
    set "PYTHON=%PROJECT_ROOT%\.venv-quick\Scripts\python.exe"
) else (
    where python >nul 2>&1
    if errorlevel 1 (
        echo Error: No python found. Install Python 3.12+ first.
        exit /b 1
    )
    for /f "delims=" %%i in ('where python') do set "PYTHON=%%i"
    echo Warning: Using system python — make sure deps installed.
)

echo   PROJECT_ROOT = %PROJECT_ROOT%
echo   PYTHON       = %PYTHON%

:: Render template (PowerShell substitution — handles UTF-16 + path escapes)
powershell -NoProfile -Command ^
  "(Get-Content -Raw '%TEMPLATE%') -replace '__PYTHON__', '%PYTHON:\=\\%' -replace '__PROJECT_ROOT__', '%PROJECT_ROOT:\=\\%' | Set-Content -Encoding Unicode '%RENDERED%'"

:: Ensure log dir
if not exist "%PROJECT_ROOT%\data\logs" mkdir "%PROJECT_ROOT%\data\logs"

:: Delete any old version (idempotent)
schtasks /Delete /TN "CompassGmailSync" /F >nul 2>&1

:: Create task from rendered XML
schtasks /Create /TN "CompassGmailSync" /XML "%RENDERED%"
if errorlevel 1 (
    echo Error: Task creation failed.
    exit /b 1
)

echo.
echo Installed Task Scheduler entry: CompassGmailSync
echo    First fire: 5 minutes after next logon, then every 4 hours
echo.
echo Useful commands:
echo   • Trigger now:    schtasks /Run /TN "CompassGmailSync"
echo   • Check status:   schtasks /Query /TN "CompassGmailSync" /V /FO LIST
echo   • See logs:       type "%PROJECT_ROOT%\data\logs\gmail-sync.out.log"
echo   • Uninstall:      tools\windows-task-scheduler\uninstall.bat
endlocal
