@echo off
REM FlowTrader bot — invoked by Windows Task Scheduler.
REM Mode argument optional: defaults to "full" (trading session).
REM Logs go to journal\bot.log (also written by main.py logger).
cd /d "%~dp0\.."
set MODE=%1
if "%MODE%"=="" set MODE=full

REM Pull latest risk profile from dashboard repo before each run
git -C "%USERPROFILE%\OneDrive\1.Projects\Flowtrader\flowtrader-dashboard" pull --ff-only >> "scripts\run_bot.log" 2>&1

REM Keep bot-local risk_profile.json in sync with dashboard (H-3 audit fix 2026-05-25)
REM Path 1 (journal\risk_profile.json) is now always present so the executor
REM never falls back to high_safety defaults if the dashboard repo is unavailable.
copy /Y "%USERPROFILE%\OneDrive\1.Projects\Flowtrader\flowtrader-dashboard\journal\risk_profile.json" "journal\risk_profile.json" >> "scripts\run_bot.log" 2>&1

python main.py %MODE% >> "scripts\run_bot.log" 2>&1
python scripts\push_journal.py >> "scripts\run_bot.log" 2>&1
python "%USERPROFILE%\OneDrive\1.Projects\Flowtrader\flowtrader-dashboard\scripts\build_positions_dashboard.py" >> "scripts\run_bot.log" 2>&1
