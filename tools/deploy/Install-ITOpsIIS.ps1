#requires -RunAsAdministrator
<#
.SYNOPSIS
    The IIS front door for one IT-Ops environment: a reverse-proxy site (URL Rewrite + ARR)
    forwarding to the loopback service. Run once per environment, elevated, on the app server.

.DESCRIPTION
    IT-Ops binds 127.0.0.1 only; IIS is its public face and terminates TLS. Beyond a plain
    proxy, IT-Ops needs (design.md section 7):

      - Response buffering OFF: MCP (AI tools) and live updates stream responses. ARR buffers
        by default, which stalls them.
      - Uploads up to 55 MB (attachments; the app itself caps at ITOPS_MAX_ATTACHMENT_MB, 50).
      - The public name and scheme reach the app as X-Forwarded-Host / X-Forwarded-Proto (set
        by this site's rewrite rule). ARR's server-wide preserveHostHeader is left alone, so the
        other proxied sites on the server see no change. The app trusts those headers only
        from loopback.
      - The caller's address reaches the app as X-Forwarded-For, which the rule SETS to
        {REMOTE_ADDR}, replacing anything the caller sent, so nobody can claim another
        address (core/ratelimit.py; Cloudflare's CF-Connecting-IP is believed only when that
        caller is the tunnel host, ITOPS_TRUSTED_PROXIES).

    Prerequisites: URL Rewrite + ARR installed, and a DNS A record for the hostname pointing
    at this server. TLS: issue the certificate with Certify the Web (DNS-01) for the same
    hostname and bind it to this site, like the other sites on this server.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string] $Environment,
    [string] $Config = (Join-Path $PSScriptRoot '..\..\private\deploy\environments.psd1')
)

$ErrorActionPreference = 'Stop'
Import-Module WebAdministration
$settings = Import-PowerShellDataFile $Config
$e = $settings[$Environment]
if (-not $e -or -not $e.Root) { throw "Unknown environment '$Environment' in $Config." }
$site = $e.ServiceName
$root = Join-Path $env:SystemDrive "inetpub\$($site.ToLower())"

# ARR's proxy must be on for a rewrite to an absolute URL to proxy (server-wide flag; the
# other proxy sites on this server rely on it too).
Set-WebConfigurationProperty -PSPath 'MACHINE/WEBROOT/APPHOST' -Filter 'system.webServer/proxy' -Name 'enabled' -Value 'True'

New-Item -ItemType Directory -Force -Path $root | Out-Null
# HTTP -> HTTPS only once the site has a certificate (Certify adds the https binding);
# before that, redirecting would send people to an address that can't answer. Re-run this
# script after the certificate is bound.
$hasHttps = [bool](Get-WebBinding -Name $site -Protocol https -ErrorAction SilentlyContinue)
$redirect = if ($hasHttps) { @"
        <rule name="HTTP to HTTPS" stopProcessing="true">
          <match url="(.*)" />
          <conditions><add input="{HTTPS}" pattern="off" /></conditions>
          <action type="Redirect" url="https://{HTTP_HOST}/{R:1}" redirectType="Permanent" />
        </rule>
"@ } else { '' }
@"
<?xml version="1.0" encoding="UTF-8"?>
<configuration>
  <system.webServer>
    <rewrite>
      <rules>
$redirect        <rule name="IT-Ops reverse proxy" stopProcessing="true">
          <match url="(.*)" />
          <conditions>
            <add input="{CACHE_URL}" pattern="^(https?)://" />
          </conditions>
          <serverVariables>
            <set name="HTTP_X_FORWARDED_HOST" value="{HTTP_HOST}" />
            <set name="HTTP_X_FORWARDED_PROTO" value="{C:1}" />
            <set name="HTTP_X_FORWARDED_FOR" value="{REMOTE_ADDR}" />
          </serverVariables>
          <action type="Rewrite" url="http://127.0.0.1:$($e.Port)/{R:1}" />
        </rule>
      </rules>
    </rewrite>
    <security>
      <requestFiltering>
        <requestLimits maxAllowedContentLength="57671680" />
      </requestFiltering>
    </security>
    <httpErrors existingResponse="PassThrough" />
  </system.webServer>
</configuration>
"@ | Set-Content -Path (Join-Path $root 'web.config') -Encoding utf8

if (-not (Test-Path "IIS:\AppPools\$site")) {
    New-WebAppPool -Name $site | Out-Null
    Set-ItemProperty "IIS:\AppPools\$site" -Name managedRuntimeVersion -Value ''   # no managed code
}
if (-not (Get-Website -Name $site -ErrorAction SilentlyContinue)) {
    New-Website -Name $site -PhysicalPath $root -ApplicationPool $site -HostHeader $e.Hostname -Port 80 | Out-Null
}

# ARR's proxy settings are server-wide only (the section can't be set per site). Buffering off
# is harmless for the other proxied sites (their responses stream instead of being collected
# first), so it's set globally. The proxy timeout is NOT raised: that would change every
# proxied site; MCP clients reconnect if ARR's default (2 min) ever cuts a stream.
$psPath = 'MACHINE/WEBROOT/APPHOST'
Set-WebConfigurationProperty -PSPath $psPath -Filter 'system.webServer/proxy' -Name 'responseBufferLimit' -Value 0
# URL Rewrite may only set server variables that are allowed server-wide (just a permission;
# no other site sets them).
foreach ($var in 'HTTP_X_FORWARDED_HOST', 'HTTP_X_FORWARDED_PROTO', 'HTTP_X_FORWARDED_FOR') {
    $allowed = Get-WebConfiguration -PSPath $psPath -Filter 'system.webServer/rewrite/allowedServerVariables/add' | Where-Object name -eq $var
    if (-not $allowed) { Add-WebConfiguration -PSPath $psPath -Filter 'system.webServer/rewrite/allowedServerVariables' -Value @{ name = $var } }
}
Start-Website -Name $site -ErrorAction SilentlyContinue

Write-Host "IIS site '$site' ($($e.Hostname)) -> http://127.0.0.1:$($e.Port), buffering off, HTTPS redirect: $hasHttps."
if (-not $hasHttps) {
    Write-Host "Next: certificate in Certify the Web (DNS-01) for $($e.Hostname), named 'IIS - $site ($env:COMPUTERNAME)',"
    Write-Host "bound to '$site'; then re-run this script to turn on the HTTP->HTTPS redirect."
}
