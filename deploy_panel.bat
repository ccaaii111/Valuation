@echo off
rem One-shot setup for a machine that has nothing installed yet:
rem   creates .venv in this folder, installs the packages pinned in requirements.txt, starts the panel.
rem The venv is created once; rerunning this file only re-checks the packages and starts the panel.
rem Usage:  deploy_panel.bat                       -> http://127.0.0.1:8765
rem         deploy_panel.bat --port 9000 --debug   other port, reload on code change
rem For a machine where the dependencies are already installed, start_panel.bat is the lighter entry.
rem ASCII-only on purpose: cmd reads .bat in the console codepage, UTF-8 Chinese turns into mojibake.
setlocal
cd /d "%~dp0"

set "BASEPY="
python -V >nul 2>nul && set "BASEPY=python"
if not defined BASEPY (
  py -3 -V >nul 2>nul && set "BASEPY=py -3"
)
if not defined BASEPY (
  echo [deploy] No usable Python interpreter found on PATH.
  echo [deploy] Install Python 3.13, or activate a conda environment, then rerun this file.
  echo [deploy] If "python" resolves to the Microsoft Store shortcut, turn off the app execution
  echo [deploy] alias under Windows settings, or set PATH to a real interpreter.
  pause
  exit /b 1
)

echo [deploy] interpreter used to create the virtual environment:
%BASEPY% -V

if not exist ".venv\Scripts\python.exe" (
  echo [deploy] creating .venv in this folder
  %BASEPY% -m venv .venv
  if errorlevel 1 (
    echo [deploy] .venv could not be created, see the lines above.
    pause
    exit /b 1
  )
)
set "VPY=%cd%\.venv\Scripts\python.exe"

echo [deploy] installing the pinned dependencies into .venv - needs PyPI access, a few minutes on first run
"%VPY%" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
  echo [deploy] pip install failed. Fix the network and rerun this file, or start the panel with an
  echo [deploy] environment that already has the packages: start_panel.bat
  pause
  exit /b 1
)

"%VPY%" scripts\serve_panel.py %*
if errorlevel 1 (
  echo [deploy] panel did not stay up, see the lines above.
  pause
)
endlocal
