@echo off
rem Starts Volt AI. Output goes to volt-ai.log in this folder.
cd /d "%LOCALAPPDATA%\VoltAI\app\src"
set PYTHONPATH=%LOCALAPPDATA%\VoltAI\app\src
set PYTHONIOENCODING=utf-8
"%LOCALAPPDATA%\VoltAI\venv\Scripts\python.exe" -m desktop_app >> "%LOCALAPPDATA%\VoltAI\volt-ai.log" 2>&1
