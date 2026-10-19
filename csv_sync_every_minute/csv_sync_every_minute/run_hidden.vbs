' Run sync_csv.ps1 invisibly in the signed-in user's session.
Option Explicit
Dim shell, scriptPath, command
Set shell = CreateObject("WScript.Shell")
scriptPath = shell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\CSVDateSync\sync_csv.ps1"
command = "powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File " & Chr(34) & scriptPath & Chr(34)
shell.Run command, 0, True
