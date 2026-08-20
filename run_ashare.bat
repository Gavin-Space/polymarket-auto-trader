@echo off
REM ============================================================
REM  AShareAuto (A股自动交易系统) 一键启动
REM  主界面:  http://localhost:5000
REM  子界面:  Polymarket 机器人 http://localhost:5001 (auto_trader.py)
REM ============================================================
cd /d "%~dp0"

echo [1/2] 检查依赖 (flask / akshare) ...
python -c "import flask, akshare, pandas" 2>nul
if errorlevel 1 (
    echo   -> 安装依赖 ...
    python -m pip install -r requirements-ashare.txt
)

echo [2/2] 启动 AShareAuto 主界面 (port 5000) ...
echo   浏览器打开 http://localhost:5000
echo   关闭本窗口即停止交易引擎
echo.
python ashare_trader.py
pause
