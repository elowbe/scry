@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\bootstrap.ps1" %*
if errorlevel 1 echo Setup failed. See the message above, fix the issue and rerun Setup.cmd.
pause
