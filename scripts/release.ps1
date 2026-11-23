# Build one release locally and publish it to GitHub.
#
#   ./scripts/release.ps1              # tests + build + smoke test + publish
#   ./scripts/release.ps1 -DryRun      # everything except publishing
#   ./scripts/release.ps1 -Version 0.2.0
#
# GitHub Actions is disabled for this repository (account billing lock), so this
# script is the release path. See docs/RELEASING.md.
param(
    [string]$Version = '',
    [switch]$DryRun,
    [switch]$SkipTests
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

$sourceVersion = (Select-String -Path 'halobar/__init__.py' -Pattern '__version__ = "([^"]+)"').Matches[0].Groups[1].Value
if ($Version -and $Version -ne $sourceVersion) {
    throw "halobar/__init__.py says $sourceVersion, but -Version $Version was requested"
}
$Version = $sourceVersion
$Tag = "v$Version"
Write-Host "halobar $Tag" -ForegroundColor Magenta

if (-not $SkipTests) {
    Invoke-Step 'tests' { python -m pytest -q }
    Invoke-Step 'selfcheck' { python scripts/selfcheck.py }
}

Invoke-Step 'build' { ./scripts/build.ps1 -SkipTests -Version $Version }

Write-Host '== smoke test ==' -ForegroundColor Cyan
$exe = Join-Path $root 'dist/halobar/halobar.exe'
if (-not (Test-Path $exe)) { throw "missing $exe" }
$process = Start-Process -FilePath $exe -ArgumentList 'run', '--no-tray', '--seconds', '6' -PassThru -Wait
if ($process.ExitCode -ne 0) { throw "packaged exe exited with $($process.ExitCode)" }
Write-Host '   packaged exe started and exited cleanly'

$zip = Join-Path $root "dist/halobar-$Tag-win64.zip"
if (-not (Test-Path $zip)) { throw "missing $zip" }
$hash = (Get-FileHash $zip -Algorithm SHA256).Hash
Write-Host "   $zip ($([math]::Round((Get-Item $zip).Length / 1MB, 1)) MB)"
Write-Host "   SHA256 $hash"

$tagExists = git rev-parse -q --verify "refs/tags/$Tag" 2>$null
if (-not $tagExists) {
    Write-Host "== tag ==" -ForegroundColor Cyan
    git tag -a $Tag -m "halobar $Tag"
    Write-Host "   created $Tag; remember to push it: git push origin $Tag"
}

if ($DryRun) {
    Write-Host '== dry run: skipping publish ==' -ForegroundColor Yellow
    return
}

Write-Host '== publish ==' -ForegroundColor Cyan
./scripts/publish-release.ps1 -Tag $Tag -Zip $zip
if ($LASTEXITCODE -ne 0) { throw 'publish failed' }

Write-Host ''
Write-Host "Release: https://github.com/zhuiye8/halo-tools/releases/tag/$Tag" -ForegroundColor Green
Write-Host "Remember to push the tag if you just created it."