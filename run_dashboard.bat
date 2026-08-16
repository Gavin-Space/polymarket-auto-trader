@echo off
chcp 65001 >nul
echo ============================================
echo   PolyMonitor - 预测市场监控仪表盘
echo   启动中...
echo ============================================
echo.
set PYTHONPATH=C:\Users\mrgao\WorkBuddy\2026-08-12-23-04-24\libs
"C:\Users\mrgao\.workbuddy\binaries\python\envs\default\Scripts\python.exe" "C:\Users\mrgao\WorkBuddy\2026-08-12-23-04-24\web_dashboard.py"
pause
