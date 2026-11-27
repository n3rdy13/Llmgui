@echo off
REM Run GenStudio from source (needs Python 3.10+ from python.org, with "Add to PATH" ticked)
cd /d "%~dp0"
if not exist .venv (
    echo Creating virtual environment...
    python -m venv .venv || goto :nopython
    .venv\Scripts\python -m pip install --upgrade pip
    .venv\Scripts\python -m pip install -r requirements.txt
)
start "" .venv\Scripts\pythonw.exe main.py
goto :eof

:nopython
echo Python was not found. Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
pause
