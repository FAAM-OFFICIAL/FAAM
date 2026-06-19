@echo off
REM ============================================================
REM  Build FAAM.exe — a self-contained Windows app with its own
REM  window (Edge WebView2). Run this ON A WINDOWS PC.
REM  Requires: Python 3.9+ on PATH. Output: dist\FAAM.exe
REM ============================================================
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo Python 3.9+ is required. Install from https://www.python.org/downloads/
  echo ^(tick "Add Python to PATH"^), then run this again.
  pause
  exit /b 1
)

echo Installing build tools (pywebview, pyinstaller)...
python -m pip install --upgrade pip >nul
python -m pip install pywebview pyinstaller
if errorlevel 1 (
  echo Failed to install build dependencies.
  pause
  exit /b 1
)

echo Building FAAM.exe ...
python -m PyInstaller --noconfirm --onefile --noconsole --name FAAM ^
  --icon FAAM.ico ^
  --add-data "static;static" ^
  --add-data "advisers;advisers" ^
  --add-data "app.py;." ^
  --hidden-import app ^
  winshell.py
if errorlevel 1 (
  echo Build failed.
  pause
  exit /b 1
)

echo.
echo ============================================================
echo  Done!  Your app is here:   dist\FAAM.exe
echo  Double-click it to run FAAM in its own window.
echo ============================================================
pause
