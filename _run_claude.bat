@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
echo ===== launch %date% %time% ===== >> "%~dp0_claude_console.log"
python app.py >> "%~dp0_claude_console.log" 2>&1
