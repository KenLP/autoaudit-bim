<#
.SYNOPSIS
    Build the AutoAudit UI (autoaudit-ui/) into a static bundle the
    AuditHub service serves at /ui.

.DESCRIPTION
    Node is only needed at build time (v1.4-G posture: no Node at deploy).
    Runs `npm ci` (clean, lockfile-exact install) then `npm run build`
    inside autoaudit-ui/, producing autoaudit-ui/dist/ which
    service/spa.py mounts directly. Re-run this any time the UI source
    changes; the dist/ folder is gitignored (build artifact, not source).

.PARAMETER SkipInstall
    Skip `npm ci` and build with whatever is already in node_modules.
    Useful for repeated local builds once dependencies are installed.
#>
param(
    [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"

$uiDir = Join-Path $PSScriptRoot "..\..\autoaudit-ui"
$uiDir = (Resolve-Path $uiDir).Path

if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
    Write-Error "npm not found. Install Node.js (build-time only dependency) and retry."
    exit 1
}

Push-Location $uiDir
try {
    if (-not $SkipInstall) {
        Write-Host "Installing autoaudit-ui dependencies (npm ci)..."
        npm ci
        if ($LASTEXITCODE -ne 0) { throw "npm ci failed with exit code $LASTEXITCODE" }
    }

    Write-Host "Building autoaudit-ui..."
    npm run build
    if ($LASTEXITCODE -ne 0) { throw "npm run build failed with exit code $LASTEXITCODE" }
}
finally {
    Pop-Location
}

$distDir = Join-Path $uiDir "dist"
if (-not (Test-Path $distDir)) {
    Write-Error "Build finished but dist/ was not created at $distDir"
    exit 1
}

Write-Host ""
Write-Host "AutoAudit UI built: $distDir"
Write-Host "Served by the AuditHub service at http://127.0.0.1:8601/ui"
