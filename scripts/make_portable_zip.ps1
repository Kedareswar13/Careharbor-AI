<#
.SYNOPSIS
  Create a portable zip of CareHarbor AI to move to another laptop.

.DESCRIPTION
  Includes source, content, the downloaded open data sources (sources/), docs, evals and scripts.
  Excludes: .venv, node_modules, frontend/dist, .env (secrets), data/*.db, caches, logs, traces, .run, .git.
  The receiving laptop runs .\setup.ps1 then .\run.ps1.

.EXAMPLE
  .\scripts\make_portable_zip.ps1                 # -> ..\careharbor-portable-<timestamp>.zip
  .\scripts\make_portable_zip.ps1 -OutDir D:\
#>
[CmdletBinding()]
param([string]$OutDir = '')
$ErrorActionPreference = 'Stop'
# Windows PowerShell 5.1 leaves $PSCommandPath empty inside param() defaults, so resolve paths here.
$root = Split-Path -Parent $PSScriptRoot
if (-not $OutDir) { $OutDir = Split-Path -Parent $root }
$stamp = Get-Date -Format 'yyyyMMdd-HHmm'
$OutDir = (Resolve-Path $OutDir).Path
$zip = Join-Path $OutDir "careharbor-portable-$stamp.zip"
$staging = Join-Path $env:TEMP "careharbor-portable-$stamp"

$excludeDirs = @('.venv', 'node_modules', 'dist', '.git', '.run', 'logs', '__pycache__', '.pytest_cache', 'traces', 'index', 'cache')
$excludeFiles = @('.env', '*.db', '*.db-wal', '*.db-shm', '*.db-journal', '*.pyc', 'careharbor-portable-*.zip', 'timing_raw.json')

if (Test-Path $staging) { Remove-Item $staging -Recurse -Force }
New-Item -ItemType Directory -Path $staging | Out-Null
# robocopy is built into Windows; /XD and /XF exclude dirs/files anywhere in the tree.
robocopy $root (Join-Path $staging 'careharbor') /E /XD @excludeDirs /XF @excludeFiles /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy failed with code $LASTEXITCODE" }

# Safety net: never ship secrets.
$leaks = Get-ChildItem (Join-Path $staging 'careharbor') -Recurse -Force -Filter '.env' -ErrorAction SilentlyContinue
if ($leaks) { $leaks | Remove-Item -Force; Write-Host 'Removed stray .env files from the package.' -ForegroundColor Yellow }

Compress-Archive -Path (Join-Path $staging 'careharbor') -DestinationPath $zip -CompressionLevel Optimal -Force
Remove-Item $staging -Recurse -Force
$size = '{0:N1} MB' -f ((Get-Item $zip).Length / 1MB)
Write-Host "Portable zip created: $zip ($size)" -ForegroundColor Green
Write-Host 'On the new laptop: unzip -> cd careharbor -> .\setup.ps1 -> .\run.ps1'
