@echo off
cd /d "%~dp0"
if exist ".venv-p0\Scripts\pythonw.exe" (
    start "" ".venv-p0\Scripts\pythonw.exe" qt_main.py
) else (
    python qt_main.py
)
