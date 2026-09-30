<#
.SYNOPSIS
    Start RecallOps as a 24/7 background service on Windows.

.DESCRIPTION
    Launches the FastAPI backend and Next.js frontend as detached background
    processes that survive the current PowerShell session. Designed to be called
    from a Windows Task Scheduler task for auto-start on boot.

    The script:
      1. Verifies Python and Node.js are available
      2. Starts the API on port 8765 (uvicorn)
      3. Starts the frontend on port 4321 (next start)
      4. Writes PID files so the service can be stopped cleanly
      5. Waits for both health endpoints to respond

.NOTES
    Run from the repository root:  powershell -ExecutionPolicy Bypass -File scripts\start-24x7.ps1
    Stop with:                      powershell -ExecutionPolicy Bypass -File scripts\stop-24x7.ps1
    Install auto-start on boot:     powershell -ExecutionPolicy Bypass -File scripts\install-task.ps1
#>

[CmdletBinding()]
param(
    [string]$RepoRoot = "",
    [string]$ApiPort = "8765",
    [string]$WebPort = "4321",
    [string]$DatabaseUrl = "sqlite:///./recallops.db",
    [switch]$NoSeed
)

$ErrorActionPreference = "Stop"

# $PSScriptRoot is not reliable inside a param() default (it is empty in some
# hosts), so resolve the repository root here instead.
if ([string]::IsNullOrWhiteSpace($RepoRoot)) {
    $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
    if ([string]::IsNullOrWhiteSpace($scriptDir)) { $scriptDir = $PSScriptRoot }
    if ([string]::IsNullOrWhiteSpace($scriptDir)) { $scriptDir = (Get-Location).Path }
    $RepoRoot = Split-Path -Parent $scriptDir
}
Set-Location -LiteralPath $RepoRoot

$pidDir = Join-Path $RepoRoot ".runtime"
New-Item -ItemType Directory -Path $pidDir -Force | Out-Null
$apiPidFile = Join-Path $pidDir "api.pid"
$webPidFile = Join-Path $pidDir "web.pid"
$apiLogFile = Join-Path $pidDir "api.log"
$webLogFile = Join-Path $pidDir "web.log"

# --- 1. Verify prerequisites -------------------------------------------------
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) { Write-Error "Python not found on PATH. Install Python 3.11+ and try again."; exit 1 }
$node = Get-Command node -ErrorAction SilentlyContinue
if (-not $node) { Write-Error "Node.js not found on PATH. Install Node.js 18+ and try again."; exit 1 }

# --- 2. Stop any existing instances ------------------------------------------
foreach ($f in @($apiPidFile, $webPidFile)) {
    if (Test-Path $f) {
        $oldPid = Get-Content $f -ErrorAction SilentlyContinue
        if ($oldPid) {
            Stop-Process -Id $oldPid -Force -ErrorAction SilentlyContinue
            Remove-Item $f -Force
        }
    }
}

# --- 3. Start the API --------------------------------------------------------
$env:PYTHONPATH = $RepoRoot
$env:DATABASE_URL = $DatabaseUrl
$env:DEMO_SEED_ON_START = if ($NoSeed) { "false" } else { "true" }
$env:HINDSIGHT_ENABLED = "0"
$env:LLM_PROVIDER = "local_heuristic"
$env:CORS_ORIGINS = "http://localhost:4321,http://127.0.0.1:4321"

$venvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$pythonExe = if (Test-Path $venvPython) { $venvPython } else { "python" }

Write-Host "Starting API on port $ApiPort ..." -ForegroundColor Cyan
$apiProc = Start-Process -FilePath $pythonExe `
    -ArgumentList "-m", "uvicorn", "recallops.main:app", "--host", "127.0.0.1", "--port", $ApiPort `
    -WorkingDirectory $RepoRoot `
    -RedirectStandardOutput $apiLogFile `
    -RedirectStandardError "$apiLogFile.err" `
    -WindowStyle Hidden `
    -PassThru
Set-Content -Path $apiPidFile -Value $apiProc.Id

# --- 4. Start the frontend ---------------------------------------------------
$env:NEXT_PUBLIC_API_URL = "http://127.0.0.1:$ApiPort"
$env:NEXT_TELEMETRY_DISABLED = "1"
$env:PORT = $WebPort

Write-Host "Starting frontend on port $WebPort ..." -ForegroundColor Cyan
$webProc = Start-Process -FilePath "npm" `
    -ArgumentList "run", "start" `
    -WorkingDirectory (Join-Path $RepoRoot "apps\web") `
    -RedirectStandardOutput $webLogFile `
    -RedirectStandardError "$webLogFile.err" `
    -WindowStyle Hidden `
    -PassThru
Set-Content -Path $webPidFile -Value $webProc.Id

# --- 5. Wait for health ------------------------------------------------------
Write-Host "Waiting for services to become healthy ..." -ForegroundColor Cyan
$deadline = (Get-Date).AddSeconds(60)
$apiHealthy = $false
$webHealthy = $false

while ((Get-Date) -lt $deadline) {
    if (-not $apiHealthy) {
        try {
            $r = Invoke-RestMethod -Uri "http://127.0.0.1:$ApiPort/health/live" -TimeoutSec 3
            if ($r.status -eq "alive") { $apiHealthy = $true; Write-Host "  API is alive (instance $($r.runtime.instance_id))" -ForegroundColor Green }
        } catch { Start-Sleep -Milliseconds 500 }
    }
    if (-not $webHealthy) {
        try {
            $r = Invoke-WebRequest -Uri "http://127.0.0.1:$WebPort" -TimeoutSec 3 -UseBasicParsing
            if ($r.StatusCode -eq 200) { $webHealthy = $true; Write-Host "  Frontend is serving" -ForegroundColor Green }
        } catch { Start-Sleep -Milliseconds 500 }
    }
    if ($apiHealthy -and $webHealthy) { break }
}

if (-not $apiHealthy) { Write-Warning "API did not become healthy within 60s. Check $apiLogFile" }
if (-not $webHealthy) { Write-Warning "Frontend did not become healthy within 60s. Check $webLogFile" }

Write-Host ""
Write-Host "RecallOps is running:" -ForegroundColor Green
Write-Host "  API:       http://127.0.0.1:$ApiPort  (PID $(Get-Content $apiPidFile))"
Write-Host "  Frontend:  http://127.0.0.1:$WebPort  (PID $(Get-Content $webPidFile))"
Write-Host "  Logs:      $apiLogFile"
Write-Host "             $webLogFile"
Write-Host ""
Write-Host "Stop with: powershell -ExecutionPolicy Bypass -File scripts\stop-24x7.ps1"
