@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
if errorlevel 1 goto failed
set "LOGFILE=%~dp0tracker-startup.log"
set "PYTHON=%~dp0.venv\Scripts\python.exe"
echo Tracker startup > "%LOGFILE%"
echo Directory: %CD% >> "%LOGFILE%"
if not exist "launcher.py" goto no_launcher
if exist "%PYTHON%" goto launch
where py >nul 2>&1
if not errorlevel 1 goto use_py
where python >nul 2>&1
if not errorlevel 1 goto use_python
echo Python 3.10+ is not installed or not in PATH. >> "%LOGFILE%"
goto failed
:use_py
py -3 -m venv .venv >> "%LOGFILE%" 2>&1
if errorlevel 1 goto failed
goto launch
:use_python
python -m venv .venv >> "%LOGFILE%" 2>&1
if errorlevel 1 goto failed
goto launch
:launch
echo Starting Tiles Survive Tracker...
echo First launch may install Python packages. Please wait.
echo Starting launcher >> "%LOGFILE%"
"%PYTHON%" -u launcher.py >> "%LOGFILE%" 2>&1
if errorlevel 1 goto failed
exit /b 0
:no_launcher
echo launcher.py not found. Extract the whole Tracker archive first. >> "%LOGFILE%"
:failed
echo.
echo Tracker could not start. Diagnostic log:
echo %LOGFILE%
echo.
type "%LOGFILE%"
echo.
pause
exit /b 1
