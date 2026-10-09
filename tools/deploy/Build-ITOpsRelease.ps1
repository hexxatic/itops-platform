#requires -Version 7.3
<#
.SYNOPSIS
    Build a self-contained IT-Ops release: one zip that installs with no internet and no
    preinstalled Python (design.md section 7).

.DESCRIPTION
    Runs on Windows x64 (a GitHub-hosted windows runner in CI, or a dev box). The zip holds:

      python\      a portable CPython (uv-managed, python-build-standalone) with IT-Ops, every
                   module in the workspace and every pinned dependency from uv.lock installed
      models\      the embedding model files, so semantic search works offline
      service\     WinSW (the Windows service wrapper), checksum-pinned
      release.json version, commit, build time

    Start it with `python\python.exe -I -m itops serve`. Always -I (isolated): without it,
    Python also loads packages from the user's profile and honours PYTHON* variables, so
    whatever else is on the server could leak in. Nothing in the zip points at the build
    machine: no console-script launchers, no virtualenv.

.PARAMETER Version
    Release name, e.g. v0.3.0 (tags) or 20261002-1530-ab12cd3 (main). Default: date + commit.
#>
[CmdletBinding()]
param(
    [string] $Version,
    [string] $OutDir = 'dist',
    [string] $PythonVersion = '3.14'
)

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
Set-Location $repo

$commit = (git rev-parse --short HEAD).Trim()
if (-not $Version) { $Version = '{0}-{1}' -f (Get-Date -Format 'yyyyMMdd-HHmm'), $commit }
if ($Version -notmatch '^[A-Za-z0-9._-]+$') { throw "Version '$Version' must be a plain folder name." }

$WinSwUrl    = 'https://github.com/winsw/winsw/releases/download/v2.12.0/WinSW-x64.exe'
$WinSwSha256 = '05b82d46ad331cc16bdc00de5c6332c1ef818df8ceefcd49c726553209b3a0da'

# A fresh working folder per build: antivirus can hold freshly extracted .pyd files for a
# while, so an old folder isn't always deletable yet. Old ones are cleaned up best-effort.
Get-ChildItem (Join-Path $repo $OutDir) -Directory -Filter 'build-*' -ErrorAction SilentlyContinue |
    ForEach-Object { Remove-Item $_.FullName -Recurse -Force -ErrorAction SilentlyContinue }
$work  = Join-Path $repo ("$OutDir\build-{0}" -f [guid]::NewGuid().ToString('N').Substring(0, 8))
$stage = Join-Path $work "itops-$Version"
New-Item -ItemType Directory -Force -Path $stage, "$work\wheels" | Out-Null

Write-Host "== Python $PythonVersion (portable) =="
uv python install $PythonVersion
$basePy = (uv python find $PythonVersion --managed-python).Trim()
Copy-Item (Split-Path $basePy) "$stage\python" -Recurse
$py = "$stage\python\python.exe"
& $py --version

Write-Host "== Dependencies from uv.lock (hash-checked) =="
uv export --frozen --no-dev --all-packages --no-emit-workspace --format requirements-txt -o "$work\requirements.txt" | Out-Null
uv pip install --python $py --break-system-packages --require-hashes --no-deps -r "$work\requirements.txt"

Write-Host "== IT-Ops core + modules (workspace wheels) =="
uv build --all-packages --wheel -o "$work\wheels"
uv pip install --python $py --break-system-packages --no-deps (Get-ChildItem "$work\wheels\*.whl").FullName

Write-Host "== Embedding model (offline search) =="
$env:ITOPS_BUILD_MODELS = "$stage\models"
& $py -I -c @'
import os
from fastembed import TextEmbedding
from itops.core.config import Settings
model = Settings(_env_file=None).ai_embeddings_model
TextEmbedding(model, cache_dir=os.environ["ITOPS_BUILD_MODELS"])
print("cached", model)
'@

Write-Host "== Service wrapper (WinSW, pinned) =="
New-Item -ItemType Directory -Force -Path "$stage\service" | Out-Null
Invoke-WebRequest $WinSwUrl -OutFile "$stage\service\WinSW-x64.exe"
$hash = (Get-FileHash "$stage\service\WinSW-x64.exe" -Algorithm SHA256).Hash.ToLower()
if ($hash -ne $WinSwSha256) { throw "WinSW checksum mismatch: got $hash" }

Write-Host "== Smoke test =="
& $py -I -m itops modules check
& $py -I -c "import itops.core.app; print('app imports')"

@{ version = $Version; commit = $commit; built = (Get-Date).ToUniversalTime().ToString('o'); python = (& $py -I -c "import sys; print(sys.version.split()[0])").Trim() } |
    ConvertTo-Json | Set-Content "$stage\release.json" -Encoding utf8

# Precompile everything once. The service account can't write into the release folder, so
# without this every start would recompile in memory. unchecked-hash: the .pyc files stay
# valid after unzipping (timestamps don't matter).
Get-ChildItem $stage -Recurse -Directory -Filter __pycache__ | Remove-Item -Recurse -Force
# A few third-party test fixtures are deliberately invalid Python; skipping those is fine.
$PSNativeCommandUseErrorActionPreference = $false
& $py -I -m compileall -q -j 0 --invalidation-mode unchecked-hash "$stage\python\Lib" | Out-Null
$PSNativeCommandUseErrorActionPreference = $true

$zip = Join-Path $repo "$OutDir\itops-$Version.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path "$stage\*" -DestinationPath $zip -CompressionLevel Optimal
Write-Host ("Built {0} ({1:N0} MB)" -f $zip, ((Get-Item $zip).Length / 1MB))
if ($env:GITHUB_OUTPUT) { "zip=$zip" >> $env:GITHUB_OUTPUT; "version=$Version" >> $env:GITHUB_OUTPUT }
