@echo off
rem ====================================================================
rem  A-Stock Picker :: DAILY UPDATE
rem
rem  Stop server -> fetch incremental bars -> rebuild panels -> start
rem  Takes ~18 min. The server WILL be unavailable during the run.
rem
rem    update.cmd                      full (rebuild panels)
rem    update.cmd --no-rebuild         raw data only (~10 min)
rem    update.cmd --industry           also refresh industry mapping
rem ====================================================================
setlocal
cd /d "%~dp0"

rem --- UTF-8 console (Chinese Windows defaults to GBK; see start.cmd) ---
chcp 65001 >nul 2>&1
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8:replace"

if not defined ASTOCK_PY if exist "%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe" set "ASTOCK_PY=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not defined ASTOCK_PY if exist "%~dp0.venv\Scripts\python.exe" set "ASTOCK_PY=%~dp0.venv\Scripts\python.exe"
if not defined ASTOCK_PY set "ASTOCK_PY=python"

"%ASTOCK_PY%" "%~dp0scripts\ctl.py" update %*
if errorlevel 1 (
    echo.
    echo [FAILED] See messages above. Raw data is append-only, safe to retry.
    pause
)
endlocal
