@echo off
setlocal
cd /d "%~dp0"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_dev.ps1" -Demo
set "launcher_exit_code=%errorlevel%"

if not "%launcher_exit_code%"=="0" (
    echo.
    echo LiveCaption Studio failed to start. Review the error above.
    pause
)

exit /b %launcher_exit_code%
