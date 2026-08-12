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
rem  The fetch step retries each failed repo 3 rounds (60s apart)
rem  inside Python. Repos that still fail are named in the card and
rem  their increments roll into the next day's report, so the push
rem  is not blocked by one unreachable repo. The outer retry below
rem  only fires when the whole run failed (no repo list, no network).
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

rem Repos in scope. Empty = all repos in the org that pass the
rem fork/archived/size filters (38 as of 2026-08-12, ~25 min).
rem To narrow it down, set e.g. REPOS=deepmd-kit,dpdata and add
rem --repos %REPOS% to the commands below.
rem
rem After ADDING a repo to an existing scope, run once with
rem --mark-notified first: a repo never seen before has its whole
rem year of history counted as "new", which would flood the report.
set REPOS=

rem Expand to nothing when REPOS is empty, so we do not pass a bare
rem --repos with no value (argparse would eat the next flag).
set REPO_ARG=
if not "%REPOS%"=="" set REPO_ARG=--repos %REPOS%

rem How many times to retry a failed fetch, and how long to wait.
rem Python already retries individual HTTP calls (backoff) and
rem individual failed repos (3 rounds, 60s apart). This outer loop
rem only covers whole-run failures: the repo list itself could not
rem be fetched, or the machine lost network entirely. Keep it small
rem - a full run takes ~24 minutes, so 2 tries is already ~50 min.
set MAX_TRIES=2
set RETRY_WAIT=300

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
"%PYTHON%" -m contributors --daily %REPO_ARG% --only-notify --notify >> "%LOG%" 2>&1
set CODE=!ERRORLEVEL!
echo [%date% %time%] done (push only), exit code !CODE! >> "%LOG%"
exit /b !CODE!

rem ------------------------------------------------------------
:do_dry
echo ============================================== >> "%LOG%"
echo [%date% %time%] start (dry run) >> "%LOG%"
"%PYTHON%" -m contributors --daily %REPO_ARG% --notify-dry-run >> "%LOG%" 2>&1
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
rem NOTE: no --strict-repos here. Individual repos that fail are
rem retried 3 rounds inside Python; whatever still fails is listed
rem in the card ("failed to fetch: X - its increments roll into
rem tomorrow's report") and genuinely does roll over, because the
rem increment is keyed on first_seen_run. Blocking the whole push
rem over one unreachable repo would lose the other 37 repos' news.
"%PYTHON%" -m contributors --daily %REPO_ARG% --no-notify >> "%LOG%" 2>&1
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
