<#
Nightly wrapper for Task Scheduler (Khối B, 2026-07-31).

scheduled_audit.ps1 assumes autoaudit-service is already up ("register it as a
separate at-logon task") — but at 22:30 on a box nobody touched since evening,
nothing guarantees that. This wrapper closes the gap: start the service if the
port is silent, wait for /health, then hand over to scheduled_audit.ps1 and
exit with ITS exit code (0 done | 2 failed | 3 busy | 4 unreachable | 5
timeout | 6 bad response | 7 service never became healthy).

The service is started with REVIT_MCP_VERSION=2027 (gate-closeout lesson:
default resolves to 7891 = Revit 2026) and is left running afterwards — it is
idle between audits and the next night reuses it.

Register (run only when user is logged on — Revit needs the session anyway):
  schtasks /Create /TN "AutoAudit Nightly" /SC DAILY /ST 22:30 /TR
    "powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\path\to\autoaudit-bim\bim-orchestrator\scripts\nightly_wrapper.ps1"
Prereqs the scheduler cannot provide: the machine awake at 22:30, the user
logged in, and Revit open with the target model (cloud models cannot be
launched from a command line — see docs/SCHEDULED_AUDIT.md).
#>
param(
    [string]$ProfilePath = "C:\path\to\autoaudit-bim\bim-orchestrator\config\audit.nightly_snowdon.yaml",
    [int]$Port = 8601,
    [int]$ServiceStartTimeoutSec = 90
)
$ErrorActionPreference = "Continue"
$repo = "C:\path\to\autoaudit-bim\bim-orchestrator"
$log  = Join-Path $repo "runs\nightly_wrapper.log"
function W([string]$m) {
    $line = "{0} | {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $m
    Add-Content -Path $log -Value $line -Encoding utf8; Write-Host $line
}

W "wrapper start profile=$ProfilePath"
$healthy = $false
try {
    $h = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 5
    if ($h.ok) { $healthy = $true; W "service already healthy" }
} catch { }

if (-not $healthy) {
    W "service not running - starting autoaudit-service"
    $env:REVIT_MCP_VERSION = "2027"
    $env:PYTHONIOENCODING = "utf-8"
    Start-Process -FilePath "uv" -ArgumentList "run", "autoaudit-service" `
        -WorkingDirectory $repo -WindowStyle Hidden
    $deadline = (Get-Date).AddSeconds($ServiceStartTimeoutSec)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 3
        try {
            $h = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 5
            if ($h.ok) { $healthy = $true; break }
        } catch { }
    }
    if (-not $healthy) { W "service never became healthy - abort"; exit 7 }
    W "service healthy"
}

& powershell -NoProfile -ExecutionPolicy Bypass `
    -File (Join-Path $repo "scripts\scheduled_audit.ps1") `
    -ProfilePath $ProfilePath
$code = $LASTEXITCODE
W "scheduled_audit exit=$code"
exit $code
