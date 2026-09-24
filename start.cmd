@echo off
rem ====================================================================
rem  A-Stock Picker :: START      (double-click, or run from terminal)
rem
rem  Examples:
rem    start.cmd                 start on port 8770, open browser
rem    start.cmd --port 9000     custom port
rem    start.cmd --no-browser    do not open browser
rem
rem  Tip: set env ASTOCK_PY to pin a specific python.exe
rem ====================================================================
setlocal
cd /d "%~dp0"

rem --- resolve python (pinned env first, then known venv, then PATH) ---
if not defined ASTOCK_PY if exist "%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe" set "ASTOCK_PY=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not defined ASTOCK_PY if exist "%~dp0.venv\Scripts\python.exe" set "ASTOCK_PY=%~dp0.venv\Scripts\python.exe"
if not defined ASTOCK_PY set "ASTOCK_PY=python"

"%ASTOCK_PY%" "%~dp0scripts\ctl.py" start %*
if errorlevel 1 (
    echo.
    echo [FAILED] See messages above.
    pause
)
endlocal
