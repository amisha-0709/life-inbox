@echo off
rem Double-click to run Life Inbox on this computer at http://127.0.0.1:8000
cd /d "%~dp0"
set LIFE_INBOX_ALLOW_DEV_LOGIN=false
.venv\Scripts\python.exe -m alembic upgrade head || goto :fail
start "" http://127.0.0.1:8000
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
goto :eof
:fail
echo.
echo Database setup failed. See the message above.
pause
