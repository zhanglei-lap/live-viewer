@echo off
cd /d "%~dp0"
python "%~dp0tools\build-p9.py"
if errorlevel 1 pause
