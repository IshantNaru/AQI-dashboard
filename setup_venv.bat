@echo off
rem One-time setup: creates a private Python environment (.venv) for the AQI Dashboard,
rem installs everything it needs there (not in your global Python), and points the
rem Windows startup entry at it. Safe to run again.
cd /d "%~dp0"
set PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe
if not exist ".venv\Scripts\python.exe" (
    echo Creating .venv ...
    "%PY%" -m venv .venv || goto :fail
)
set VPY=.venv\Scripts\python.exe
"%VPY%" -m pip install --no-cache-dir --upgrade pip || goto :fail
echo Installing app requirements ...
"%VPY%" -m pip install --no-cache-dir -r requirements.txt || goto :fail
echo Installing python-miio (for reading the Mi purifier) ...
rem --no-cache-dir: the shared pip cache can hold files written by an Administrator terminal
rem that a normal window may not read (Errno 13), so this setup never reads from it.
rem netifaces has no prebuilt wheel for Python 3.12 on Windows; netifaces-plus is a maintained
rem drop-in with wheels, so install it and python-miio without letting pip try to compile netifaces.
"%VPY%" -m pip install --no-cache-dir netifaces-plus || goto :fail
"%VPY%" -m pip install --no-cache-dir --no-deps python-miio==0.5.12 || goto :fail
"%VPY%" -m pip install --no-cache-dir appdirs attrs "construct>=2.10.56,<3" croniter "cryptography>=35" defusedxml micloud pytz "zeroconf<1" android_backup "PyYAML>=5,<7" "click>=8" "tqdm>=4,<5" || goto :fail
"%VPY%" -c "import netifaces, miio; print('python-miio', miio.__version__, 'OK')" || goto :fail
echo Pointing the startup entry at .venv ...
"%VPY%" setup_autostart.py || goto :fail
echo.
echo Done. Quit the running dashboard from the tray, then start it with "Start AQI Dashboard.bat".
pause
exit /b 0
:fail
echo.
echo Setup stopped with an error (see above). Nothing outside this folder was changed.
pause
exit /b 1
