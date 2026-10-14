@echo off
rem companion bot restart (called from !restart / self_restart)
rem wait for qdrant etc. locks to release before starting bot.py
rem stdout/stderr go to FILES, not the console: a paused console
rem window blocks writes forever and freezes the whole process via
rem the logging lock (the real log is data/bot.log anyway).
rem NB: exe path needs backslashes; redirect targets use slash to
rem keep the file free of backslash-b sequences editors mangle.
cd /d "%~dp0"
timeout /t 5 /nobreak >nul
start "companion-bot" /min cmd /c ".venv\Scripts\python.exe -u bot.py >> data/bot_stdout.log 2>> data/bot_stderr.log"
