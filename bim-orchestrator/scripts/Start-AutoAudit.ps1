<#
.SYNOPSIS
    One-click launcher for AutoAudit: start the AuditHub service and open the UI.

.DESCRIPTION
    AutoAudit is ONE process (FastAPI/uvicorn on 127.0.0.1:8601) that backs BOTH
    surfaces the user thinks of as separate:
      * the web UI      -> http://127.0.0.1:8601/ui/   (any browser)
      * the Revit panel -> WebView2 pointed at the SAME origin
    So "start AutoAudit on Revit and on the web" is a single service start, not two.

    Idempotent by design: if the port is already listening this opens the browser
    instead of starting a second uvicorn that would only fail to bind.

    What this script deliberately does NOT do: start the Revit MCP addin. That
    addin lives INSIDE Revit and has no out-of-process entry point, so the script
    REPORTS its status (port 7891 + (version - 2026)) rather than pretending.

.PARAMETER Stop
    Stop the running service instead of starting one.

.PARAMETER NoBrowser
    Start the service but do not open a browser (Revit panel only).

.PARAMETER TimeoutSeconds
    How long to wait for /api/health. A cold start imports LangGraph + FastAPI,
    which is slow on first run; 90s is deliberately generous.

.EXAMPLE
    & scripts\Start-AutoAudit.ps1
.EXAMPLE
    & scripts\Start-AutoAudit.ps1 -Stop
#>
param(
    [switch]$Stop,
    [switch]$NoBrowser,
    [int]$TimeoutSeconds = 90
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

if ($env:AUTOAUDIT_PORT) { $port = [int]$env:AUTOAUDIT_PORT } else { $port = 8601 }
$uiUrl     = "http://127.0.0.1:$port/ui/"
$healthUrl = "http://127.0.0.1:$port/api/health"
$logDir    = Join-Path $repoRoot "runs"
$outLog    = Join-Path $logDir "service_console.out.log"
$errLog    = Join-Path $logDir "service_console.err.log"

function Test-TcpPort {
    # Ask the OS for a LISTENER on this port rather than dialing it. Two dialing
    # approaches were measured wrong here on 2026-08-25:
    #   * connecting to 127.0.0.1 misses a server bound to ::1 -- and vite binds
    #     the NAME "localhost", which Windows resolves to ::1 first, so netstat
    #     said [::1]:5173 LISTENING while the launcher declared failure;
    #   * adding a "::1" attempt did NOT fix it, because .NET Framework's
    #     TcpClient() defaults to an InterNetwork (v4) socket and cannot dial a
    #     v6 address at all.
    # Get-NetTCPConnection is address-family agnostic and is what -Stop already
    # relied on successfully, so both paths now share one source of truth.
    param([int]$Port)
    return ($null -ne (Get-ListenerPid -Port $Port))
}

function Wait-Key {
    # A shortcut double-click is interactive, but the same script run from a
    # non-interactive host (CI, an automation harness) must not die inside the
    # error path it was trying to report.
    param([string]$Message = "  Press Enter to close")
    try { Read-Host $Message | Out-Null } catch { Start-Sleep -Seconds 5 }
}

function Get-ListenerPid {
    # NOTE: never name this $pid -- that is a PowerShell automatic variable.
    param([int]$Port)
    try {
        $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop
        return ($conn | Select-Object -First 1).OwningProcess
    } catch {
        $hit = netstat -ano | Select-String ":$Port\s.*LISTENING"
        if ($hit) {
            $parts = ($hit[0].ToString().Trim() -split '\s+')
            return [int]$parts[$parts.Length - 1]
        }
        return $null
    }
}

function Get-DotEnvValue {
    param([string]$Key)
    $envFile = Join-Path $repoRoot ".env"
    if (-not (Test-Path $envFile)) { return $null }
    $m = Select-String -Path $envFile -Pattern "^\s*$Key\s*=\s*(.+?)\s*$" | Select-Object -First 1
    if ($m) { return $m.Matches[0].Groups[1].Value.Trim('"').Trim("'") }
    return $null
}

# --- stop mode ---------------------------------------------------------------
if ($Stop) {
    $procId = Get-ListenerPid -Port $port
    if (-not $procId) {
        Write-Host "AutoAudit is not running (port $port is free)." -ForegroundColor Yellow
        Start-Sleep -Seconds 2
        exit 0
    }
    # `autoaudit-service.exe` actually runs as a 3-process chain (console-script
    # shim -> venv python -> uv-managed python) and only the LAST link holds the
    # socket. Killing just that link was MEASURED on 2026-08-25 to collapse the
    # whole chain -- both ancestors exited on their own -- so no tree-kill is
    # needed here. Re-measure before adding one.
    Stop-Process -Id $procId -Force
    Write-Host "Stopped AutoAudit (pid $procId, port $port)." -ForegroundColor Green
    Start-Sleep -Seconds 2
    exit 0
}

# --- start -------------------------------------------------------------------
Write-Host ""
Write-Host "  AutoAudit launcher" -ForegroundColor Cyan
Write-Host "  $repoRoot"
Write-Host ""

$proc = $null
if (Test-TcpPort -Port $port) {
    Write-Host "  [ok] service already listening on port $port" -ForegroundColor Green
} else {
    $exe = Join-Path $repoRoot ".venv\Scripts\autoaudit-service.exe"
    if (Test-Path $exe) {
        $target = $exe
        $targetArgs = @()
    } else {
        # No venv console script (fresh clone / not yet `uv sync`ed) -- let uv
        # resolve it. Slower, but it works instead of erroring out.
        $target = "uv"
        $targetArgs = @("run", "autoaudit-service")
    }
    if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }

    $sp = @{
        FilePath               = $target
        WorkingDirectory       = $repoRoot   # load_dotenv() searches upward from cwd
        WindowStyle            = "Hidden"
        RedirectStandardOutput = $outLog
        RedirectStandardError  = $errLog
        PassThru               = $true
    }
    if ($targetArgs.Count -gt 0) { $sp.ArgumentList = $targetArgs }
    $proc = Start-Process @sp
    Write-Host "  starting service (pid $($proc.Id))" -NoNewline
}

# --- wait for health ---------------------------------------------------------
$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
$ready = $false
while ((Get-Date) -lt $deadline) {
    try {
        $r = Invoke-WebRequest -Uri $healthUrl -UseBasicParsing -TimeoutSec 3
        if ($r.StatusCode -eq 200) { $ready = $true; break }
    } catch {
        # not up yet -- expected during cold start, keep polling
    }
    if ($proc -and $proc.HasExited) { break }
    Start-Sleep -Milliseconds 700
    if ($proc) { Write-Host "." -NoNewline }
}
if ($proc) { Write-Host "" }

if (-not $ready) {
    Write-Host ""
    Write-Host "  [FAIL] service did not answer $healthUrl" -ForegroundColor Red
    if (Test-Path $errLog) {
        Write-Host "  --- last lines of $errLog ---" -ForegroundColor DarkGray
        Get-Content $errLog -Tail 15 | ForEach-Object { Write-Host "  $_" -ForegroundColor DarkGray }
    }
    Write-Host ""
    Wait-Key
    exit 1
}

Write-Host "  [ok] service healthy      $healthUrl" -ForegroundColor Green
Write-Host "  [ok] web UI               $uiUrl" -ForegroundColor Green
Write-Host "  [ok] Revit panel uses the same origin -- no second start needed"

# --- Revit addin status (informational; cannot be started from here) ---------
$revitVer = $env:REVIT_MCP_VERSION
if (-not $revitVer) { $revitVer = Get-DotEnvValue "REVIT_MCP_VERSION" }
if (-not $revitVer) { $revitVer = "2026" }
if ($env:REVIT_MCP_PORT) {
    $revitPort = [int]$env:REVIT_MCP_PORT
} else {
    $revitPort = 7891 + [Math]::Max(0, ([int]$revitVer - 2026))
}
if (Test-TcpPort -Port $revitPort) {
    Write-Host "  [ok] Revit $revitVer addin listening on $revitPort" -ForegroundColor Green
} else {
    Write-Host "  [--] Revit $revitVer addin NOT listening on $revitPort" -ForegroundColor Yellow
    Write-Host "       open Revit + a model; the panel connects on its own"
}

if (-not $NoBrowser) { Start-Process $uiUrl }

Write-Host ""
Write-Host "  Stop it with:  scripts\Start-AutoAudit.ps1 -Stop"
Write-Host ""
Start-Sleep -Seconds 3
