# Stage 2 Scenario S2.1 -- Path B partition (dry-run)
#
# Drives `--run-revit --dry-run` against the live Snowdon Towers
# instance to verify DesignAgent splits findings correctly between
# Path A (manual -> ACC Issue) and Path B (auto -> Revit param write).
#
# Usage (from repo root or bim-orchestrator/):
#     .\scripts\dogfood\s21_partition.ps1
#
# Output:
#     * Full log    -> $env:TEMP\s21_partition.log
#     * Filtered    -> stdout, partition / preview / autonomy lines only
#
# Pre-flight: Revit 2026 must be running with Snowdon Towers loaded
# and the MCP addin responding on http://127.0.0.1:7891/health.

# NOTE: leave $ErrorActionPreference at the default Continue. asyncio's
# "Using proactor: IocpProactor" startup line goes to stderr and PowerShell
# wraps native stderr as errors -- with Stop, that abruptly kills the
# pipeline and you lose Tee-Object output. See s22_converge.ps1 for the
# full explanation.

# Anchor to bim-orchestrator regardless of where we were called from
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Resolve-Path (Join-Path $scriptDir "..\..")
Set-Location $repoRoot

$env:PYTHONIOENCODING = "utf-8"
$uv = "$env:USERPROFILE\.local\bin\uv.exe"
$logFile = "$env:TEMP\s21_partition.log"

Write-Host "S2.1 -- Path B partition (dry-run)" -ForegroundColor Cyan
Write-Host "Log -> $logFile"
Write-Host ""

& $uv run bim-orchestrator `
    --run-revit `
    --rules config/rules.room_compliance.yaml `
    --bep-fixture `
    --max-rooms 54 `
    --limit 100 `
    --no-forma `
    --dry-run `
    --verbose 2>&1 |
    Tee-Object -FilePath $logFile |
    Select-String -Pattern "design_agent\.(partition|start|done|revit\.preview|revit\.autonomy)|Run-Revit summary"

Write-Host ""
Write-Host "Done. Full log saved to: $logFile" -ForegroundColor Green
Write-Host "Run-Revit summary block is the bottom-most section in the full log."
