<#
.SYNOPSIS
  One-time setup for CareHarbor AI on a Windows laptop (no admin rights needed).

.DESCRIPTION
  Creates the Python 3.11 virtual environment with uv, installs Python and UI dependencies,
  creates .env from .env.example (if missing), downloads the open data sources (if missing),
  seeds the synthetic clinic database (only if missing - existing bookings and chats are kept) and builds the RAG index.
  Safe to run again.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\setup.ps1
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\setup.ps1 -Reseed   # reset the demo data (deletes bookings and saved chats)
#>
[CmdletBinding()]
param(
    [switch]$SkipUI,        # skip npm install (API-only setup)
    [switch]$SkipModels,    # don't check for / pull Ollama models
    [switch]$Reseed         # rebuild the clinic database from scratch
)
$ErrorActionPreference = 'Continue'   # PowerShell 5.1 treats native tools' stderr as errors under 'Stop'; we check exit codes instead
Set-Location -Path $PSScriptRoot
$env:PYTHONUTF8 = '1'
$env:UV_LINK_MODE = 'copy'        # OneDrive / corporate folders break uv hardlinks
$env:UV_HTTP_TIMEOUT = '600'      # slow networks

function Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Native([scriptblock]$block) {
    & $block 2>&1 | ForEach-Object { "   $_" } | Out-Host
    return $LASTEXITCODE
}
function Retry([scriptblock]$block, [int]$times = 3) {
    for ($i = 1; $i -le $times; $i++) {
        if ((Native $block) -eq 0) { return }
        Write-Host "   attempt $i failed, retrying..." -ForegroundColor Yellow
        Start-Sleep -Seconds (5 * $i)
    }
    throw "Command failed after $times attempts."
}

Step 'Checking tools'
foreach ($tool in 'uv', 'node', 'npm') {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        if ($tool -eq 'uv') { throw "uv not found. Install (no admin): powershell -ExecutionPolicy ByPass -c `"irm https://astral.sh/uv/install.ps1 | iex`"" }
        if (-not $SkipUI) { throw "$tool not found. Install Node.js LTS (user scope): winget install OpenJS.NodeJS.LTS --scope user" }
    }
}
Write-Host "   uv $(uv --version)"
if (-not $SkipUI) { Write-Host "   node $(node --version)" }

Step 'Python 3.11 environment + dependencies (uv)'
if (Test-Path .venv) {
    # A previous interrupted run can leave a half-created venv: detect it and start clean.
    $venvOk = $false
    try { & .\.venv\Scripts\python.exe --version *> $null; $venvOk = ($LASTEXITCODE -eq 0) } catch { $venvOk = $false }
    if (-not $venvOk) { Write-Host '   existing .venv is broken - recreating'; Remove-Item .venv -Recurse -Force }
}
if (-not (Test-Path .venv)) { if ((Native { uv venv --python 3.11 }) -ne 0) { throw 'uv venv failed.' } }
Retry { uv sync }

Step 'Configuration'
function New-Secret([int]$n = 48) { -join ((48..57) + (65..90) + (97..122) | Get-Random -Count $n | ForEach-Object { [char]$_ }) }
if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
    Write-Host '   created .env from .env.example'
} else { Write-Host '   .env already exists - kept (missing settings are added below)' }
$envText = Get-Content .env -Raw
if ($envText -notmatch '(?m)^SESSION_SECRET=(?!change-me\s*$).+') {
    # Signs chat sessions, the staff cookie, OTP hashes and memory keys. A new value signs everyone out once.
    if ($envText -match '(?m)^SESSION_SECRET=') { $envText = $envText -replace '(?m)^SESSION_SECRET=.*', "SESSION_SECRET=$(New-Secret)" }
    else { $envText = $envText.TrimEnd() + "`r`nSESSION_SECRET=$(New-Secret)`r`n" }
    Set-Content .env $envText -Encoding UTF8 -NoNewline
    Write-Host '   generated a random SESSION_SECRET'
}
if ((Get-Content .env -Raw) -match '(?m)^STAFF_PASSWORD=change-me\s*$') {
    Write-Host '   NOTE: STAFF_PASSWORD is still "change-me" - edit .env before showing the staff console to anyone.' -ForegroundColor Yellow
}

Step 'Open data sources (Synthea + MedlinePlus)'
$needFetch = -not (Test-Path 'sources\synthea\synthea_sample_data_csv_latest.zip') -or ((Get-ChildItem 'content\medlineplus\*.md' -ErrorAction SilentlyContinue).Count -lt 20)
if ($needFetch) { Retry { uv run python scripts/fetch_sources.py } } else { Write-Host '   already present (bundled)' }

Step 'Clinic database'
if ($Reseed) {
    Write-Host '   -Reseed: rebuilding the clinic data (this deletes bookings and saved chats)'
    if ((Native { uv run python scripts/seed.py }) -ne 0) { throw 'Seeding failed.' }
} else {
    # Keeps an existing database (bookings + chat history); seeds only if it is missing or empty.
    if ((Native { uv run python scripts/seed.py --if-missing }) -ne 0) { throw 'Seeding failed.' }
}

Step 'Building the vectorless RAG index'
if ((Native { uv run python -c "from careharbor.rag.index import get_index; i=get_index(rebuild=True); print(f'{len(i.docs)} documents, {len(i.nodes)} sections')" }) -ne 0) { throw 'Index build failed.' }

if (-not $SkipUI) {
    Step 'UI dependencies (npm)'
    Push-Location frontend
    try { Retry { npm install --no-audit --no-fund --loglevel=error } } finally { Pop-Location }
}

if (-not $SkipModels) {
    Step 'Local models (Ollama)'
    $tags = curl.exe -s --max-time 5 http://localhost:11434/api/tags
    if (-not $tags) {
        Write-Host '   Ollama is not running. Install it from https://ollama.com and start it, or set LLM_BASE_URL/LLM_API_KEY/LLM_MODEL in .env for a cloud model.' -ForegroundColor Yellow
    } else {
        foreach ($m in 'qwen2.5:3b', 'qwen2.5:1.5b') {
            if ($tags -notmatch [regex]::Escape($m)) { Write-Host "   pulling $m ..."; ollama pull $m } else { Write-Host "   $m present" }
        }
    }
}

Step 'Done'
Write-Host '   Start everything with:   .\run.ps1' -ForegroundColor Green
Write-Host '   Stop it with:            .\run.ps1 -Stop'
