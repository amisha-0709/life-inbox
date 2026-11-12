@echo off
rem Double-click to run Life Inbox on this computer at http://127.0.0.1:8000
cd /d "%~dp0"
set LIFE_INBOX_ALLOW_DEV_LOGIN=false

rem AI key: asked once, kept in .ai-key (ignored by git, never committed).
rem Claude keys (sk-ant-...) and OpenAI keys both work.
rem Run change-ai-key.bat to replace or remove it.
if exist ".openai-key" if not exist ".ai-key" ren ".openai-key" ".ai-key"
if not exist ".ai-key" (
    echo.
    echo Optional: paste your Claude or OpenAI API key to turn on
    echo AI organizing and written Ask answers.
    echo Press Enter without typing anything to skip.
    echo.
    set "NEWKEY="
    set /p "NEWKEY=API key: "
    call :savekey
)
set "AIKEY="
for /f "usebackq delims=" %%K in (".ai-key") do set "AIKEY=%%K"
set "ANTHROPIC_API_KEY="
set "OPENAI_API_KEY="
if not defined AIKEY (
    echo AI features: OFF - run change-ai-key.bat to add a key
) else if "%AIKEY:~0,7%"=="sk-ant-" (
    set "ANTHROPIC_API_KEY=%AIKEY%"
    echo AI features: ON - Claude
) else (
    set "OPENAI_API_KEY=%AIKEY%"
    echo AI features: ON - OpenAI
)
set "AIKEY="

.venv\Scripts\python.exe -m pip install -q -r requirements.txt || goto :fail
.venv\Scripts\python.exe -m alembic upgrade head || goto :fail
start "" http://127.0.0.1:8000
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
goto :eof

:savekey
if defined NEWKEY (
    <nul set /p "=%NEWKEY%" > ".ai-key"
) else (
    type nul > ".ai-key"
)
goto :eof

:fail
echo.
echo Setup failed. See the message above.
pause
