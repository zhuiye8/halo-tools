# Publish (or update) a GitHub release with the portable zip.
#
# Two details learned the hard way:
#   * the release body is written as UTF-8 bytes and sent verbatim, because
#     PowerShell's -InFile re-encodes it as ANSI and produces mojibake;
#   * the SHA256 in the notes must describe the asset that is actually on GitHub.
#     PyInstaller output is not byte-reproducible, so a local rebuild has a
#     different hash; we therefore read the asset digest from the API.
param(
    [string]$Tag = 'v0.1.0',
    [string]$Zip = '',
    [switch]$ReplaceAsset
)

$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root

if (-not $Zip) { $Zip = "dist/halobar-$Tag-win64.zip" }
if (-not (Test-Path $Zip)) { throw "missing $Zip (run scripts/build.ps1 first)" }
$assetName = Split-Path $Zip -Leaf

$cred = "protocol=https`nhost=github.com`n`n" | git credential fill
$token = ($cred | Where-Object { $_ -like 'password=*' }) -replace '^password=', ''
if (-not $token) { throw 'no GitHub credential found via git credential fill' }
$headers = @{
    Authorization = "Bearer $token"
    Accept        = 'application/vnd.github+json'
    'User-Agent'  = 'halobar-release-script'
}
$repoApi = 'https://api.github.com/repos/zhuiye8/halo-tools'

function Send-Json {
    param([string]$Uri, [string]$Method)
    $jsonFile = Join-Path $env:TEMP "halobar-release-$Tag.json"
    python scripts/publish_release.py --tag $Tag --zip $Zip --emit-json $jsonFile "--digest=$digest"
    if ($LASTEXITCODE -ne 0) { throw 'failed to build the release body' }
    $bytes = [IO.File]::ReadAllBytes($jsonFile)
    return Invoke-RestMethod $Uri -Method $Method -Headers $headers `
        -ContentType 'application/json; charset=utf-8' -Body $bytes -TimeoutSec 60
}

$release = $null
try { $release = Invoke-RestMethod "$repoApi/releases/tags/$Tag" -Headers $headers -TimeoutSec 30 }
catch { $release = $null }

if (-not $release) {
    $digest = ''
    $release = Send-Json "$repoApi/releases" 'Post'
    Write-Host "created release: $($release.html_url)"
}

$asset = $release.assets | Where-Object { $_.name -eq $assetName } | Select-Object -First 1
if ($asset -and $ReplaceAsset) {
    Write-Host "deleting existing asset $assetName ..."
    Invoke-RestMethod "$repoApi/releases/assets/$($asset.id)" -Method Delete -Headers $headers -TimeoutSec 30 | Out-Null
    $asset = $null
}
if (-not $asset) {
    $uploadUrl = $release.upload_url -replace '\{.*\}$', ''
    Write-Host 'uploading asset...'
    $asset = Invoke-RestMethod "$uploadUrl`?name=$assetName" -Method Post `
        -Headers $headers -ContentType 'application/zip' -InFile $Zip -TimeoutSec 600
    Write-Host ("uploaded: {0} ({1:N1} MB)" -f $asset.browser_download_url, ($asset.size / 1MB))
} else {
    Write-Host "asset already attached: $assetName"
}

# Prefer GitHub's own digest; fall back to the local file when it is missing.
$digest = ''
if ($asset.digest) {
    $digest = ($asset.digest -replace '^sha256:', '').ToUpper()
    Write-Host "asset sha256: $digest"
} else {
    $digest = (Get-FileHash $Zip -Algorithm SHA256).Hash
    Write-Host "asset digest unavailable; using local hash: $digest"
}

$release = Send-Json "$repoApi/releases/$($release.id)" 'Patch'
Write-Host "updated release: $($release.html_url)"
Write-Host "body starts with: $($release.body.Split("`n")[0])"