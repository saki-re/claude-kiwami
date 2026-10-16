' Toushin ranking (8050) hidden launcher with auto-restart.
Dim sh, env, dir, cmd
Set sh = CreateObject("WScript.Shell")
Set env = sh.Environment("PROCESS")
dir = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = dir
cmd = """" & dir & "\_run_toushin.bat"""
Do
  sh.Run cmd, 0, True
  WScript.Sleep 5000
Loop
