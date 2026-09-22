# 공개 링크(GitHub Pages) 자동 갱신을 작업 스케줄러에 등록한다. 한 번만 돌리면 된다(다시 돌리면 덮어쓴다).
#   powershell -ExecutionPolicy Bypass -File scripts\register-task.ps1
#
# 작업 이름: ETF-Radar-Publish
#   평일 08:55 부터 5분마다(장 마감 15:35 를 지나 15:55 까지) publish.ps1 -LiveOnly
#     -> 장중 시세만 올린다. 아침 수집으로 기준일이 바뀐 첫 실행은 저절로 전체(화면+결과+ETF 상세)를 올린다.
#   평일 19:10 에 한 번 더 -> 저녁 수집 결과 반영.
# 기록: data\publish.log  (안 올라갈 때 여기부터 본다)
# 이 PC 에 로그인돼 있을 때만 돈다(git 자격증명이 로그인 사용자 것이라서).
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$script = Join-Path $root "scripts\publish.ps1"
$log = Join-Path $root "data\publish.log"
New-Item -ItemType Directory -Force (Join-Path $root "data") | Out-Null

$cmd = "& '$script' -LiveOnly 2>&1 | Out-File -Append -Encoding utf8 '$log'; " +
       "if ((Get-Item '$log').Length -gt 1MB) { Get-Content '$log' -Tail 300 | Set-Content '$log' -Encoding utf8 }"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
  -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -Command `"$cmd`"" `
  -WorkingDirectory $root

$days = "Monday", "Tuesday", "Wednesday", "Thursday", "Friday"
$intraday = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $days -At 08:55
$intraday.Repetition = (New-ScheduledTaskTrigger -Once -At 08:55 -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Hours 7)).Repetition
$evening = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $days -At 19:10

$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 20) `
  -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName "ETF-Radar-Publish" -Action $action -Trigger @($intraday, $evening) `
  -Settings $settings -Principal $principal -Force `
  -Description "ETF 매집 레이더: 서버 결과를 GitHub Pages 에 올린다(평일 장중 5분마다, 19:10 한 번)" | Out-Null
Write-Output "등록했습니다: ETF-Radar-Publish  (기록: $log)"
Get-ScheduledTask -TaskName "ETF-Radar-Publish" | Select-Object -ExpandProperty Triggers | ForEach-Object { $_.StartBoundary + "  " + $_.Repetition.Interval }
