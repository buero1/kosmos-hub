@echo off
setlocal
cd /d "%~dp0"

where npm.cmd >nul 2>nul
if errorlevel 1 (
  echo Node.js und npm wurden nicht gefunden.
  echo Bitte Node.js installieren und diesen Starter danach erneut ausfuehren.
  pause
  exit /b 1
)

if not exist "node_modules\electron\package.json" (
  echo Der Notifier wird einmalig eingerichtet ...
  call npm.cmd install
  if errorlevel 1 (
    echo.
    echo Die Einrichtung ist fehlgeschlagen.
    pause
    exit /b 1
  )
)

echo Kosmos Notifier wird im Entwicklungsmodus gestartet ...
call npm.cmd start
