<#
.SYNOPSIS
    Health check for the RecallOps 24/7 service.

.DESCRIPTION
    Verifies that the API and frontend are responding, and reports the backend
    instance ID so an operator can confirm they are talking to the process they
    expect. Intended for monitoring (Task Scheduler, Nagios, Zabbix, etc.).

.NOTES
    Exit code 0 = healthy, 1 = degraded, 2 = unreachable.
    Can be called from any monitoring system:
      powershell -ExecutionPolicy Bypass -File scripts\health-check.ps1
#>

[CmdletBinding()]
param(
    [string]$ApiPort = "8765",
    [string]$WebPort = "4321",
    [string]$ApiBase = "http://127.0.0.1:$ApiPort"
)

$healthy = $true
$exitCode = 0

# --- API liveness ------------------------------------------------------------
try {
    $live = Invoke-RestMethod -Uri "$ApiBase/health/live" -TimeoutSec 5
    if ($live.status -eq "alive") {
        Write-Host "API: alive (instance $($live.runtime.instance_id), uptime $([math]::Round($live.runtime.uptime_s))s)"
    } else {
        Write-Warning "API liveness returned unexpected status: $($live.status)"
        $healthy = $false
        $exitCode = 1
    }
} catch {
    Write-Error "API is not responding: $_"
    $healthy = $false
    $exitCode = 2
}

# --- API readiness -----------------------------------------------------------
if ($healthy) {
    try {
        $ready = Invoke-RestMethod -Uri "$ApiBase/health/ready" -TimeoutSec 5
        if ($ready.status -eq "ready") {
            Write-Host "API: ready (db=$($ready.checks.database_writable), schema=$($ready.checks.schema_complete))"
        } else {
            Write-Warning "API is not ready: $($ready.checks.error)"
            $healthy = $false
            $exitCode = 1
        }
    } catch {
        Write-Error "Readiness check failed: $_"
        $healthy = $false
        $exitCode = 2
    }
}

# --- Frontend ----------------------------------------------------------------
try {
    $resp = Invoke-WebRequest -Uri "http://127.0.0.1:$WebPort" -TimeoutSec 5 -UseBasicParsing
    if ($resp.StatusCode -eq 200) {
        Write-Host "Frontend: serving (HTTP 200)"
    } else {
        Write-Warning "Frontend returned HTTP $($resp.StatusCode)"
        $healthy = $false
        $exitCode = 1
    }
} catch {
    Write-Error "Frontend is not responding: $_"
    $healthy = $false
    $exitCode = 2
}

# --- Summary -----------------------------------------------------------------
if ($healthy) {
    Write-Host "RecallOps is healthy." -ForegroundColor Green
} else {
    Write-Warning "RecallOps is degraded (exit code $exitCode)." -ForegroundColor Yellow
}
exit $exitCode
