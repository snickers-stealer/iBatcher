@echo off
setlocal
cd /d "%~dp0"
set "PYTHON=python"
if exist ".venv\Scripts\python.exe" set "PYTHON=.venv\Scripts\python.exe"
"%PYTHON%" build_release.py
if errorlevel 1 (
    echo Build failed. See the error above.
    pause
    exit /b 1
)
explorer.exe "%~dp0dist"
pause
