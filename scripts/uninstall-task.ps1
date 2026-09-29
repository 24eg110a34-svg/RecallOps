<#
.SYNOPSIS
    Remove the RecallOps Windows Task Scheduler task.

.DESCRIPTION
    Unregisters the "RecallOps-24x7" scheduled task. Does not stop a running
    instance - use stop-24x7.ps1 for that.

.NOTES
    Run as Administrator:
      powershell -ExecutionPolicy Bypass -File scripts\uninstall-task.ps1
#>

[CmdletBinding()]
param(
    [string]$TaskName = "RecallOps-24x7"
)

$ErrorActionPreference = "Stop"

$currentPrincipal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $currentPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Error "This script must be run as Administrator."
    exit 1
}

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Task '$TaskName' removed." -ForegroundColor Green
} else {
    Write-Host "Task '$TaskName' was not installed." -ForegroundColor DarkGray
}
