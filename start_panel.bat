@echo off
rem Start the assumption-correction panel (backend/web) hidden in the background, then let the page
rem open itself in the default browser. The window you double-clicked only relaunches this same file
rem inside a hidden cmd and closes right away, so no server console stays on screen.
rem Console output is appended to %TEMP%\valuation_panel.log. To stop the background instance, double-click
rem stop_panel.bat (it finds the process listening on the port). Without that, the panel keeps serving until
rem you end python or log off.
rem Usage:  start_panel.bat                      -> http://127.0.0.1:8765
rem         start_panel.bat --port 9000 --debug  other port, reload on code change
rem ASCII-only on purpose: cmd reads .bat in the console codepage, UTF-8 Chinese turns into mojibake.
rem This file does not install anything. On a fresh machine use deploy_panel.bat, which creates .venv
rem and installs requirements.txt before starting the panel - there the window stays visible on purpose,
rem because a failed install has to be readable.
setlocal
if not "%PANEL_HIDDEN%"=="1" (
  set "PANEL_HIDDEN=1"
  powershell -NoProfile -Command "Start-Process -FilePath $env:ComSpec -ArgumentList '/c \"%~f0\" %*' -WindowStyle Hidden"
  echo [start_panel] starting in the background, the browser opens as soon as the panel answers.
  echo [start_panel] log: %TEMP%\valuation_panel.log    stop it with stop_panel.bat
  exit /b 0
)

cd /d "%~dp0"
set "LOG=%TEMP%\valuation_panel.log"
>> "%LOG%" echo(
>> "%LOG%" echo [start_panel] ==== %DATE% %TIME% ====
python scripts\serve_panel.py --open-browser %* >> "%LOG%" 2>&1
rem 9009 is what cmd leaves behind when the interpreter itself was not found.
if errorlevel 9009 (
  >> "%LOG%" echo [start_panel] python not found. Activate the environment first, e.g. conda activate base,
  >> "%LOG%" echo [start_panel] then install the dependencies once: python -m pip install -r requirements.txt
) else if errorlevel 3 (
  rem Exit code 2 is the documented "a panel is already listening" answer - the page was opened anyway.
  >> "%LOG%" echo [start_panel] panel did not stay up, exit code %ERRORLEVEL%, see the lines above.
)
endlocal
