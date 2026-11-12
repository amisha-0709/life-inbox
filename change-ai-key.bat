@echo off
rem Forget the saved AI key; start-life-inbox.bat will ask for a new one.
cd /d "%~dp0"
if exist ".ai-key" del ".ai-key"
if exist ".openai-key" del ".openai-key"
echo Saved AI key removed.
echo Close the Life Inbox window if it is open, then double-click
echo start-life-inbox.bat - it will ask for a Claude or OpenAI key
echo (or press Enter to skip).
pause
