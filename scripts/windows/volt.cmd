@echo off
rem Volt Code: coding assistant in the terminal (part of Volt AI).
set "PYTHONPATH=%LOCALAPPDATA%\VoltAI\app\src"
set PYTHONIOENCODING=utf-8
"%LOCALAPPDATA%\VoltAI\venv\Scripts\python.exe" -m volt_code %*
