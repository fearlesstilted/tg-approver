@echo off
chcp 65001 >nul
set PYTHONUTF8=1
echo Starting Claude Telegram Approver...
cd /d D:\vsc\f\tg_approver
python server.py
pause
