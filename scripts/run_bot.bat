@echo off
REM FlowTrader bot — invoked by Windows Task Scheduler.
REM Mode argument optional: defaults to "full" (trading session).
REM Logs go to journal\bot.log (also written by main.py logger).
cd /d "%~dp0\.."
set MODE=%1
if "%MODE%"=="" set MODE=full
"C:\Python313\python.exe" main.py %MODE% >> "scripts\run_bot.log" 2>&1
