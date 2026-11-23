# Publish (or update) a GitHub release with the portable zip.
#
# The release body is built by scripts/publish_release.py and written as UTF-8
# bytes, then sent verbatim: PowerShell's -InFile path re-encoded Chinese text as
# ANSI on this machine and produced mojibake release notes.
param(
    [string]$Tag = 'v0.1.0',
    [string]$Zip = ''
)

$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root

if (-not $Zip) { $Zip = "dist/halobar-$Tag-win64.zip" }
if (-not (Test-Path $Zip)) { throw "missing $Zip (run scripts/build.ps1 first)" }

$jsonFile = Join-Path $env:TEMP "halobar-release-$Tag.json"
python scripts/publish_release.py --tag $Tag --zip $Zip --emit-json $jsonFile
if ($LASTEXITCODE -ne 0) { throw 'failed to build the release body' }
$bodyBytes = [IO.File]::ReadAllBytes($jsonFile)

$cred = "protocol=https`nhost=github.com`n`n" | git credential fill
$token = ($cred | Where-Object { $_ -like 'password=*' }) -replace '^password=', ''
if (-not $token) { throw 'no GitHub credential found via git credential fill' }
$headers = @{
    Authorization = "Bearer $token"
    Accept        = 'application/vnd.github+json'
    'User-Agent'  = 'halobar-release-script'
}

$repoApi = 'https://api.github.com/repos/zhuiye8/halo-tools'
$existing = $null
try {
    $existing = Invoke-RestMethod "$repoApi/releases/tags/$Tag" -Headers $headers -TimeoutSec 30
} catch {
    $existing = $null
}

if ($existing) {
    $release = Invoke-RestMethod "$repoApi/releases/$($existing.id)" -Method Patch `
        -Headers $headers -ContentType 'application/json; charset=utf-8' `
        -Body $bodyBytes -TimeoutSec 60
    Write-Host "updated release: $($release.html_url)"
} else {
    $release = Invoke-RestMethod "$repoApi/releases" -Method Post `
        -Headers $headers -ContentType 'application/json; charset=utf-8' `
        -Body $bodyBytes -TimeoutSec 60
    Write-Host "created release: $($release.html_url)"
}

$assetName = Split-Path $Zip -Leaf
$already = $release.assets | Where-Object { $_.name -eq $assetName }
if ($already) {
    Write-Host "asset already attached: $assetName"
} else {
    $uploadUrl = $release.upload_url -replace '\{.*\}$', ''
    $asset = Invoke-RestMethod "$uploadUrl`?name=$assetName" -Method Post `
        -Headers $headers -ContentType 'application/zip' -InFile $Zip -TimeoutSec 600
    Write-Host ("uploaded: {0} ({1:N1} MB)" -f $asset.browser_download_url, ($asset.size / 1MB))
}

$final = Invoke-RestMethod "$repoApi/releases/tags/$Tag" -Headers $headers -TimeoutSec 30
Write-Host "body starts with: $($final.body.Split("`n")[0])"