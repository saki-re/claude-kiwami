# 投資信託ランキングを、毎朝9時に見る予約タスクに入れる（新しい月の成績がまだなら 8050 の「自動で取る」を始め、終わったら Discord へ1通）。
#   powershell -ExecutionPolicy Bypass -File "毎月のお知らせを登録する.ps1"
# 何度走らせても増えない（同じ名前を置きなおすだけ）。PC が9時に止まっていたら、起きたあとに取り返す。
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$taskName = "Toushin-Notify"
$python = Join-Path $env:USERPROFILE "AppData\Local\Programs\Python\Python314\python.exe"
if (-not (Test-Path $python)) { $python = (Get-Command python -ErrorAction SilentlyContinue).Source }
if (-not $python) { Write-Error "python が見つかりません"; exit 1 }
New-Item -ItemType Directory -Force (Join-Path $here "data") | Out-Null
$script = Join-Path $here "notify.py"
$log = Join-Path $here "data\_notify_log.txt"
$argument = "-NoProfile -WindowStyle Hidden -Command " +
            "`"`$env:PYTHONIOENCODING='utf-8'; `$env:PYTHONUTF8='1'; " +
            "& '$python' '$script' *>> '$log'`""
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $argument -WorkingDirectory $here
$trigger = New-ScheduledTaskTrigger -Daily -At 9:00
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Write-Host "登録しました: $taskName（毎朝9時に見て、要る日だけ Discord へ1通）"
