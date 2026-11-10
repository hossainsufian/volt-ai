Set sh = CreateObject("WScript.Shell")
sh.Run """" & sh.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\VoltAI\run.cmd""", 0, False
