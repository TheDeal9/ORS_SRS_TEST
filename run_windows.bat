@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Creating Python virtual environment...
  python -m venv .venv
  if errorlevel 1 goto error
)
call ".venv\Scripts\activate.bat"
python -m pip install -r requirements-dev.txt
if errorlevel 1 goto error
if not exist ".env" (
  echo First run: choose the administrator ID and password. Only a hash is saved, in .env next to this file.
  python app.py setup
  if errorlevel 1 goto error
)
echo Starting ORS/SRS app at http://127.0.0.1:5000/admin/login. Keep this window open.
python app.py
pause
exit /b 0
:error
echo Setup failed. Check the error above.
pause
exit /b 1
