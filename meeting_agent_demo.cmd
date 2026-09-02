@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\run_meeting_agent_demo.ps1"
exit /b %ERRORLEVEL%
