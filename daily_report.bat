@echo off
rem ============================================================
rem  DeepModeling community daily report
rem
rem  Two ways to run:
rem
rem   1) Split (current setup, matches "fetch 10:30, push 11:00"):
rem        daily_report.bat --fetch    at 10:30 - fetch, no push
rem        daily_report.bat --push     at 11:00 - push only, seconds
rem      The push step does NOT re-fetch, so it finishes in seconds
rem      and can be scheduled at an exact time. Fetch time varies a
rem      lot (a single repo has taken over 600s), which is why the
rem      two steps are separate.
rem
rem   2) Single chain (simpler, push time floats):
rem        daily_report.bat            fetch then push immediately
rem        daily_report.bat --dry      render only, send nothing
rem
rem  Feishu docs advise avoiding exact hour / half-hour marks
rem  (rate limit 100/min, 5/sec). 10:33 / 11:03 are safer than
rem  10:30 / 11:00.
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

rem Pick the mode from the first argument.
set MODE=--notify
set LABEL=fetch+push
if /i "%~1"=="--fetch" set MODE=--no-notify& set LABEL=fetch only
if /i "%~1"=="--push"  set MODE=--only-notify --notify& set LABEL=push only
if /i "%~1"=="--dry"   set MODE=--notify-dry-run& set LABEL=dry run

echo ============================================== >> "%LOG%"
echo [%date% %time%] start (%LABEL%) >> "%LOG%"

"%PYTHON%" -m contributors --daily --repos %REPOS% %MODE% >> "%LOG%" 2>&1
set CODE=%ERRORLEVEL%

echo [%date% %time%] done (%LABEL%), exit code %CODE% >> "%LOG%"

rem Exit code reflects the statistics run only. A failed push does
rem not change it: the push self-heals (next run sends both days'
rem increments) and a silent group is noticed anyway.
endlocal & exit /b %CODE%
