' Claude Kiwami (8030) hidden launcher with auto-restart.
' Runs _run_claude.bat (real python.exe, output -> _claude_console.log) with no visible window.
' If it exits/crashes, wait 5s and relaunch. Loops forever.
Dim sh, env, profile, dir, cmd
Set sh = CreateObject("WScript.Shell")
Set env = sh.Environment("PROCESS")
profile = env("USERPROFILE")
dir = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = dir
cmd = """" & dir & "\_run_claude.bat"""
Do
  ' 0 = hidden window, True = wait until it exits, then relaunch
  sh.Run cmd, 0, True
  WScript.Sleep 5000
Loop
