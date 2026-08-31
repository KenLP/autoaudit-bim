<#
.SYNOPSIS
    Download forma-mcp.exe (the SEA standalone build) into vendor/forma-mcp/,
    verifying its SHA-256 before it is allowed to become the installed binary.

.DESCRIPTION
    The exe is a build artifact of the sibling repo acc-forma-mcp-server,
    distributed via GitHub Releases (rolling tag `forma-mcp-sea`), NOT committed
    to git. It is an unsigned executable, so this script does the verifying that
    a code signature would otherwise do.

    How the expected hash is obtained, strongest first:

      1. -ExpectedSha256 <hex>   You pin it out of band. This is the only mode
                                 that survives a compromise of the release
                                 itself, and it is what a locked-down company
                                 machine should use.
      2. The GitHub API          The release asset carries a published digest.
                                 Verifying against it catches corruption in
                                 transit, a truncated download, or a proxy
                                 serving something else. It does NOT prove the
                                 release was not tampered with at source: the
                                 hash and the file come from the same origin.
      3. -SkipHashCheck          No verification. Prints a warning and says so.
                                 There when a network genuinely cannot reach
                                 api.github.com; not a default.

    Why not pin a constant here: `forma-mcp-sea` is a ROLLING tag. A hash baked
    into this repo would go stale every time the server is rebuilt, and a fetch
    that fails on every legitimate republish teaches people to pass
    -SkipHashCheck, which is worse than not checking.

    The download lands in a .tmp file and is renamed over the real path only
    after the hash matches, so a failed or interrupted run can never leave a
    half-written or unverified exe where the orchestrator will launch it.

    Run once after cloning (or whenever the server is rebuilt + republished):
        powershell -ExecutionPolicy Bypass -File scripts/fetch-forma-mcp.ps1

.PARAMETER Tag
    Release tag to download from. Defaults to the rolling `forma-mcp-sea` tag.

.PARAMETER Repo
    owner/name of the server repo. Defaults to KenLP/acc-forma-mcp-server.

.PARAMETER ExpectedSha256
    Pin the expected hash yourself. Overrides the published digest.

.PARAMETER SkipHashCheck
    Install without verifying. Warns loudly; use only when you must.
#>
param(
    [string]$Tag  = "forma-mcp-sea",
    [string]$Repo = "KenLP/acc-forma-mcp-server",
    [string]$ExpectedSha256,
    [switch]$SkipHashCheck
)

$ErrorActionPreference = "Stop"

$vendorDir = Join-Path $PSScriptRoot "..\vendor\forma-mcp"
New-Item -ItemType Directory -Force -Path $vendorDir | Out-Null
$exe     = Join-Path $vendorDir "forma-mcp.exe"
$tmp     = "$exe.tmp"
$sidecar = "$exe.sha256"
$url     = "https://github.com/$Repo/releases/download/$Tag/forma-mcp.exe"

# ---- 1. what hash do we expect? --------------------------------------------
$expected = $null
$hashSource = $null
if ($ExpectedSha256) {
    $expected = $ExpectedSha256.ToLower() -replace '^sha256:', ''
    $hashSource = "pinned on the command line"
} elseif (-not $SkipHashCheck) {
    try {
        $api = "https://api.github.com/repos/$Repo/releases/tags/$Tag"
        $rel = Invoke-RestMethod -Uri $api -Headers @{ "User-Agent" = "autoaudit-fetch" } -UseBasicParsing
        $asset = $rel.assets | Where-Object { $_.name -eq "forma-mcp.exe" } | Select-Object -First 1
        if ($asset -and $asset.digest) {
            $expected = ($asset.digest -replace '^sha256:', '').ToLower()
            $hashSource = "published with the release"
        }
    } catch {
        Write-Host "Could not reach the GitHub API for the published digest: $($_.Exception.Message)" -ForegroundColor Yellow
    }
    if (-not $expected) {
        Write-Error @"
No expected SHA-256 available, so the download cannot be verified.

This binary is unsigned; installing it unverified is a real risk on a machine
that will hold ACC credentials. Options:

  * Read the hash from the release page and pass it:
      scripts\fetch-forma-mcp.ps1 -ExpectedSha256 <hex>
  * Or, if you accept the risk and know why:
      scripts\fetch-forma-mcp.ps1 -SkipHashCheck
"@
        exit 1
    }
}

# ---- 2. download to a temporary file ---------------------------------------
if (Test-Path $tmp) { Remove-Item $tmp -Force }
Write-Host "Downloading forma-mcp.exe from $Repo release '$Tag'..."
try {
    # -UseBasicParsing works on a machine with no IE engine configured; the
    # progress bar is suppressed because it makes large downloads dramatically
    # slower in Windows PowerShell 5.1.
    $prev = $ProgressPreference
    $ProgressPreference = "SilentlyContinue"
    Invoke-WebRequest -Uri $url -OutFile $tmp -UseBasicParsing
    $ProgressPreference = $prev
} catch {
    Write-Host "Direct download failed: $($_.Exception.Message)" -ForegroundColor Yellow
    if (Get-Command gh -ErrorAction SilentlyContinue) {
        Write-Host "Retrying via GitHub CLI (works for private repos)..."
        gh release download $Tag --repo $Repo --pattern "forma-mcp.exe" --dir $vendorDir --clobber
        if (Test-Path $exe) { Move-Item -Force $exe $tmp }
    } else {
        if (Test-Path $tmp) { Remove-Item $tmp -Force }
        Write-Error @"
Could not download $url

If $Repo is public this is usually a network or proxy problem -- try the URL in
a browser. If it is private, install the GitHub CLI (https://cli.github.com),
run 'gh auth login', and re-run this script; it will then use your login.
"@
        exit 1
    }
}
if (-not (Test-Path $tmp)) {
    Write-Error "Download reported success but $tmp is missing."
    exit 1
}

# ---- 3. verify BEFORE it becomes the installed binary ----------------------
$actual = (Get-FileHash $tmp -Algorithm SHA256).Hash.ToLower()
if ($SkipHashCheck -and -not $expected) {
    Write-Host "WARNING: installed WITHOUT verification (-SkipHashCheck)." -ForegroundColor Red
    Write-Host "         sha256 = $actual" -ForegroundColor Red
} elseif ($actual -ne $expected) {
    Remove-Item $tmp -Force
    Write-Error @"
SHA-256 MISMATCH -- refusing to install.

  expected : $expected  ($hashSource)
  actual   : $actual

The downloaded file was discarded. Either the release was republished while
this ran (re-run the script), or something served you a different file.
"@
    exit 1
} else {
    Write-Host "SHA-256 verified ($hashSource): $actual" -ForegroundColor Green
}

# ---- 4. atomic-ish install + a sidecar --doctor can re-check against -------
Move-Item -Force $tmp $exe
if ($expected) {
    Set-Content -Path $sidecar -Value $actual -Encoding ascii -NoNewline
} elseif (Test-Path $sidecar) {
    # An unverified install must not inherit the previous install's proof.
    Remove-Item $sidecar -Force
}

$mb = [math]::Round((Get-Item $exe).Length / 1MB, 1)
Write-Host "OK: $exe ($mb MB)" -ForegroundColor Green
Write-Host "Next: copy vendor/forma-mcp/.env.example to .env and fill APS/SSA credentials (or use the Setup tab wizard)."
