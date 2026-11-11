' Starts Volt AI with no console window. Options (such as --open-chat) are passed on.
Set sh = CreateObject("WScript.Shell")
args = ""
For Each a In WScript.Arguments
    args = args & " " & a
Next
sh.Run """" & sh.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\VoltAI\run.cmd""" & args, 0, False
