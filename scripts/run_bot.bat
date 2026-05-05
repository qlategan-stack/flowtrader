@echo off
REM FlowTrader bot — invoked by Windows Task Scheduler.
REM Mode argument optional: defaults to "full" (trading session).
REM Logs go to journal\bot.log (also written by main.py logger).
cd /d "%~dp0\.."
set MODE=%1
if "%MODE%"=="" set MODE=full

REM Pull latest risk profile from dashboard repo before each run
git -C "C:\Users\quint\OneDrive\1.Projects\Flowtrader\flowtrader-dashboard" pull --ff-only >> "scripts\run_bot.log" 2>&1

"C:\Python313\python.exe" main.py %MODE% >> "scripts\run_bot.log" 2>&1
"C:\Python313\python.exe" scripts\push_journal.py >> "scripts\run_bot.log" 2>&1
