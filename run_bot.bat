@echo off
REM Polymarket Bot Launcher
REM Usage: run_bot.bat [scan|once|live|stats|report|cancel-all]

set PYTHONPATH=C:\Users\mrgao\WorkBuddy\2026-08-12-23-04-24\libs
set PYTHON=C:\Users\mrgao\.workbuddy\binaries\python\envs\default\Scripts\python.exe
set BOT_DIR=C:\Users\mrgao\WorkBuddy\2026-08-12-23-04-24

cd /d %BOT_DIR%

if "%1"=="scan" (
    echo === Scanning markets (dry run, no trades) ===
    %PYTHON% polymarket_bot.py --scan
) else if "%1"=="once" (
    echo === Running one cycle (dry run) ===
    %PYTHON% polymarket_bot.py --once
) else if "%1"=="live" (
    echo === Starting LIVE trading mode ===
    echo WARNING: This will place REAL trades with REAL money!
    pause
    %PYTHON% polymarket_bot.py --live
) else if "%1"=="live-once" (
    echo === Running one LIVE cycle ===
    %PYTHON% polymarket_bot.py --live --once
) else if "%1"=="stats" (
    %PYTHON% polymarket_bot.py --stats
) else if "%1"=="report" (
    %PYTHON% polymarket_bot.py --report
) else if "%1"=="cancel-all" (
    %PYTHON% polymarket_bot.py --cancel-all
) else (
    echo.
    echo Polymarket Bot Commands:
    echo.
    echo   run_bot.bat scan        - Scan markets, show opportunities (no trades)
    echo   run_bot.bat once        - Run one full cycle in dry-run mode
    echo   run_bot.bat live        - Start continuous LIVE trading
    echo   run_bot.bat live-once   - Run one LIVE cycle then stop
    echo   run_bot.bat stats       - Show position stats and P&L
    echo   run_bot.bat report      - Generate daily report
    echo   run_bot.bat cancel-all  - Cancel all open orders
    echo.
)
