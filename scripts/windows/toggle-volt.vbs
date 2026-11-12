' Runs toggle-volt.ps1 without a console window.
Set sh = CreateObject("WScript.Shell")
ps = sh.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\VoltAI\toggle-volt.ps1"
sh.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & ps & """", 0, False
