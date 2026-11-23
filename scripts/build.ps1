#!/usr/bin/env pwsh
# Build the portable Windows package: dist/halobar/ + dist/halobar-v<version>-win64.zip
param(
    [switch]$SkipTests,
    [switch]$SkipZip
)

$ErrorActionPreference = 'Continue'
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root

function Invoke-Step {
    param([string]$Name, [scriptblock]$Action)
    Write-Host "== $Name ==" -ForegroundColor Cyan
    & $Action
    if ($LASTEXITCODE -ne 0) { throw "$Name failed (exit $LASTEXITCODE)" }
}

$version = (Select-String -Path 'halobar/__init__.py' -Pattern '__version__ = "([^"]+)"').Matches[0].Groups[1].Value
Write-Host "halobar v$version" -ForegroundColor Magenta

if (-not $SkipTests) {
    Invoke-Step 'tests' { python -m pytest -q }
}

Invoke-Step 'pyinstaller' {
    python -m PyInstaller --noconfirm --clean --distpath dist --workpath build packaging/halobar.spec
}

$out = Join-Path $root 'dist/halobar'
Copy-Item 'packaging/README.txt' (Join-Path $out 'README.txt') -Force
Copy-Item 'packaging/create-shortcut.cmd' $out -Force
Copy-Item 'packaging/create-shortcut.ps1' $out -Force
Copy-Item 'LICENSE' (Join-Path $out 'LICENSE.txt') -Force

if (-not $SkipZip) {
    $zip = Join-Path $root "dist/halobar-v$version-win64.zip"
    if (Test-Path $zip) { Remove-Item $zip -Force }
    Compress-Archive -Path (Join-Path $out '*') -DestinationPath $zip -CompressionLevel Optimal
    $size = [math]::Round((Get-Item $zip).Length / 1MB, 1)
    Write-Host "== package ==" -ForegroundColor Green
    Write-Host "  $zip  ($size MB)"
    Get-FileHash $zip -Algorithm SHA256 | ForEach-Object { Write-Host "  SHA256 $($_.Hash)" }
}