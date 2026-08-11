@echo off
rem ============================================================
rem  DeepModeling community daily report - fetch and push
rem
rem  Runs the pipeline then pushes immediately. Do NOT split this
rem  into "fetch at 9:50, push at 10:00": fetch time depends on
rem  how many new commits landed and on network speed (measured:
rem  a single repo can exceed 10 minutes). A fixed push time would
rem  send half-finished data on slow days.
rem
rem  Suggested trigger time: 9:30. Feishu docs advise avoiding
rem  exact hour / half-hour marks (rate limit 100/min, 5/sec).
rem
rem  Usage:
rem    daily_report.bat          run and push
rem    daily_report.bat --dry    render only, send nothing
rem
rem  NOTE: comments here are ASCII on purpose. cmd.exe re-reads the
rem  batch file per line; non-ASCII text under a different active
rem  code page gets parsed as commands and the script dies.
rem ============================================================

setlocal

rem Scheduled tasks do not start in the script directory.
cd /d "%~dp0"

rem Absolute path, not "py -3.12": a scheduled task has a different
rem PATH than an interactive shell and may not find the launcher.
set PYTHON=C:\Users\a\AppData\Local\Programs\Python\Python312\python.exe

rem Python prints Chinese; without this the log is mojibake and
rem useless for troubleshooting. Set on the child process only, so
rem the batch file itself is still parsed under the original code page.
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

rem Repos in scope. After adding a repo here, run once with
rem --mark-notified first, otherwise that repo's whole year of
rem history is reported as one day's activity.
set REPOS=deepmd-kit,dpdata

if not exist "logs" mkdir "logs"
for /f "tokens=1-3 delims=/- " %%a in ("%date%") do set TODAY=%%a-%%b-%%c
set LOG=logs\daily-%TODAY%.log

set NOTIFY=--notify
if /i "%~1"=="--dry" set NOTIFY=--notify-dry-run

echo ============================================== >> "%LOG%"
echo [%date% %time%] start >> "%LOG%"

"%PYTHON%" -m contributors --daily --repos %REPOS% %NOTIFY% >> "%LOG%" 2>&1
set CODE=%ERRORLEVEL%

echo [%date% %time%] done, exit code %CODE% >> "%LOG%"

rem Exit code reflects the statistics run only. A failed push does
rem not change it: the push self-heals (next run sends both days'
rem increments) and a silent group is noticed anyway.
endlocal & exit /b %CODE%
