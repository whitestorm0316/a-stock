@echo off
rem ====================================================================
rem  A-Stock Picker :: DAILY UPDATE + FEISHU ALERT
rem
rem  One command does three things:
rem    1. update today's data  (stop server -> fetch -> rebuild -> start)
rem    2. make sure the server is actually up (fallback start)
rem    3. if "bear market AND has signals": send a Feishu alert
rem
rem  Examples:
rem    daily_alert.cmd                      full run (~18 min)
rem    daily_alert.cmd --dry-run            print the message, do NOT send
rem    daily_alert.cmd --skip-update        skip update, check only (seconds)
rem    daily_alert.cmd --check              verify webhook / server config
rem
rem  To let OTHER people receive the alert, use a Feishu group custom-bot
rem  webhook (no node / lark-cli needed). Put the URL in ONE of:
rem    - env var  A_STOCK_ALERT_WEBHOOK
rem    - file     data\feishu_webhook.txt
rem    - file     %USERPROFILE%\.a-stock\feishu_webhook.txt
rem  See README section "每日提醒" for how to create the bot.
rem
rem  Tip: set env ASTOCK_PY to pin a specific python.exe
rem ====================================================================
setlocal
cd /d "%~dp0"

rem --- switch console to UTF-8 so Chinese messages are not mojibake ---
rem (Chinese Windows defaults to codepage 936/GBK; Python then refuses to
rem  print emoji and renders Chinese as garbage. Harmless if already 65001.)
chcp 65001 >nul 2>&1
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8:replace"

rem --- resolve python (pinned env first, then known venv, then PATH) ---
if not defined ASTOCK_PY if exist "%USERPROFILE%\.workbuddy-ai\binaries\python\envs\default\Scripts\python.exe" set "ASTOCK_PY=%USERPROFILE%\.workbuddy-ai\binaries\python\envs\default\Scripts\python.exe"
if not defined ASTOCK_PY if exist "%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe" set "ASTOCK_PY=%USERPROFILE%\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
if not defined ASTOCK_PY if exist "%~dp0.venv\Scripts\python.exe" set "ASTOCK_PY=%~dp0.venv\Scripts\python.exe"
if not defined ASTOCK_PY set "ASTOCK_PY=python"

"%ASTOCK_PY%" "%~dp0scripts\daily_alert.py" %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
    echo.
    echo [FAILED] exit=%RC%  See messages above.
    pause
)
endlocal & exit /b %RC%
