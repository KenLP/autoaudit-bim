<#
.SYNOPSIS
    Seed the AU 2026 pilot-preview demo: one zero-network Demo Villa run plus
    two fixture approval proposals, ready for the AutoAudit UI.

.DESCRIPTION
    SPEC_AU_DEMO_PACKAGE.md item 3. Idempotent -- safe to re-run:
      (a) if no Demo Villa run exists yet under runs/, invoke
          `uv run bim-orchestrator --audit config/audit.au_demo.yaml` --
          the profile's `mode: demo` dispatches to the zero-network mock
          Revit + Forma loop (same path the UI's Run drawer exercises via
          POST /api/audits, so seeding also smoke-tests the demo mode).
      (b) copy the 2 committed fixture approval records from
          references/demo_approvals/ into runs/approvals/, skipping any
          file that is already there.

.PARAMETER ServiceUrl
    Printed at the end as the "next step" hint. Defaults to the AuditHub
    service's default bind address.
#>
param(
    [string]$ServiceUrl = "http://127.0.0.1:8601/ui"
)

$ErrorActionPreference = "Stop"

$RepoDir = Resolve-Path (Join-Path $PSScriptRoot "..")
$RunsDir = Join-Path $RepoDir "runs"
$ApprovalsDir = Join-Path $RunsDir "approvals"
$FixturesDir = Resolve-Path (Join-Path $PSScriptRoot "..\..\references\demo_approvals")
$DemoProjectId = "demo-villa-simulated"

function Test-DemoRunExists {
    if (-not (Test-Path $RunsDir)) {
        return $false
    }
    $runFolders = Get-ChildItem -Path $RunsDir -Directory -Filter "run-*" -ErrorAction SilentlyContinue
    foreach ($folder in $runFolders) {
        $metaPath = Join-Path $folder.FullName "metadata.json"
        if (-not (Test-Path $metaPath)) {
            continue
        }
        try {
            $meta = Get-Content $metaPath -Raw | ConvertFrom-Json
        } catch {
            continue
        }
        if ($meta.project_id -eq $DemoProjectId) {
            return $true
        }
    }
    return $false
}

Write-Host "=== AU demo seed ===" -ForegroundColor Cyan

if (Test-DemoRunExists) {
    Write-Host "Demo Villa run already present under runs/ -- skipping --demo (idempotent)."
} else {
    Write-Host "No Demo Villa run found -- running 'uv run bim-orchestrator --audit config/audit.au_demo.yaml'..."
    Push-Location $RepoDir
    try {
        uv run bim-orchestrator --audit config/audit.au_demo.yaml
        if ($LASTEXITCODE -ne 0) {
            Write-Error "bim-orchestrator --audit exited with code $LASTEXITCODE"
            exit 1
        }
    } finally {
        Pop-Location
    }
}

New-Item -ItemType Directory -Force -Path $ApprovalsDir | Out-Null

$fixtures = Get-ChildItem -Path $FixturesDir -Filter "*.json"
if ($fixtures.Count -eq 0) {
    Write-Error "No fixture files found in $FixturesDir"
    exit 1
}

foreach ($fixture in $fixtures) {
    $dest = Join-Path $ApprovalsDir $fixture.Name
    if (Test-Path $dest) {
        Write-Host "  Skipping $($fixture.Name) -- already present in runs/approvals/."
    } else {
        Copy-Item -Path $fixture.FullName -Destination $dest
        Write-Host "  Seeded $($fixture.Name) -> runs/approvals/"
    }
}

Write-Host ""
Write-Host "AU demo seeded -- open $ServiceUrl" -ForegroundColor Green
