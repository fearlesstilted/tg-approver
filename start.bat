@echo off
chcp 65001 >nul
set PYTHONUTF8=1
echo Starting Claude Telegram Approver...
cd /d %~dp0
python server.py
pause
