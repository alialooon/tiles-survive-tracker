@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
if errorlevel 1 goto failed
echo Starting Tiles Survive Tracker...
where powershell.exe >nul 2>&1
if errorlevel 1 goto failed
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; $url='https://raw.githubusercontent.com/alialooon/tiles-survive-tracker/main/tracker-bootstrap.ps1'; $new=Join-Path (Get-Location).Path 'tracker-bootstrap.ps1.new'; Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $new -TimeoutSec 15; Move-Item -Force -LiteralPath $new -Destination 'tracker-bootstrap.ps1'" >nul 2>&1
if errorlevel 1 echo GitHub is unavailable. Trying cached bootstrap...
if not exist "tracker-bootstrap.ps1" goto failed
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tracker-bootstrap.ps1"
if errorlevel 1 goto failed
exit /b 0
:failed
echo.
echo Tracker could not start. See tracker-startup.log in this directory.
echo Make sure the COMPLETE original Tracker archive is extracted.
echo.
pause
exit /b 1
