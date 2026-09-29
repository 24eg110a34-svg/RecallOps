<#
.SYNOPSIS
    Install RecallOps as a Windows Task Scheduler task for auto-start on boot.

.DESCRIPTION
    Creates a scheduled task "RecallOps-24x7" that runs start-24x7.ps1 at system
    startup. The task runs whether or not a user is logged in, and restarts the
    service if it fails.

    Requires Administrator privileges.

.NOTES
    Run as Administrator:
      powershell -ExecutionPolicy Bypass -File scripts\install-task.ps1

    Uninstall:
      powershell -ExecutionPolicy Bypass -File scripts\uninstall-task.ps1
#>

[CmdletBinding()]
param(
    [string]$RepoRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$TaskName = "RecallOps-24x7"
)

$ErrorActionPreference = "Stop"

# --- Verify Administrator ----------------------------------------------------
$currentPrincipal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $currentPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Error "This script must be run as Administrator. Right-click PowerShell and select 'Run as Administrator'."
    exit 1
}

$startScript = Join-Path $RepoRoot "scripts\start-24x7.ps1"
if (-not (Test-Path $startScript)) { Write-Error "start-24x7.ps1 not found at $startScript"; exit 1 }

# --- Remove existing task if present -----------------------------------------
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Removing existing task '$TaskName' ..." -ForegroundColor Yellow
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

# --- Create the task ----------------------------------------------------------
$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$startScript`" -RepoRoot `"$RepoRoot`"" `
    -WorkingDirectory $RepoRoot

$trigger = New-ScheduledTaskTrigger -AtStartup

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 0)

$principal = New-ScheduledTaskPrincipal `
    -UserId "SYSTEM" `
    -LogonType ServiceAccount `
    -RunLevel Highest

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "RecallOps AI incident response platform - API on :8765, frontend on :4321" | Out-Null

Write-Host "Task '$TaskName' installed successfully." -ForegroundColor Green
Write-Host "  Starts at system boot"
Write-Host "  Restarts up to 3 times on failure (1-minute interval)"
Write-Host "  Runs as SYSTEM"
Write-Host ""
Write-Host "Start now with:  schtasks /run /tn $TaskName"
Write-Host "Remove with:     powershell -ExecutionPolicy Bypass -File scripts\uninstall-task.ps1"
