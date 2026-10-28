<#
.SYNOPSIS
  Start or stop CareHarbor AI (API on 8010, UI on 5174).

.EXAMPLE
  .\run.ps1            # start API + UI (dev server) in the background, wait until healthy
  .\run.ps1 -Prod      # build the UI once and serve everything from the API on 8010
  .\run.ps1 -Stop      # stop only the processes this script started (by recorded PID trees)
  .\run.ps1 -Status
#>
[CmdletBinding()]
param([switch]$Stop, [switch]$Status, [switch]$Prod, [int]$ApiPort = 8010, [int]$UiPort = 5174)
$ErrorActionPreference = 'Continue'   # native tools write progress to stderr; failures are detected via exit codes / health checks
Set-Location -Path $PSScriptRoot
$env:PYTHONUTF8 = '1'
$env:UV_LINK_MODE = 'copy'
$runDir = Join-Path $PSScriptRoot '.run'
$logDir = Join-Path $PSScriptRoot 'logs'
$pidFile = Join-Path $runDir 'pids.json'
New-Item -ItemType Directory -Force -Path $runDir, $logDir | Out-Null

function Get-ListeningPid([int]$port) {
    $line = netstat -ano -p tcp | Select-String -Pattern "^\s*TCP\s+\S+:$port\s+\S+\s+LISTENING\s+(\d+)" | Select-Object -First 1
    if ($line) { return [int]$line.Matches[0].Groups[1].Value }
    return $null
}

function Read-Pids { if (Test-Path $pidFile) { return Get-Content $pidFile -Raw | ConvertFrom-Json } else { return $null } }

function Test-Http([string]$url) { (curl.exe -s -o NUL -w '%{http_code}' --max-time 4 $url) -eq '200' }

if ($Status) {
    $p = Read-Pids
    Write-Host "API $ApiPort : $(if (Test-Http "http://127.0.0.1:$ApiPort/api/health") {'up'} else {'down'})   UI $UiPort : $(if (Test-Http "http://127.0.0.1:$UiPort/") {'up'} else {'down'})"
    if ($p) { Write-Host "Recorded PIDs: api=$($p.api) ui=$($p.ui)" }
    return
}

if ($Stop) {
    $p = Read-Pids
    if (-not $p) { Write-Host 'Nothing to stop (no CareHarbor AI PIDs recorded).'; return }
    foreach ($name in 'ui', 'api') {
        $id = $p.$name
        if ($id -and (Get-Process -Id $id -ErrorAction SilentlyContinue)) {
            # Kill only the process TREE we started (uv -> python, npm -> node). Never by name.
            taskkill /PID $id /T /F 2>&1 | Out-Null
            Write-Host "Stopped CareHarbor AI $name (PID tree $id)"
        }
    }
    Remove-Item $pidFile -ErrorAction SilentlyContinue
    return
}

foreach ($port in @($ApiPort) + $(if ($Prod) { @() } else { @($UiPort) })) {
    $owner = Get-ListeningPid $port
    if ($owner) {
        $who = (Get-Process -Id $owner -ErrorAction SilentlyContinue).ProcessName
        throw "Port $port is already in use by PID $owner ($who). CareHarbor AI will not start a second server on the same port. Stop it first (.\run.ps1 -Stop if it is CareHarbor AI)."
    }
}
if (-not (Test-Path '.env')) { throw 'No .env found - run .\setup.ps1 first.' }
$env:API_PORT = "$ApiPort"
$env:UI_PORT = "$UiPort"

if ($Prod) {
    Write-Host 'Building the UI for production...'
    Push-Location frontend; try { npm run build 2>&1 | Out-Host } finally { Pop-Location }
}

Write-Host "Starting API on http://127.0.0.1:$ApiPort ..."
$api = Start-Process -FilePath 'uv' -ArgumentList "run uvicorn careharbor.api:app --host 127.0.0.1 --port $ApiPort" -WorkingDirectory $PSScriptRoot `
    -RedirectStandardOutput (Join-Path $logDir 'api.log') -RedirectStandardError (Join-Path $logDir 'api.err.log') -WindowStyle Hidden -PassThru
$ui = $null
if (-not $Prod) {
    Write-Host "Starting UI on http://127.0.0.1:$UiPort ..."
    $ui = Start-Process -FilePath 'npm.cmd' -ArgumentList 'run dev' -WorkingDirectory (Join-Path $PSScriptRoot 'frontend') `
        -RedirectStandardOutput (Join-Path $logDir 'ui.log') -RedirectStandardError (Join-Path $logDir 'ui.err.log') -WindowStyle Hidden -PassThru
}
@{ api = $api.Id; ui = $(if ($ui) { $ui.Id } else { $null }); started = (Get-Date).ToString('s') } | ConvertTo-Json | Set-Content $pidFile -Encoding UTF8

$deadline = (Get-Date).AddSeconds(150)
$apiUp = $false; $uiUp = $Prod
while ((Get-Date) -lt $deadline -and -not ($apiUp -and $uiUp)) {
    Start-Sleep -Seconds 2
    if (-not $apiUp) { $apiUp = Test-Http "http://127.0.0.1:$ApiPort/api/health" }
    if (-not $uiUp) { $uiUp = Test-Http "http://127.0.0.1:$UiPort/" }
    if ($api.HasExited) { break }
}
if (-not $apiUp) {
    Write-Host 'API did not become healthy. Last log lines:' -ForegroundColor Red
    Get-Content (Join-Path $logDir 'api.err.log') -Tail 25 -ErrorAction SilentlyContinue
    & $PSCommandPath -Stop
    exit 1
}
$url = if ($Prod) { "http://127.0.0.1:$ApiPort" } else { "http://127.0.0.1:$UiPort" }
Write-Host "`nCareHarbor AI is running." -ForegroundColor Green
Write-Host "  Patient chat : $url"
Write-Host "  Staff console: $url/#/staff"
Write-Host "  API health   : http://127.0.0.1:$ApiPort/api/health"
Write-Host "  Logs         : $logDir"
Write-Host "  Stop         : .\run.ps1 -Stop"
