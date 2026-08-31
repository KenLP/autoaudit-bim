# Revert autonomy.yaml back to production default after S2.2.
# Idempotent -- safe to run twice.

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Resolve-Path (Join-Path $scriptDir "..\..")
$autonomyPath = Join-Path $repoRoot "config\autonomy.yaml"

$content = Get-Content $autonomyPath -Raw
if ($content -match "severity_medium:\s*auto") {
    $patched = $content -replace "severity_medium:\s*auto[^\r\n]*", "severity_medium: approve # missing required project params -- gate on humans"
    Set-Content $autonomyPath -Value $patched -NoNewline
    Write-Host "Reverted: severity_medium auto -> approve" -ForegroundColor Green
} else {
    Write-Host "Already at production default (severity_medium: approve). No change." -ForegroundColor Gray
}

Write-Host ""
Write-Host "Verify tests still green:"
Write-Host "  uv run pytest -q"
