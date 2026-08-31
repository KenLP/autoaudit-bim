# Stage 2 Scenario S2.2 -- Converge in 1-3 iterations (LIVE writes)
#
# Drives --run-revit WITHOUT --dry-run against the live Snowdon Towers
# instance. Path B will commit Department writes for ~52 rooms via the
# Revit MCP addin. Iterate up to 3 times so a re-query after writes
# proves convergence (iter_2 findings < iter_0 findings).
#
# REQUIRES: autonomy.yaml flipped to severity_medium=auto FIRST.
# The script aborts pre-flight if the flip isn't in place. See
# DOGFOOD_PLAN Sec.Stage 2 + STAGE_2_log.md for the rationale.
#
# Usage (from repo root or bim-orchestrator/):
#     .\scripts\dogfood\s22_converge.ps1
#
# Output:
#     * Full log -> $env:TEMP\s22_converge.log
#     * Filtered -> stdout, partition / commits / iteration / summary

# NOTE: Do NOT set $ErrorActionPreference = "Stop" here. Python's asyncio
# emits "Using proactor: IocpProactor" to stderr on Windows startup,
# which PowerShell wraps as a NativeCommandError and -- with Stop --
# aborts the pipeline before Tee-Object can flush the log file. The
# orchestrator subprocess survives (it's a separate process tree), so
# writes still commit, but you lose the terminal evidence trail.
# Default Continue is fine here; the subprocess return code is what
# tells us success.

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Resolve-Path (Join-Path $scriptDir "..\..")
Set-Location $repoRoot

# Pre-flight: check autonomy.yaml has the flip in place. Match the
# specific line so a stray "auto" elsewhere in the file doesn't fool us.
$autonomyPath = Join-Path $repoRoot "config\autonomy.yaml"
$autonomy = Get-Content $autonomyPath -Raw
if ($autonomy -notmatch "severity_medium:\s*auto") {
    Write-Host "ABORT: autonomy.yaml still has severity_medium=approve (production default)." -ForegroundColor Red
    Write-Host "       Edit config\autonomy.yaml under mutations.parameters.set_value:"
    Write-Host "         severity_medium: auto    # TEMP for S2.2 -- revert after"
    Write-Host "       Then re-run this script. Revert is in s22_revert_autonomy.ps1."
    exit 2
}
Write-Host "Pre-flight OK: autonomy.yaml severity_medium=auto -- writes will execute." -ForegroundColor Green

$env:PYTHONIOENCODING = "utf-8"
$uv = "$env:USERPROFILE\.local\bin\uv.exe"
$logFile = "$env:TEMP\s22_converge.log"

Write-Host ""
Write-Host "S2.2 -- Converge in 1-3 iterations (LIVE Revit writes)" -ForegroundColor Cyan
Write-Host "Log -> $logFile"
Write-Host "Expected: iter_0 ~59 findings, iter_1 ~7 (Department fills committed)."
Write-Host ""

# NB: no --dry-run -- writes will land in Revit.
# --max-iterations 3 caps the cyclic graph; W6 D5 single-pass converges
# at iter_1 because the re-query sees Department auto-fills persisted.
& $uv run bim-orchestrator `
    --run-revit `
    --rules config/rules.room_compliance.yaml `
    --bep-fixture `
    --max-rooms 54 `
    --limit 100 `
    --no-forma `
    --max-iterations 3 `
    --verbose 2>&1 |
    Tee-Object -FilePath $logFile |
    Select-String -Pattern "design_agent\.(partition|start|done|revit\.executed)|qc_agent\.done|bump\.next_iteration|route\.|Run-Revit summary"

Write-Host ""
Write-Host "Done. Full log: $logFile" -ForegroundColor Green
Write-Host ""
Write-Host "NEXT: revert autonomy.yaml back to severity_medium=approve before any" -ForegroundColor Yellow
Write-Host "      further test runs or you'll start tripping production guard rails." -ForegroundColor Yellow
Write-Host "      Use: .\scripts\dogfood\s22_revert_autonomy.ps1"
