@echo off
REM FlowTrader heartbeat watchdog — schedule this on its OWN Windows Task
REM Scheduler entry (every 30-60 min), SEPARATE from run_bot.bat. It is the
REM thing that notices when run_bot.bat itself has stopped (C-2 audit 2026-06-16:
REM the June 10-13 blackout went unnoticed for 4 days because nothing watched
REM the watcher). Alerts via Telegram; makes no API/exchange calls so it keeps
REM working even when the outage is an API/credit failure.
cd /d "%~dp0\.."
python scripts\heartbeat_watchdog.py >> "scripts\watchdog.log" 2>&1
