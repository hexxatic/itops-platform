#requires -RunAsAdministrator
<#
.SYNOPSIS
    Install the self-hosted GitHub Actions runner that deploys IT-Ops (design.md section 7).
    Run once, elevated, on the app server.

.DESCRIPTION
    Registers a runner for the IT-Ops repository only, labelled 'itops-deploy', running as its
    own gMSA. Only the deploy jobs target that label; CI checks stay on GitHub-hosted runners.

    The runner account is deliberately NOT a local admin. Install-ITOpsService.ps1 gives it
    exactly what deploying needs: modify on each environment's app\ folder and start/stop on
    each IT-Ops service. It can't read .env (secrets) or data, and has no database access.

.PARAMETER Token
    Short-lived runner registration token:
        gh api -X POST /repos/<owner>/<repo>/actions/runners/registration-token --jq .token
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string] $Token,
    [Parameter(Mandatory)] [string] $RepoUrl,
    [string] $RunnerDir = 'C:\ghr\IT-Ops',
    [string] $RunnerName = "$env:COMPUTERNAME-itops",
    [string] $Label = 'itops-deploy',
    [string] $Config = (Join-Path $PSScriptRoot '..\..\private\deploy\environments.psd1')
)

$ErrorActionPreference = 'Stop'
$account = (Import-PowerShellDataFile $Config).RunnerAccount

New-Item -ItemType Directory -Force -Path $RunnerDir | Out-Null
Set-Location $RunnerDir
if (-not (Test-Path (Join-Path $RunnerDir 'config.cmd'))) {
    $rel = Invoke-RestMethod 'https://api.github.com/repos/actions/runner/releases/latest' -Headers @{ 'User-Agent' = 'itops-setup' }
    $asset = $rel.assets | Where-Object { $_.name -match '^actions-runner-win-x64-[\d.]+\.zip$' } | Select-Object -First 1
    if (-not $asset) { throw "No win-x64 runner asset in $($rel.tag_name)." }
    $zip = Join-Path $RunnerDir $asset.name
    Invoke-WebRequest $asset.browser_download_url -OutFile $zip
    # GitHub publishes each asset's SHA-256; refuse anything that doesn't match.
    if ($asset.digest) {
        $want = ($asset.digest -replace '^sha256:', '').ToLower()
        $got = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
        if ($got -ne $want) { throw "Runner download checksum mismatch ($got vs $want)." }
    }
    Expand-Archive $zip -DestinationPath $RunnerDir -Force
    [IO.File]::Delete($zip)
}

# config.cmd refuses an empty --windowslogonpassword, so a gMSA can't be given here. Install
# as NETWORK SERVICE (its default), then switch the service to the gMSA: the same way the
# other gMSA runners on this server were set up. --replace makes re-runs idempotent.
& (Join-Path $RunnerDir 'config.cmd') --url $RepoUrl --token $Token --name $RunnerName --labels $Label `
    --unattended --replace --runasservice
if ($LASTEXITCODE -ne 0) { throw "Runner configuration failed ($LASTEXITCODE)." }

$svc = Get-CimInstance Win32_Service | Where-Object { $_.PathName -like "*$RunnerDir\bin\RunnerService.exe*" } | Select-Object -First 1
if (-not $svc) { throw "Runner service for $RunnerDir not found." }

# The gMSA needs to write the runner folder (work dirs, logs, updates).
$acl = Get-Acl $RunnerDir
$acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($account, 'Modify', 'ContainerInherit,ObjectInherit', 'None', 'Allow')))
Set-Acl $RunnerDir $acl

# 'Log on as a service' for the gMSA.
$sid = (New-Object Security.Principal.NTAccount($account)).Translate([Security.Principal.SecurityIdentifier]).Value
$inf = Join-Path $env:TEMP 'itops_runner_secpol.inf'; $sdb = Join-Path $env:TEMP 'itops_runner_secpol.sdb'
& secedit /export /areas USER_RIGHTS /cfg $inf | Out-Null
$content = Get-Content $inf
if (-not (($content -join "`n") -match [regex]::Escape("*$sid"))) {
    $content = $content -replace '^(SeServiceLogonRight\s*=.*)$', "`$1,*$sid"
    Set-Content -Path $inf -Value $content
    & secedit /configure /db $sdb /cfg $inf /areas USER_RIGHTS | Out-Null
}
foreach ($f in $inf, $sdb) { if (Test-Path $f) { [IO.File]::Delete($f) } }

Stop-Service $svc.Name -Force
$chg = Invoke-CimMethod -InputObject $svc -MethodName Change -Arguments @{ StartName = $account; StartPassword = '' }
if ($chg.ReturnValue -ne 0) { throw "Switching the runner to $account failed (Win32_Service.Change=$($chg.ReturnValue))." }
Start-Service $svc.Name
Write-Host "Runner '$RunnerName' ($Label) installed as service $($svc.Name), running as $account."
