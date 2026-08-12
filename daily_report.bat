@echo off
rem ============================================================
rem  DeepModeling community daily report
rem
rem  Modes:
rem    daily_report.bat --fetch   fetch only, retries on failure
rem    daily_report.bat --push    push only (seconds)
rem    daily_report.bat --daily   fetch, then push ONLY if fetch ok
rem    daily_report.bat           fetch then push (same as --daily)
rem    daily_report.bat --dry     render only, send nothing
rem
rem  Why --daily chains them: with two independent scheduled tasks,
rem  a failed fetch still lets the push run, which then reports the
rem  previous run's data with no warning. Chaining makes the push
rem  conditional on a successful fetch.
rem
rem  The fetch step runs with --strict-repos, so a single repo
rem  failing (network hiccup, SSL error) makes the whole step fail
rem  and get retried, rather than silently reporting partial data.
rem
rem  If you prefer two separate scheduled tasks (exact push time),
rem  use --fetch and --push, and check the log for the fetch exit
rem  code. See STATUS file written below.
rem
rem  Feishu docs advise avoiding exact hour / half-hour marks
rem  (rate limit 100/min, 5/sec). Prefer 10:33 / 11:03.
rem
rem  NOTE: comments here are ASCII on purpose. cmd.exe re-reads the
rem  batch file per line; non-ASCII text under a different active
rem  code page gets parsed as commands and the script dies.
rem ============================================================

setlocal enabledelayedexpansion

cd /d "%~dp0"

rem Absolute path, not "py -3.12": a scheduled task has a different
rem PATH than an interactive shell and may not find the launcher.
set PYTHON=C:\Users\a\AppData\Local\Programs\Python\Python312\python.exe

rem Python prints Chinese; without this the log is mojibake.
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

rem Repos in scope. After adding a repo here, run once with
rem --mark-notified first, otherwise that repo's whole year of
rem history is reported as one day's activity.
set REPOS=deepmd-kit,dpdata

rem How many times to retry a failed fetch, and how long to wait.
rem The Python layer already retries individual HTTP calls with
rem backoff; this outer loop covers whole-run failures (git fetch
rem died, machine lost network for a while).
set MAX_TRIES=3
set RETRY_WAIT=120

if not exist "logs" mkdir "logs"
for /f "tokens=1-3 delims=/- " %%a in ("%date%") do set TODAY=%%a-%%b-%%c
set LOG=logs\daily-%TODAY%.log
set STATUS=logs\last-fetch-status.txt

set ACTION=%~1
if "%ACTION%"=="" set ACTION=--daily

if /i "%ACTION%"=="--fetch" goto do_fetch
if /i "%ACTION%"=="--push"  goto do_push
if /i "%ACTION%"=="--dry"   goto do_dry
if /i "%ACTION%"=="--daily" goto do_chain
echo Unknown option: %ACTION%
echo Use --fetch, --push, --daily, or --dry
exit /b 2

rem ------------------------------------------------------------
:do_fetch
call :fetch_with_retry
exit /b %FETCH_CODE%

rem ------------------------------------------------------------
:do_push
echo ============================================== >> "%LOG%"
echo [%date% %time%] start (push only) >> "%LOG%"
"%PYTHON%" -m contributors --daily --repos %REPOS% --only-notify --notify >> "%LOG%" 2>&1
set CODE=!ERRORLEVEL!
echo [%date% %time%] done (push only), exit code !CODE! >> "%LOG%"
exit /b !CODE!

rem ------------------------------------------------------------
:do_dry
echo ============================================== >> "%LOG%"
echo [%date% %time%] start (dry run) >> "%LOG%"
"%PYTHON%" -m contributors --daily --repos %REPOS% --notify-dry-run >> "%LOG%" 2>&1
set CODE=!ERRORLEVEL!
echo [%date% %time%] done (dry run), exit code !CODE! >> "%LOG%"
exit /b !CODE!

rem ------------------------------------------------------------
rem Fetch with retry, then push only if the fetch succeeded.
:do_chain
call :fetch_with_retry
if !FETCH_CODE! neq 0 (
    echo [%date% %time%] fetch failed after %MAX_TRIES% tries, SKIPPING push >> "%LOG%"
    echo [%date% %time%] no report was sent - fix the error above and rerun >> "%LOG%"
    exit /b !FETCH_CODE!
)
goto do_push

rem ------------------------------------------------------------
rem Subroutine: run the fetch step, retrying transient failures.
:fetch_with_retry
set FETCH_CODE=1
set TRY=1
:retry_loop
echo ============================================== >> "%LOG%"
echo [%date% %time%] start (fetch, attempt !TRY! of %MAX_TRIES%) >> "%LOG%"
"%PYTHON%" -m contributors --daily --repos %REPOS% --no-notify --strict-repos >> "%LOG%" 2>&1
set FETCH_CODE=!ERRORLEVEL!
echo [%date% %time%] done (fetch attempt !TRY!), exit code !FETCH_CODE! >> "%LOG%"

if !FETCH_CODE! equ 0 (
    echo ok %date% %time% > "%STATUS%"
    exit /b 0
)

set /a TRY+=1
if !TRY! leq %MAX_TRIES% (
    echo [%date% %time%] fetch failed, waiting %RETRY_WAIT%s before retry >> "%LOG%"
    rem timeout needs a console; ping is the portable sleep for tasks
    ping -n %RETRY_WAIT% 127.0.0.1 >nul 2>&1
    goto retry_loop
)

echo failed %date% %time% exit=!FETCH_CODE! > "%STATUS%"
exit /b !FETCH_CODE!
