@echo off
cd /d "%~dp0"
python "%~dp0tools\build-p9-single.py"
if errorlevel 1 pause
