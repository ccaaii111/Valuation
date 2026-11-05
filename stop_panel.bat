@echo off
rem Stop the background panel that start_panel.bat launched: look up the process listening on the port
rem and end it, together with the reloader child a --debug run leaves behind.
rem Usage:  stop_panel.bat        -> port 8765, the default
rem         stop_panel.bat 9000   -> another port, the one start_panel.bat was given
rem The log the panel wrote stays in %TEMP%\valuation_panel.log - this file does not touch it.
rem ASCII-only on purpose: cmd reads .bat in the console codepage, UTF-8 Chinese turns into mojibake.
setlocal
set "PORT=%~1"
if "%PORT%"=="" set "PORT=8765"
set "FOUND=0"
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r /c:":%PORT%[ ]" ^| findstr /i /c:"LISTENING"') do (
  set "FOUND=1"
  for /f "tokens=1,2" %%n in ('tasklist /nh /fi "PID eq %%p"') do echo [stop_panel] pid %%p is %%n
  echo [stop_panel] ending pid %%p
  taskkill /pid %%p /t /f >nul 2>&1
)
if "%FOUND%"=="0" echo [stop_panel] nothing is listening on port %PORT% - the panel is not running.
pause
endlocal
