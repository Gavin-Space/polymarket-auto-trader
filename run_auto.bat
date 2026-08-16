@echo off
chcp 65001 >nul 2>&1
title PolyAuto - 全自动交易系统
cd /d "%~dp0"
set PYTHONPATH=%~dp0libs
"C:\Users\mrgao\.workbuddy\binaries\python\envs\default\Scripts\python.exe" auto_trader.py
pause
