@echo off
cd /d "%~dp0"
if not exist "data\logs" mkdir "data\logs"
echo. >> "data\logs\run.log"
echo [%date% %time%] start >> "data\logs\run.log"
".venv\Scripts\python.exe" run_daily.py >> "data\logs\run.log" 2>&1
echo [%date% %time%] exit=%ERRORLEVEL% >> "data\logs\run.log"
