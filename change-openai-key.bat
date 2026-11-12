@echo off
rem Forget the saved OpenAI key; start-life-inbox.bat will ask for a new one.
cd /d "%~dp0"
if exist ".openai-key" del ".openai-key"
echo Saved OpenAI key removed.
echo Close the Life Inbox window if it is open, then double-click
echo start-life-inbox.bat - it will ask for a key (or press Enter to skip).
pause
