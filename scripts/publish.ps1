# 돌고 있는 서버의 결과를 GitHub Pages(gh-pages 브랜치)에 올린다.
#   powershell -ExecutionPolicy Bypass -File scripts\publish.ps1            전부(화면 + 결과 + ETF 상세)
#   powershell -ExecutionPolicy Bypass -File scripts\publish.ps1 -LiveOnly  장중 값만(몇 분마다 돌릴 때)
# gh-pages 는 매번 한 커밋으로 덮어쓴다 — 결과 파일의 이력은 남기지 않는다.
param(
  [string]$Server = "http://127.0.0.1:3200",
  [string]$Repo = "https://github.com/samsamgo/etf-radar.git",
  [switch]$LiveOnly
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$site = Join-Path $root "site"
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

# 장중 값만 올릴 때도 나머지 파일이 있어야 한다(브랜치를 통째로 덮어쓰므로)
$full = (-not $LiveOnly) -or (-not (Test-Path (Join-Path $site "data\bootstrap.json")))
$pyArgs = @((Join-Path $PSScriptRoot "publish_site.py"), $Server, $site)
if (-not $full) { $pyArgs += "--live" }
& $py @pyArgs
if ($LASTEXITCODE -ne 0) { throw "결과를 받지 못했습니다 — 서버($Server)가 떠 있는지 확인하세요." }

Push-Location $site
try {
  if (Test-Path ".git") { Remove-Item -Recurse -Force ".git" }
  # PowerShell 5.1 은 git 이 stderr 로 내는 '경고'도 오류로 친다 — 경고 자체를 끄고, 성패는 종료 코드로만 본다
  $ErrorActionPreference = "Continue"
  git init -q -b gh-pages
  git -c core.autocrlf=false -c core.safecrlf=false add -A
  git -c core.autocrlf=false commit -q -m ("site: " + (Get-Date -Format "yyyy-MM-dd HH:mm"))
  if ($LASTEXITCODE -ne 0) { throw "커밋하지 못했습니다." }
  git push -f -q $Repo gh-pages
  if ($LASTEXITCODE -ne 0) { throw "GitHub 에 올리지 못했습니다." }
  $ErrorActionPreference = "Stop"
} finally {
  if (Test-Path ".git") { Remove-Item -Recurse -Force ".git" }
  Pop-Location
}
Write-Output "올렸습니다 → https://samsamgo.github.io/etf-radar/"
