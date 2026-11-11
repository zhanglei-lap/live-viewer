@echo off
cd /d "%~dp0"
if exist ".venv-p0\Scripts\python.exe" (
    ".venv-p0\Scripts\python.exe" main.py
    if errorlevel 1 pause
    exit /b
)
python -c "import PySide6, requests, qrcode" >nul 2>&1
if errorlevel 1 (
    echo Python with PySide6, requests and qrcode is required.
    echo Install requirements: python -m pip install -r requirements.txt
    pause
    exit /b 1
)
python main.py
if errorlevel 1 pause
