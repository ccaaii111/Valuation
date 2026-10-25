@echo off
rem Start the assumption-correction panel (backend/web). This window stays as the server console.
rem Usage:  start_panel.bat                      -> http://127.0.0.1:8765
rem         start_panel.bat --port 9000 --debug  other port, reload on code change
rem ASCII-only on purpose: cmd reads .bat in the console codepage, UTF-8 Chinese turns into mojibake.
rem This file does not install anything. On a fresh machine use deploy_panel.bat, which creates .venv
rem and installs requirements.txt before starting the panel.
setlocal
cd /d "%~dp0"
python scripts\serve_panel.py %*
rem 9009 is what cmd leaves behind when the interpreter itself was not found.
if errorlevel 9009 (
  echo [start_panel] python not found. Activate the environment first, e.g.  conda activate base
  echo [start_panel] then install the dependencies once:  python -m pip install -r requirements.txt
) else if errorlevel 1 (
  echo [start_panel] panel did not stay up, see the lines above.
)
if errorlevel 1 pause
endlocal
