$ErrorActionPreference = 'Stop'
$exe = Join-Path $PSScriptRoot 'halobar.exe'
if (-not (Test-Path $exe)) {
    Write-Host "halobar.exe not found next to this script."
    exit 1
}
$ws = New-Object -ComObject WScript.Shell
$lnk = Join-Path ([Environment]::GetFolderPath('Desktop')) 'halobar.lnk'
$sc = $ws.CreateShortcut($lnk)
$sc.TargetPath = $exe
$sc.WorkingDirectory = $PSScriptRoot
$sc.IconLocation = "$exe,0"
$sc.Description = 'halobar - PixelBar lyrics'
$sc.Save()
Write-Host "Desktop shortcut created: $lnk"