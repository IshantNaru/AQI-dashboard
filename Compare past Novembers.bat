@echo off
rem Fetches your area's PM2.5 for 15 Oct - 30 Nov of this year and the two before (OpenAQ archive).
cd /d "%~dp0"
set PY=%~dp0.venv\Scripts\python.exe
if not exist "%PY%" set PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe
"%PY%" "%~dp0history_compare.py"
