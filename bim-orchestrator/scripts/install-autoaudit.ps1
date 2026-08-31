<#
.SYNOPSIS
    Bootstrap installer for AutoAudit (bim-orchestrator) on a fresh pilot machine.

.DESCRIPTION
    Scripted version of docs/PILOT_INSTALL.md. Idempotent - safe to re-run;
    every step skips (or overwrites harmlessly) work that's already done.

    Steps:
      1. Ensure `uv` is installed (winget, falling back to pip).
      2. `uv sync --extra dev --extra service --inexact` in bim-orchestrator.
      3. (-WithAxes only) create Python 3.10 venvs for the lod-validator and
         spatial-qc satellite repos and pip-install them editable. If a
         Python 3.10 launcher isn't found, this WARNS and marks the axes
         unavailable - it does NOT fail the installer.
      4. Fetch forma-mcp.exe via scripts/fetch-forma-mcp.ps1.
      5. Generate .env / vendor/forma-mcp/.env / config/audit_services.yaml
         from their .example templates (skipped if already present).
      6. Create Start Menu shortcuts for "AutoAudit Console" (Streamlit) and
         "AutoAudit Service" (AuditHub API, :8601).
      7. Run `bim-orchestrator --doctor` as a final sanity check (if the
         installed build doesn't have --doctor yet, this warns, not fails).

.PARAMETER WithAxes
    Also set up the LOD + spatial audit satellites (each needs its own
    Python 3.10 venv - see CLAUDE.md "Audit axes ride the SAME K7 bucket").
    Requires -LodValidatorPath / -SpatialQcPath (or you'll be prompted).

.PARAMETER LodValidatorPath
    Local checkout path of the lod-validator satellite repo. Only used with
    -WithAxes.

.PARAMETER SpatialQcPath
    Local checkout path of the spatial-qc satellite repo. Only used with
    -WithAxes.

.EXAMPLE
    & scripts\install-autoaudit.ps1

.EXAMPLE
    & scripts\install-autoaudit.ps1 -WithAxes -LodValidatorPath "C:\path\to\lod-validator" -SpatialQcPath "C:\path\to\spatial-qc"
#>
param(
    [switch]$WithAxes,
    [string]$LodValidatorPath,
    [string]$SpatialQcPath
)

$ErrorActionPreference = "Stop"

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $repoRoot

function Write-Step {
    param([string]$Text)
    Write-Host ""
    Write-Host "==> $Text" -ForegroundColor Cyan
}

function Write-Warn2 {
    param([string]$Text)
    Write-Host "WARN: $Text" -ForegroundColor Yellow
}

function Copy-IfMissing {
    param([string]$Example, [string]$Target)
    if (Test-Path $Target) {
        Write-Host "  $Target already exists - leaving it alone."
        return $false
    }
    if (-not (Test-Path $Example)) {
        Write-Warn2 "template $Example not found - skipping $Target"
        return $false
    }
    Copy-Item $Example $Target
    Write-Host "  created $Target from $(Split-Path $Example -Leaf)"
    return $true
}

# --- 1. uv -----------------------------------------------------------------
Write-Step "Checking for uv"
if (Get-Command uv -ErrorAction SilentlyContinue) {
    Write-Host "  uv already installed: $(uv --version)"
} else {
    Write-Host "  uv not found - installing..."
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        winget install --id=astral-sh.uv -e --accept-source-agreements --accept-package-agreements
    } else {
        Write-Warn2 "winget not found - falling back to 'pip install uv'."
        if (-not (Get-Command pip -ErrorAction SilentlyContinue)) {
            Write-Error "Neither winget nor pip is available. Install uv manually: https://docs.astral.sh/uv/getting-started/installation/"
            exit 1
        }
        pip install uv
    }
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Write-Error "uv install did not put 'uv' on PATH - open a new shell and re-run this script, or install manually."
        exit 1
    }
    Write-Host "  installed: $(uv --version)"
}

# --- 2. Python deps ----------------------------------------------------------
Write-Step "Installing Python dependencies (uv sync --extra dev --extra service --inexact)"
# --inexact is load-bearing: a bare 'uv sync' exact-syncs the venv and will
# REMOVE the optional editable LLM-extension / rules_extractor installs if
# present (see CLAUDE.md). --inexact leaves anything extra alone.
uv sync --extra dev --extra service --inexact
if ($LASTEXITCODE -ne 0) {
    Write-Error "uv sync failed (exit $LASTEXITCODE) - see output above."
    exit 1
}

# --- 3. Audit satellites (optional) -----------------------------------------
$axesAvailable = $false
if ($WithAxes) {
    Write-Step "Setting up audit satellites (-WithAxes)"

    $py310Ok = $false
    try {
        & py -3.10 -c "print('ok')" 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { $py310Ok = $true }
    } catch {
        $py310Ok = $false
    }

    if (-not $py310Ok) {
        Write-Warn2 "Python 3.10 launcher ('py -3.10') not found. The LOD and spatial "
        Write-Warn2 "audit axes need their OWN Python 3.10 venvs (they pin deps that "
        Write-Warn2 "conflict with this project's Python 3.12+ env)."
        Write-Warn2 "Install Python 3.10 from https://www.python.org/downloads/ (check "
        Write-Warn2 "'Add to PATH' and the 'py launcher' component), then re-run with "
        Write-Warn2 "-WithAxes. Continuing WITHOUT axes - the LOI (rule) checks still work."
    } else {
        if (-not $LodValidatorPath) {
            $LodValidatorPath = Read-Host "Path to the lod-validator repo checkout"
        }
        if (-not $SpatialQcPath) {
            $SpatialQcPath = Read-Host "Path to the spatial-qc repo checkout"
        }

        function Install-Satellite {
            param([string]$Name, [string]$RepoPath, [string]$PipExtra)
            if (-not $RepoPath -or -not (Test-Path $RepoPath)) {
                Write-Warn2 "$Name path '$RepoPath' not found - skipping. Clone/copy the repo "
                Write-Warn2 "first, then re-run with -WithAxes -${Name}Path <path>."
                return $null
            }
            $venvDir = Join-Path $RepoPath ".venv"
            if (-not (Test-Path $venvDir)) {
                Write-Host "  creating Python 3.10 venv for $Name at $venvDir"
                & py -3.10 -m venv $venvDir
                if ($LASTEXITCODE -ne 0) {
                    Write-Warn2 "venv creation failed for $Name - skipping."
                    return $null
                }
            } else {
                Write-Host "  $Name venv already exists - reusing $venvDir"
            }
            $venvPython = Join-Path $venvDir "Scripts\python.exe"
            $pkgSpec = if ($PipExtra) { ".[$PipExtra]" } else { "." }
            Push-Location $RepoPath
            try {
                & $venvPython -m pip install -e $pkgSpec
                $installOk = ($LASTEXITCODE -eq 0)
            } finally {
                Pop-Location
            }
            if (-not $installOk) {
                Write-Warn2 "pip install -e failed for $Name - check output above."
                return $null
            }
            return @{ python = $venvPython; cwd = $RepoPath }
        }

        $lodInfo = Install-Satellite -Name "LodValidator" -RepoPath $LodValidatorPath -PipExtra "mcp"
        $spatialInfo = Install-Satellite -Name "SpatialQc" -RepoPath $SpatialQcPath -PipExtra $null

        if ($lodInfo -and $spatialInfo) {
            $axesAvailable = $true

            $servicesExample = Join-Path $repoRoot "config\audit_services.yaml.example"
            $servicesTarget = Join-Path $repoRoot "config\audit_services.yaml"
            $created = Copy-IfMissing -Example $servicesExample -Target $servicesTarget
            if ($created) {
                # Fresh file - fill in the paths we just set up so it's ready to use,
                # not just a template with placeholder paths from another machine.
                $yaml = @"
# Machine-local paths to the audit satellites (Phase 3, D3).
# Generated by scripts/install-autoaudit.ps1 -WithAxes on $(Get-Date -Format 'yyyy-MM-dd').
# Each satellite runs in ITS OWN Python 3.10 venv - never merge the envs.
# A missing file or entry does not crash: the axis reports "unconfigured".
lod_validator:
  python: "$($lodInfo.python -replace '\\','/')"
  cwd:    "$($lodInfo.cwd -replace '\\','/')"
spatial_qc:
  python: "$($spatialInfo.python -replace '\\','/')"
  cwd:    "$($spatialInfo.cwd -replace '\\','/')"
"@
                Set-Content -Path $servicesTarget -Value $yaml -Encoding utf8
                Write-Host "  wrote satellite paths into $servicesTarget"
            } else {
                Write-Host "  audit_services.yaml already existed - not overwriting with the new paths."
                Write-Host "  (edit it by hand if you want to point at the venvs just created)"
            }
        } else {
            Write-Warn2 "one or both satellites did not install cleanly - axes remain unavailable."
        }
    }
} else {
    Write-Host ""
    Write-Host "Skipping audit satellites (pass -WithAxes to set up LOD + spatial axes)."
}

# --- 4. forma-mcp.exe --------------------------------------------------------
Write-Step "Fetching forma-mcp.exe"
& (Join-Path $PSScriptRoot "fetch-forma-mcp.ps1")
if ($LASTEXITCODE -ne 0) {
    Write-Warn2 "fetch-forma-mcp.ps1 failed - ACC connectivity will not work until this is resolved by hand (check network access to github.com; the download is a public HTTPS release and needs no login)."
}

# --- 5. .env / vendor .env / audit_services.yaml ----------------------------
Write-Step "Generating config from .example templates"
Copy-IfMissing -Example (Join-Path $repoRoot ".env.example") -Target (Join-Path $repoRoot ".env") | Out-Null
Copy-IfMissing -Example (Join-Path $repoRoot "vendor\forma-mcp\.env.example") -Target (Join-Path $repoRoot "vendor\forma-mcp\.env") | Out-Null
if (-not $WithAxes) {
    # -WithAxes already handled audit_services.yaml above (with real paths filled in).
    Copy-IfMissing -Example (Join-Path $repoRoot "config\audit_services.yaml.example") -Target (Join-Path $repoRoot "config\audit_services.yaml") | Out-Null
}
Write-Host ""
Write-Host "  Fill in ACC/APS credentials in .env and vendor\forma-mcp\.env by hand"
Write-Host "  (see docs\PILOT_INSTALL.md steps 5-6 for what each key means)."

# --- 6. Start Menu shortcuts -------------------------------------------------
Write-Step "Creating Start Menu shortcuts"
$startMenu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
$shell = New-Object -ComObject WScript.Shell

function New-AppShortcut {
    param([string]$Name, [string]$Command)
    $lnkPath = Join-Path $startMenu "$Name.lnk"
    $shortcut = $shell.CreateShortcut($lnkPath)
    $shortcut.TargetPath = "powershell.exe"
    $shortcut.Arguments = "-NoExit -Command `"Set-Location '$repoRoot'; $Command`""
    $shortcut.WorkingDirectory = $repoRoot
    $shortcut.Description = $Name
    $shortcut.Save()
    Write-Host "  $lnkPath"
}

New-AppShortcut -Name "AutoAudit Console" -Command "uv run streamlit run streamlit_app/app.py"
New-AppShortcut -Name "AutoAudit Service" -Command "uv run autoaudit-service"

# --- 7. Doctor check ----------------------------------------------------------
Write-Step "Running bim-orchestrator --doctor"
uv run bim-orchestrator --doctor
$doctorExit = $LASTEXITCODE
if ($doctorExit -eq 0) {
    Write-Host ""
    Write-Host "Install complete - doctor checks passed." -ForegroundColor Green
} elseif ($doctorExit -eq 2 -or $doctorExit -eq $null) {
    # argparse's "unrecognized arguments" exits 2; treat any non-clean doctor
    # run as informational, not a hard installer failure - --doctor may not
    # exist yet in this build (it ships alongside this script but can land
    # in a separate commit), and per-machine WARN-level gaps (e.g. no Revit
    # open, no satellites) are expected and already explained in its output.
    Write-Warn2 "doctor exited $doctorExit - see its output above."
    Write-Warn2 "If it says unrecognized arguments --doctor, this build does not have it yet."
    Write-Warn2 "Everything else above still completed."
} else {
    Write-Warn2 "doctor exited $doctorExit - review its output above for what needs attention."
}

Write-Host ""
Write-Host "Next: docs\PILOT_INSTALL.md steps 5, 6, 8, and 11 (fill credentials, export an IFC, run your first --audit)." -ForegroundColor Cyan
