@echo off
rem Starts the AQI Dashboard detached from any console window, so closing a terminal never stops it.
rem Uses the private .venv if it exists, otherwise your Python 3.12. If it's already running,
rem this just brings the existing window forward.
set PYW=%~dp0.venv\Scripts\pythonw.exe
if not exist "%PYW%" set PYW=%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe
start "" "%PYW%" "%~dp0dashboard.py"
