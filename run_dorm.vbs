' Silent launcher for dorm_electricity_monitor (no console window).
' Double-click to start the background service; or place a shortcut in the Startup folder.
' NOTE: the most reliable launcher is the .lnk shortcut that points directly at
'       .venv\Scripts\pythonw.exe (no VBScript / COM dependency at all).
Set ws = CreateObject("WScript.Shell")
scriptDir = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\"))
ws.CurrentDirectory = scriptDir
py = Chr(34) & scriptDir & ".venv\Scripts\pythonw.exe" & Chr(34)
script = Chr(34) & scriptDir & "dorm_elec_auto.py" & Chr(34)
ws.Run py & " " & script, 0, False
