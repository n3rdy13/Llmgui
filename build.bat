@echo off
REM Build GenStudio.exe locally (output: dist\GenStudio.exe)
cd /d "%~dp0"
if not exist .venv python -m venv .venv
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -r requirements.txt pyinstaller
.venv\Scripts\python -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name GenStudio ^
  --icon genstudio\icon.ico ^
  --add-data "genstudio\models.json;genstudio" ^
  --add-data "genstudio\icon.ico;genstudio" ^
  --collect-all customtkinter ^
  --hidden-import keyring.backends.Windows ^
  main.py
echo.
echo Done. Your app is in dist\GenStudio.exe
pause
