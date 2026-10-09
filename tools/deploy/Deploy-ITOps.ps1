<#
.SYNOPSIS
    Deploy an IT-Ops release bundle to one environment, with automatic rollback.

.DESCRIPTION
    Runs on the app server, normally as the self-hosted GitHub runner (design.md section 7);
    usable by hand too. Test and production use this same script, so production runs exactly
    what test already proved.

      1. Unpack the bundle into <Root>\app\releases\<version> (the running release is untouched).
      2. Stop the service, point <Root>\app\current at the new release, start it.
         The app applies its own database migrations at startup, as its service account, so
         the deploying account never needs database access.
      3. Wait for /health (loopback) to report ok.
      4. Not healthy in time: point current back at the previous release, restart, and fail.
      5. Keep the newest few releases, delete older ones.

    Migrations must stay backward-compatible (add, don't rename or drop in the same release):
    a rollback restores the old code, not the old schema.

.PARAMETER Environment
    An environment from the settings file (test, production).

.PARAMETER Bundle
    The release zip from Build-ITOpsRelease.ps1.

.PARAMETER Config
    Environment settings (.psd1). Default: private\deploy\environments.psd1 in the repo.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string] $Environment,
    [Parameter(Mandatory)] [string] $Bundle,
    [string] $Config = (Join-Path $PSScriptRoot '..\..\private\deploy\environments.psd1'),
    [int] $HealthTimeoutSeconds = 120,
    [int] $KeepReleases = 5
)

$ErrorActionPreference = 'Stop'
$settings = Import-PowerShellDataFile $Config
$envCfg = $settings[$Environment]
if (-not $envCfg -or -not $envCfg.Root) { throw "Unknown environment '$Environment' in $Config." }

$app      = Join-Path $envCfg.Root 'app'
$releases = Join-Path $app 'releases'
$current  = Join-Path $app 'current'
$health   = "http://127.0.0.1:$($envCfg.Port)/health"
$service  = $envCfg.ServiceName
if (-not (Get-Service $service -ErrorAction SilentlyContinue)) {
    throw "Service '$service' isn't installed. Run Install-ITOpsService.ps1 -Environment $Environment once (as an admin)."
}

function Get-CurrentTarget {
    $item = Get-Item $current -ErrorAction SilentlyContinue
    if ($item -and $item.LinkType -eq 'Junction') { return [string]$item.Target }
    return $null
}

function Set-Current([string] $target) {
    # Remove only the link, never what it points at.
    if (Get-Item $current -ErrorAction SilentlyContinue) { [IO.Directory]::Delete($current) }
    New-Item -ItemType Junction -Path $current -Target $target | Out-Null
}

function Restart-App {
    $svc = Get-Service $service
    if ($svc.Status -ne 'Stopped') {
        Stop-Service $service -Force
        $svc.WaitForStatus('Stopped', '00:00:30')
    }
    Start-Service $service
}

function Wait-Healthy([string] $expected, [switch] $AllowUnversioned) {
    # Healthy means: status ok, no module refused, AND /health reports the release we
    # just switched to. Otherwise an old process still holding the port would pass.
    $deadline = (Get-Date).AddSeconds($HealthTimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-RestMethod $health -TimeoutSec 5
            $ok = $r.status -eq 'ok' -and -not $r.refused.PSObject.Properties.Count
            # Releases from before v0.2.1 don't report their version (no "release" in
            # /health); a rollback to one of those is accepted on status alone.
            $legacy = $AllowUnversioned -and -not $r.release
            if ($ok -and ($r.version -eq $expected -or $legacy)) {
                Write-Host "Healthy: $($r | ConvertTo-Json -Compress -Depth 4)"
                return $true
            }
            if ($ok) { Write-Host "Answering, but as $($r.version), not $expected; waiting..."; Start-Sleep 2; continue }
            Write-Host "Not ready: $($r | ConvertTo-Json -Compress)"
        } catch { }
        Start-Sleep 2
    }
    return $false
}

# ---- 1. unpack -------------------------------------------------------------------------
New-Item -ItemType Directory -Force -Path $releases | Out-Null
$tmp = Join-Path $releases ('.incoming-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
Expand-Archive -Path $Bundle -DestinationPath $tmp
$version = (Get-Content (Join-Path $tmp 'release.json') -Raw | ConvertFrom-Json).version
$target = Join-Path $releases $version
if (Test-Path $target) {
    Write-Host "Release $version is already unpacked; reusing it."
    Remove-Item $tmp -Recurse -Force
} else {
    Rename-Item $tmp $target
}

$previous = Get-CurrentTarget
if ($previous -and ((Resolve-Path $previous).Path -eq (Resolve-Path $target).Path)) {
    Write-Host "$Environment is already on $version; restarting to be sure it's healthy."
}

# ---- 2-3. switch + verify ------------------------------------------------------------------
Write-Host "Deploying $version to $Environment (previous: $(if ($previous) { Split-Path $previous -Leaf } else { 'none' }))"
Set-Current $target
Restart-App
if (Wait-Healthy $version) {
    Write-Host "$Environment is on $version."
} else {
    # ---- 4. roll back --------------------------------------------------------------------
    Write-Warning "$version didn't become healthy within $HealthTimeoutSeconds s. Rolling back."
    $logs = Join-Path $envCfg.Root 'logs'
    Get-ChildItem $logs -Filter '*.err.log' -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime | Select-Object -Last 1 |
        ForEach-Object { Write-Host "--- last lines of $($_.Name) ---"; Get-Content $_.FullName -Tail 40 }
    if (-not $previous) { throw "No previous release to roll back to; $Environment is down." }
    Set-Current $previous
    Restart-App
    if (-not (Wait-Healthy (Split-Path $previous -Leaf) -AllowUnversioned)) { throw "Rollback to $(Split-Path $previous -Leaf) is unhealthy too; $Environment needs a person." }
    throw "Deploy of $version failed; rolled back to $(Split-Path $previous -Leaf)."
}

# ---- 5. prune --------------------------------------------------------------------------
$keep = @((Resolve-Path $target).Path)
if ($previous) { $keep += (Resolve-Path $previous).Path }
Get-ChildItem $releases -Directory | Where-Object { -not $_.Name.StartsWith('.') } |
    Sort-Object LastWriteTime -Descending | Select-Object -Skip $KeepReleases |
    Where-Object { $keep -notcontains $_.FullName } |
    ForEach-Object { Write-Host "Removing old release $($_.Name)"; Remove-Item $_.FullName -Recurse -Force -ErrorAction Continue }
