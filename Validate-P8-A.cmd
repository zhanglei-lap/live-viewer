@echo off
cd /d "%~dp0"
if not exist ".venv-p0\Scripts\python.exe" (
  echo Missing project environment. Install requirements.txt in .venv-p0 first.
  pause
  exit /b 1
)
".venv-p0\Scripts\python.exe" "tools\validate-p8a.py" %*
if errorlevel 1 pause
