@echo off
rem Starts Volt AI. Output goes to volt-ai.log in this folder.
cd /d "%LOCALAPPDATA%\VoltAI\app\src"
set PYTHONPATH=%LOCALAPPDATA%\VoltAI\app\src
set PYTHONIOENCODING=utf-8
set "LOG=%LOCALAPPDATA%\VoltAI\volt-ai.log"
rem A running Volt keeps its log open and Windows then refuses a second
rem writer, so a new launch (which replaces the running one) uses the other log.
(>>"%LOG%" type nul) 2>nul || set "LOG=%LOCALAPPDATA%\VoltAI\volt-ai-2.log"
"%LOCALAPPDATA%\VoltAI\venv\Scripts\python.exe" -m desktop_app %* >> "%LOG%" 2>&1
