# 发布流程

这个仓库不使用 GitHub Actions 发布（账号的 Actions 因为账单问题被锁，工作流已在仓库设置里手动禁用）。
发布在本地完成，一条命令打出安装包并上传到 GitHub Release。

## 一次性准备

- Python 3.12，装好项目依赖：`python -m pip install -e ".[media,dev]" pyinstaller`
- Git 凭据可用（`git credential fill` 能取到 token），`publish-release.ps1` 用它调 GitHub API
- 能正常访问 `github.com` 和 `api.github.com`

## 每次发版的步骤

**1. 改版本号**

版本号只有一个来源：`halobar/__init__.py` 里的 `__version__`。

```
halobar/__init__.py     __version__ = "0.2.0"
pyproject.toml          version = "0.2.0"
CHANGELOG.md            新增 "## v0.2.0（日期）" 小节
```

CHANGELOG 的这一节会被自动抓取，作为 Release 说明的正文，所以按人话写清楚改了什么。

**2. 提交并打 tag**

```powershell
git add -A
git commit -m "release: v0.2.0"
git tag -a v0.2.0 -m "halobar v0.2.0"
git push origin main
git push origin v0.2.0
```

**3. 一键发布**

```powershell
./scripts/release.ps1
```

脚本会依次做：

1. 校验 `__version__` 与 `-Version` 参数一致；
2. 跑 `pytest`（51 个测试）和 `scripts/selfcheck.py`；
3. 用 PyInstaller 打包成 `dist/halobar/`；
4. 冒烟测试：用打包出来的 `halobar.exe` 跑 6 秒，退出码必须是 0；
5. 生成 `dist/halobar-v0.2.0-win64.zip`，打印大小与 SHA256；
6. 若 tag 不存在则创建；
7. 调 GitHub API 创建/更新 Release，上传 zip，自动附上 SHA256。

常用参数：

```powershell
./scripts/release.ps1 -DryRun      # 只构建和冒烟测试，不发布
./scripts/release.ps1 -SkipTests   # 跳过测试（不建议）
```

## 发布后自检

- 打开 Release 页面，确认说明是正常中文、附件大小约 14 MB；
- 下载 zip 解压，双击 `halobar.exe`，确认托盘、控制台、设备连接正常；
- 在控制台点一次「创建桌面快捷方式」，确认落在桌面上；
- 确认 `gh release` 里的 SHA256 与本地 `Get-FileHash` 一致。

## 只更新 Release 说明

改了 CHANGELOG 想同步到已发布的版本，不需要重新打包：

```powershell
./scripts/publish-release.ps1 -Tag v0.2.0 -Zip dist/halobar-v0.2.0-win64.zip
```

脚本检测到资产已存在会跳过上传，只更新说明。

## 说明

- 这些脚本发送 Release 正文时用的是**原始 UTF-8 字节**。早先版本用 `Invoke-RestMethod -InFile` 直接发文件，PowerShell 会按 ANSI 重编码，导致 Release 中文变乱码，别改回去。
- 如果之后 GitHub 账单恢复、想重新启用自动构建：仓库 Settings → Actions → General → 重新启用工作流，或者

  ```powershell
  gh api -X PUT repos/zhuiye8/halo-tools/actions/workflows/427470697/enable
  gh api -X PUT repos/zhuiye8/halo-tools/actions/workflows/427485146/enable
  ```

  工作流文件（`.github/workflows/ci.yml`、`release.yml`）一直保留着，没有删除。届时打 tag 会自动构建并发布，`scripts/release.ps1` 可以留着当本地兜底。