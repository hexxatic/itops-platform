#requires -RunAsAdministrator
<#
.SYNOPSIS
    One-time setup of an IT-Ops environment on the app server: folders, permissions, config
    file, and the Windows service (run as the environment's gMSA). Idempotent; safe to re-run.

.DESCRIPTION
    Run once per environment, elevated, ON the app server. After this, deploys are
    Deploy-ITOps.ps1 (normally run by the self-hosted GitHub runner).

    Layout under the environment's Root:

      app\releases\<version>  unpacked release bundles     runner: modify   service: read
      app\current             link to the live release
      .env                    settings + secrets           service: read    runner: none
      data\files              uploaded files               service: modify  runner: none
      logs\                   service wrapper logs         service: modify
      service\                WinSW + its XML definition   service: read    runner: none

    The runner can deploy new code but never read the secrets or the data.

    .env is created with a generated session key and every non-secret setting filled in.
    The Entra client secret is NOT: set it with -EntraClientSecret or edit the file after.
    An existing .env is never overwritten.

.PARAMETER Bundle
    Any release zip (only its WinSW wrapper is used here).
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string] $Environment,
    [Parameter(Mandatory)] [string] $Bundle,
    [securestring] $EntraClientSecret,
    [string] $Config = (Join-Path $PSScriptRoot '..\..\private\deploy\environments.psd1')
)

$ErrorActionPreference = 'Stop'
$settings = Import-PowerShellDataFile $Config
$e = $settings[$Environment]
if (-not $e -or -not $e.Root) { throw "Unknown environment '$Environment' in $Config." }
$root = $e.Root; $svcName = $e.ServiceName; $svcAcct = $e.ServiceAccount; $runner = $settings.RunnerAccount

function Sid([string] $account) {
    (New-Object Security.Principal.NTAccount($account)).Translate([Security.Principal.SecurityIdentifier]).Value
}

function Set-FolderAcl([string] $path, [hashtable] $grants) {
    # Protected ACL: SYSTEM + Administrators full control, plus exactly the grants given.
    $acl = New-Object Security.AccessControl.DirectorySecurity
    $acl.SetAccessRuleProtection($true, $false)
    $inherit = 'ContainerInherit,ObjectInherit'
    foreach ($who in 'NT AUTHORITY\SYSTEM', 'BUILTIN\Administrators') {
        $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($who, 'FullControl', $inherit, 'None', 'Allow')))
    }
    foreach ($who in $grants.Keys) {
        $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($who, $grants[$who], $inherit, 'None', 'Allow')))
    }
    Set-Acl -Path $path -AclObject $acl
}

function Grant-ServiceLogonRight([string] $account) {
    $sid = Sid $account
    $inf = Join-Path $env:TEMP 'itops_secpol.inf'; $sdb = Join-Path $env:TEMP 'itops_secpol.sdb'
    & secedit /export /areas USER_RIGHTS /cfg $inf | Out-Null
    $content = Get-Content $inf
    if (($content -join "`n") -match [regex]::Escape("*$sid")) { Write-Host "  'log on as a service' already granted to $account"; return }
    if ($content -match '^SeServiceLogonRight') {
        $content = $content -replace '^(SeServiceLogonRight\s*=.*)$', "`$1,*$sid"
    } else {
        $content = $content -replace '(\[Privilege Rights\])', "`$1`r`nSeServiceLogonRight = *$sid"
    }
    Set-Content -Path $inf -Value $content
    & secedit /configure /db $sdb /cfg $inf /areas USER_RIGHTS | Out-Null
    foreach ($f in $inf, $sdb) { if (Test-Path $f) { [IO.File]::Delete($f) } }
    Write-Host "  granted 'log on as a service' to $account"
}

Write-Host "== IT-Ops $Environment -> $root (service $svcName as $svcAcct) =="

# ---- folders + permissions -----------------------------------------------------------------
foreach ($d in 'app\releases', 'data\files', 'logs', 'service') { New-Item -ItemType Directory -Force -Path (Join-Path $root $d) | Out-Null }
Set-FolderAcl $root                      @{ $svcAcct = 'ReadAndExecute' }
Set-FolderAcl (Join-Path $root 'app')    @{ $svcAcct = 'ReadAndExecute'; $runner = 'Modify' }
Set-FolderAcl (Join-Path $root 'data')   @{ $svcAcct = 'Modify' }
Set-FolderAcl (Join-Path $root 'logs')   @{ $svcAcct = 'Modify'; $runner = 'ReadAndExecute' }
# The service account runs the wrapper from here, so it needs read + execute (no secrets in it).
Set-FolderAcl (Join-Path $root 'service') @{ $svcAcct = 'ReadAndExecute' }
Write-Host "  folders + permissions set"

# ---- .env (never overwritten) ---------------------------------------------------------------
$envFile = Join-Path $root '.env'
if (Test-Path $envFile) {
    Write-Host "  .env exists; left as is"
} else {
    $bytes = New-Object byte[] 32
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)   # works on 5.1 and 7
    $secretKey = [Convert]::ToBase64String($bytes)
    $clientSecret = if ($EntraClientSecret) { [Net.NetworkCredential]::new('', $EntraClientSecret).Password } else { '' }
    $hosts = ConvertTo-Json -Compress @($e.Hostname, '127.0.0.1', '127.0.0.1:*', 'localhost', 'localhost:*')
    @(
        "# IT-Ops $Environment. Read by the service at startup (restart it after edits). Secrets: never copy this file elsewhere."
        'ITOPS_ENV=production'
        'ITOPS_AUTH_MODE=entra'
        "ITOPS_PUBLIC_URL=https://$($e.Hostname)"
        "ITOPS_ALLOWED_HOSTS=$hosts"
        "ITOPS_DB_SERVER=$($settings.DbServer)"
        "ITOPS_DB_NAME=$($e.Database)"
        '# No internal CA yet (design.md section 7): encrypted, certificate not validated.'
        'ITOPS_DB_TRUST_SERVER_CERTIFICATE=true'
        "ITOPS_FILES_DIR=$root\data\files"
        "ITOPS_AI_MODELS_DIR=$root\app\current\models"
        "ITOPS_ENTRA_TENANT_ID=$($settings.EntraTenantId)"
        "ITOPS_ENTRA_CLIENT_ID=$($settings.EntraClientId)"
        "ITOPS_ENTRA_CLIENT_SECRET=$clientSecret"
        "ITOPS_SECRET_KEY=$secretKey"
    ) | Set-Content -Path $envFile -Encoding utf8
    Remove-Variable secretKey, clientSecret
    $acl = New-Object Security.AccessControl.FileSecurity
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($who in 'NT AUTHORITY\SYSTEM', 'BUILTIN\Administrators') { $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($who, 'FullControl', 'Allow'))) }
    $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($svcAcct, 'Read', 'Allow')))
    Set-Acl -Path $envFile -AclObject $acl
    Write-Host "  .env created$(if (-not $EntraClientSecret) { ' (ITOPS_ENTRA_CLIENT_SECRET still empty: set it before the first start)' })"
}

# ---- service (WinSW) -------------------------------------------------------------------------
$svcDir = Join-Path $root 'service'
$exe = Join-Path $svcDir "$svcName.exe"
if (-not (Test-Path $exe)) {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [IO.Compression.ZipFile]::OpenRead((Resolve-Path $Bundle).Path)
    try {
        $entry = $zip.Entries | Where-Object { $_.FullName -replace '\\', '/' -eq 'service/WinSW-x64.exe' }
        if (-not $entry) { throw "$Bundle has no service/WinSW-x64.exe" }
        [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $exe)
    } finally { $zip.Dispose() }
}
$python = Join-Path $root 'app\current\python\python.exe'
@"
<service>
  <id>$svcName</id>
  <name>$($e.DisplayName)</name>
  <description>IT-Ops platform ($Environment). Loopback only; IIS site $($e.Hostname) fronts it.</description>
  <executable>$python</executable>
  <arguments>-I -m itops serve --host 127.0.0.1 --port $($e.Port)</arguments>
  <workingdirectory>$root</workingdirectory>
  <env name="HF_HUB_OFFLINE" value="1" />
  <stoptimeout>15 sec</stoptimeout>
  <onfailure action="restart" delay="10 sec" />
  <onfailure action="restart" delay="30 sec" />
  <onfailure action="restart" delay="2 min" />
  <resetfailure>1 hour</resetfailure>
  <logpath>$root\logs</logpath>
  <log mode="roll-by-size"><sizeThreshold>10240</sizeThreshold><keepFiles>8</keepFiles></log>
</service>
"@ | Set-Content -Path (Join-Path $svcDir "$svcName.xml") -Encoding utf8

if (-not (Get-Service $svcName -ErrorAction SilentlyContinue)) {
    & $exe install
    if ($LASTEXITCODE -ne 0) { throw "WinSW install failed ($LASTEXITCODE)." }
    Write-Host "  service $svcName installed"
} else {
    & $exe refresh | Out-Null
    Write-Host "  service $svcName exists; definition refreshed"
}

Grant-ServiceLogonRight $svcAcct
# Win32_Service.Change, not sc.exe: an empty password (gMSA) doesn't survive sc.exe from PowerShell.
$svcObj = Get-CimInstance Win32_Service -Filter "Name='$svcName'"
$chg = Invoke-CimMethod -InputObject $svcObj -MethodName Change -Arguments @{ StartName = $svcAcct; StartPassword = '' }
if ($chg.ReturnValue -ne 0) { throw "Setting run-as to $svcAcct failed (Win32_Service.Change=$($chg.ReturnValue))." }
Set-Service $svcName -StartupType Automatic
Write-Host "  run-as $svcAcct, automatic start"

# ---- let the runner start/stop this one service (and nothing else) ----------------------------
$runnerSid = Sid $runner
$sddl = ((& sc.exe sdshow $svcName) -join '').Trim()
if ($sddl -notmatch [regex]::Escape($runnerSid)) {
    $ace = "(A;;RPWPLCLORC;;;$runnerSid)"   # start, stop, query status, interrogate, read
    $new = if ($sddl -match 'S:') { $sddl -replace 'S:', "${ace}S:" } else { "$sddl$ace" }
    & sc.exe sdset $svcName $new | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Granting $runner control of $svcName failed." }
    Write-Host "  $runner may start/stop $svcName"
} else {
    Write-Host "  $runner already controls $svcName"
}

Write-Host "Done. Next: Install-ITOpsIIS.ps1 -Environment $Environment, then deploy a release."
