@echo off
rem ====================================================================
rem  A-Stock Picker :: STOP       stop running server (default 8770)
rem    stop.cmd --port 9000
rem ====================================================================
setlocal
cd /d "%~dp0"

if not defined ASTOCK_PY if exist "%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe" set "ASTOCK_PY=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not defined ASTOCK_PY if exist "%~dp0.venv\Scripts\python.exe" set "ASTOCK_PY=%~dp0.venv\Scripts\python.exe"
if not defined ASTOCK_PY set "ASTOCK_PY=python"

"%ASTOCK_PY%" "%~dp0scripts\ctl.py" stop %*
if errorlevel 1 pause
endlocal
