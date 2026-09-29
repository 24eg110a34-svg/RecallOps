<#
.SYNOPSIS
    Stop the RecallOps 24/7 background service.

.DESCRIPTION
    Stops the API and frontend processes started by start-24x7.ps1 using the
    PID files in .runtime/. Also removes the PID files.

.NOTES
    Run from the repository root:  powershell -ExecutionPolicy Bypass -File scripts\stop-24x7.ps1
#>

[CmdletBinding()]
param(
    [string]$RepoRoot = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = "Stop"
$pidDir = Join-Path $RepoRoot ".runtime"

foreach ($name in @("api", "web")) {
    $pidFile = Join-Path $pidDir "$name.pid"
    if (Test-Path $pidFile) {
        $procId = Get-Content $pidFile -ErrorAction SilentlyContinue
        if ($procId) {
            $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
            if ($proc) {
                Write-Host "Stopping $name (PID $procId) ..." -ForegroundColor Yellow
                Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
            } else {
                Write-Host "$name (PID $procId) is not running" -ForegroundColor DarkGray
            }
        }
        Remove-Item $pidFile -Force
    } else {
        Write-Host "$name PID file not found - service may not be running" -ForegroundColor DarkGray
    }
}

Write-Host "RecallOps stopped." -ForegroundColor Green
