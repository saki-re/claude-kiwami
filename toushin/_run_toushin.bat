@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
echo ===== launch %date% %time% ===== >> "%~dp0data\_toushin_console.log"
"%USERPROFILE%\AppData\Local\Programs\Python\Python314\python.exe" app.py >> "%~dp0data\_toushin_console.log" 2>&1
