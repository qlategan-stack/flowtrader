@echo off
REM Bybit Collateral Status Check
REM Double-click this file to run the collateral check.
REM All output is appended to check_collateral.log (L-3 audit fix 2026-05-25).

cd /d "%USERPROFILE%\OneDrive\1.Projects\Flowtrader\trading-bot\trading-bot"

set LOGFILE=scripts\check_collateral.log

echo. >> "%LOGFILE%"
echo ================================================================ >> "%LOGFILE%"
echo BYBIT COLLATERAL CHECK — %DATE% %TIME% >> "%LOGFILE%"
echo ================================================================ >> "%LOGFILE%"

echo.
echo ================================================================
echo BYBIT COLLATERAL STATUS CHECK
echo ================================================================
echo.

python bybit_collateral_manager.py --check >> "%LOGFILE%" 2>&1
python bybit_collateral_manager.py --check

echo ================================================================ >> "%LOGFILE%"

echo.
echo ================================================================
echo Output also saved to: %LOGFILE%
pause
