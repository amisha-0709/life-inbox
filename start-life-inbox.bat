@echo off
rem Double-click to run Life Inbox on this computer at http://127.0.0.1:8000
cd /d "%~dp0"
set LIFE_INBOX_ALLOW_DEV_LOGIN=false

rem OpenAI key: asked once, kept in .openai-key (ignored by git, never committed).
rem Run change-openai-key.bat to replace or remove it.
if not exist ".openai-key" (
    echo.
    echo Optional: paste your OpenAI API key to turn on AI organizing,
    echo written Ask answers and voice transcription.
    echo Press Enter without typing anything to skip.
    echo.
    set "NEWKEY="
    set /p "NEWKEY=OpenAI key: "
    call :savekey
)
set "OPENAI_API_KEY="
for /f "usebackq delims=" %%K in (".openai-key") do set "OPENAI_API_KEY=%%K"
if defined OPENAI_API_KEY (echo AI features: ON) else (echo AI features: OFF - run change-openai-key.bat to add a key)

.venv\Scripts\python.exe -m alembic upgrade head || goto :fail
start "" http://127.0.0.1:8000
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
goto :eof

:savekey
if defined NEWKEY (
    <nul set /p "=%NEWKEY%" > ".openai-key"
) else (
    type nul > ".openai-key"
)
goto :eof

:fail
echo.
echo Database setup failed. See the message above.
pause
