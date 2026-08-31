<#
Scheduled audit client for AuditHub (Muc 1 continuous audit).
POSTs /audits, polls until done. Exit codes:
  0 done | 2 audit failed | 3 busy (409, another audit running)
  4 service unreachable | 5 timeout | 6 bad response
Register (daily 02:00, run whether user logged on or not — needs stored creds):
  Register-ScheduledTask -TaskName "AutoAudit Nightly" `
    -Action (New-ScheduledTaskAction -Execute "powershell.exe" `
      -Argument "-NoProfile -ExecutionPolicy Bypass -File D:\...\scripts\scheduled_audit.ps1 -ProfilePath D:\...\config\audit.nightly.yaml") `
    -Trigger (New-ScheduledTaskTrigger -Daily -At 02:00)
Prereq: autoaudit-service is running (register it as a separate at-logon task).
#>
param(
    [Parameter(Mandatory = $true)][string]$ProfilePath,
    [int]$Port = 8601,
    [int]$TimeoutMinutes = 120,
    [int]$PollSeconds = 15,
    [string]$LogPath = ""
)
$base = "http://127.0.0.1:$Port"
if ($LogPath -eq "") {
    $LogPath = Join-Path (Split-Path -Parent $PSScriptRoot) "runs\scheduled_audit.log"
}
function Write-Log([string]$msg) {
    $line = "{0} | {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    $dir = Split-Path -Parent $LogPath
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force $dir | Out-Null }
    Add-Content -Path $LogPath -Value $line -Encoding utf8
    Write-Host $line
}
Write-Log "start profile=$ProfilePath"
try {
    $resp = Invoke-RestMethod -Method Post -Uri "$base/audits" `
        -ContentType "application/json" `
        -Body (@{ profile_path = $ProfilePath } | ConvertTo-Json) -TimeoutSec 30
} catch {
    $status = $null
    if ($_.Exception.Response) { $status = [int]$_.Exception.Response.StatusCode }
    if ($status -eq 409) { Write-Log "busy: another audit is running"; exit 3 }
    Write-Log "service unreachable: $($_.Exception.Message)"; exit 4
}
$auditId = $resp.audit_id
if (-not $auditId) { Write-Log "bad response: no audit_id"; exit 6 }
Write-Log "accepted audit_id=$auditId"
$deadline = (Get-Date).AddMinutes($TimeoutMinutes)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds $PollSeconds
    try {
        $st = Invoke-RestMethod -Method Get -Uri "$base/audits/$auditId" -TimeoutSec 30
    } catch {
        Write-Log "poll failed (transient): $($_.Exception.Message)"; continue
    }
    if ($st.status -eq "done") {
        $summary = ""
        if ($st.summary) { $summary = ($st.summary | ConvertTo-Json -Compress) }
        Write-Log "done run_id=$($st.run_id) summary=$summary"; exit 0
    }
    if ($st.status -eq "failed") {
        Write-Log "failed run_id=$($st.run_id) error=$($st.error)"; exit 2
    }
}
Write-Log "timeout after $TimeoutMinutes minutes (audit_id=$auditId)"; exit 5
