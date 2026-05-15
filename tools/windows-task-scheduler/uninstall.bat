@echo off
:: Copyright (c) 2026 Jiawen ^<Jiawen.Cc@outlook.com^>
:: Licensed under the Elastic License 2.0 — see LICENSE in the project root.

schtasks /Delete /TN "CompassGmailSync" /F
if errorlevel 1 (
    echo No task named CompassGmailSync found — nothing to remove.
    exit /b 0
)
echo Removed CompassGmailSync from Task Scheduler.
echo    Log files left in data\logs\ — delete manually if you want.
