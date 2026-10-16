# 投資信託ランキング（8050）を、ログオン時に自動で起こす予約タスクに入れて、いま起こす。
#   powershell -ExecutionPolicy Bypass -File "登録する.ps1"
# 何度走らせても増えない（同じ名前を置きなおすだけ）。同じPCの、ほかのアプリの予約タスクには触らない。
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$taskName = "Toushin-Ranking"
$vbs = Join-Path $here "_launch_hidden_toushin.vbs"
New-Item -ItemType Directory -Force (Join-Path $here "data") | Out-Null
$action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "`"$vbs`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
Write-Host "登録して起こしました: $taskName"
