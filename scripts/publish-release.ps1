# Create the v0.1.0 GitHub release and attach the portable zip (no Actions needed).
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)

$cred = "protocol=https`nhost=github.com`n`n" | git credential fill
$token = ($cred | Where-Object { $_ -like 'password=*' }) -replace '^password=', ''
if (-not $token) { throw 'no GitHub credential found' }
$headers = @{
    Authorization = "Bearer $token"
    Accept        = 'application/vnd.github+json'
    'User-Agent'  = 'halobar-release-script'
}

$notes = @"
首个可用版本。

**这会做什么**

- 把正在播放的歌词送到花再 Halo PixelBar 的 256×32 像素屏
- 支持长句滚动、切片淡入、间奏处理、暂停待机、退出恢复
- 控制台（127.0.0.1）里可以微调、改对齐/动画、上传自定义像素屏
- 网易云走 6K Labs 精确进度时，拖动进度条也不会漂

**下载后**

解压到任意目录，双击 ``halobar.exe``。不需要安装 Python，也不需要管理员权限。
程序不会开机自启；桌面快捷方式可以在控制台里一键创建。

**首次运行提示"未知发布者"** 是因为没有代码签名，点"更多信息 → 仍要运行"。

**想要精准进度**：需要给官网版网易云安装 BetterNCM + InfLink-rs + 6K Labs，
控制台顶部的「环境与插件」里有分步指引。

**已测试范围**：花再 Halo PixelBar（VID 2D99 / PID A160），固件 1.1.1，Windows 11 x64。

SHA256: F94ECC917396EE22DAC14426A599DCF2CFE09B02A9FDA37F40EF85716A5CFB9A
"@

$payload = @{
    tag_name   = 'v0.1.0'
    name       = 'halobar v0.1.0'
    body       = $notes
    draft      = $false
    prerelease = $false
} | ConvertTo-Json -Depth 4

$bodyFile = Join-Path $env:TEMP 'halobar-release.json'
[IO.File]::WriteAllText($bodyFile, $payload, [Text.UTF8Encoding]::new($false))

Write-Host 'creating release...'
$release = Invoke-RestMethod 'https://api.github.com/repos/zhuiye8/halo-tools/releases' `
    -Method Post -Headers $headers -ContentType 'application/json; charset=utf-8' `
    -InFile $bodyFile -TimeoutSec 60
Write-Host "release created: $($release.html_url)"

$uploadUrl = ($release.upload_url -replace '\{.*\}$', '')
$zip = Join-Path (Get-Location) 'dist/halobar-v0.1.0-win64.zip'
if (-not (Test-Path $zip)) { throw "missing $zip" }

Write-Host 'uploading asset...'
$asset = Invoke-RestMethod "$uploadUrl`?name=halobar-v0.1.0-win64.zip" `
    -Method Post -Headers $headers -ContentType 'application/zip' `
    -InFile $zip -TimeoutSec 600
Write-Host "asset uploaded: $($asset.browser_download_url)  ($([math]::Round($asset.size / 1MB, 1)) MB)"