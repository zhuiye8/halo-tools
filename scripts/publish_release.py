"""Create or update a GitHub release and (optionally) upload the portable zip.

Everything goes through the REST API with explicit UTF-8 bodies, because
PowerShell's ``Invoke-RestMethod -InFile`` re-encodes the JSON as ANSI on this
machine and produced mojibake release notes.

Usage:
    python scripts/publish_release.py --tag v0.1.0
    python scripts/publish_release.py --tag v0.1.0 --zip dist/halobar-v0.1.0-win64.zip

Credentials are taken from ``git credential fill`` (the same token git uses), so
no token has to be pasted anywhere.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = "zhuiye8/halo-tools"
API = f"https://api.github.com/repos/{REPO}"
ROOT = Path(__file__).resolve().parent.parent

DOWNLOAD_BLOCK = """\
## 下载

解压到任意目录（支持中文路径），双击 `halobar.exe`。不需要装 Python，也不需要管理员权限。

第一次运行时 Windows 可能提示"未知发布者"（没有代码签名），点"更多信息 → 仍要运行"。

## 需要什么

- 花再 Halo PixelBar 音箱，USB 连接电脑，切到声卡输入（红色指示灯）；
- 开箱即用；想要"拖动进度条也不漂"的精确歌词，需要给官网版网易云安装
  BetterNCM + InfLink-rs + 6K Labs，控制台「环境与插件」里有分步指引。

## 已测试范围

花再 Halo PixelBar（VID `2D99` / PID `A160`），固件 1.1.1，Windows 11 x64。
"""


def token() -> str:
    payload = "protocol=https\nhost=github.com\n\n"
    out = subprocess.run(["git", "credential", "fill"], input=payload.encode(),
                         capture_output=True, check=True).stdout.decode()
    for line in out.splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("no GitHub credential found via git credential fill")


def api(method: str, url: str, payload: dict | None = None,
        raw: bytes | None = None, content_type: str = "application/json; charset=utf-8"):
    data = raw if raw is not None else (
        json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None)
    request = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token()}",
        "Accept": "application/vnd.github+json",
        "Content-Type": content_type,
        "User-Agent": "halobar-release-script",
    })
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read()
            return json.loads(body.decode("utf-8")) if body else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise SystemExit(f"GitHub API {exc.code}: {detail}") from exc


def changelog_section(version: str) -> str:
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    marker = f"## v{version}"
    start = text.find(marker)
    if start < 0:
        return f"halobar v{version}"
    end = text.find("\n## ", start + len(marker))
    section = text[start:end if end > 0 else None].strip()
    return section.split("\n", 1)[1].strip() if "\n" in section else section


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", required=True, help="e.g. v0.1.0")
    parser.add_argument("--zip", type=Path, default=None, help="asset to upload")
    parser.add_argument("--notes-file", type=Path, default=None)
    parser.add_argument("--digest", default=None,
                        help="sha256 of the asset already on GitHub (hex, no prefix)")
    parser.add_argument("--emit-json", type=Path, default=None,
                        help="write the release JSON body here instead of calling the API")
    args = parser.parse_args()

    version = args.tag.lstrip("v")
    if args.notes_file:
        body = args.notes_file.read_text(encoding="utf-8")
    else:
        body = changelog_section(version) + "\n\n" + DOWNLOAD_BLOCK

    zip_path = args.zip
    if zip_path is None:
        candidate = ROOT / f"dist/halobar-v{version}-win64.zip"
        zip_path = candidate if candidate.exists() else None

    # The digest must describe the file on GitHub, not a local rebuild: PyInstaller
    # output is not byte-reproducible, so a fresh build has a different hash.
    digest = args.digest
    if digest is None and zip_path is not None and zip_path.exists():
        digest = hashlib.sha256(zip_path.read_bytes()).hexdigest().upper()
    if digest:
        digest = digest.lower().replace("sha256:", "").upper()
        name = zip_path.name if zip_path is not None else f"halobar-v{version}-win64.zip"
        body += f"\n## 校验\n\n```\n{digest}  {name}\n```\n"

    payload = {"tag_name": args.tag, "name": f"halobar {args.tag}",
               "body": body, "draft": False, "prerelease": False}

    if args.emit_json:
        args.emit_json.write_text(json.dumps(payload, ensure_ascii=False),
                                  encoding="utf-8")
        print(f"wrote {args.emit_json} ({len(body)} body chars, digest={digest or '-'})")
        return 0

    try:
        release = api("GET", f"{API}/releases/tags/{args.tag}")
    except SystemExit:
        release = None

    if release:
        release = api("PATCH", f"{API}/releases/{release['id']}",
                      {"name": f"halobar {args.tag}", "body": body})
        print(f"updated release {release['html_url']}")
    else:
        release = api("POST", f"{API}/releases", {
            "tag_name": args.tag, "name": f"halobar {args.tag}",
            "body": body, "draft": False, "prerelease": False,
        })
        print(f"created release {release['html_url']}")

    if zip_path is not None and zip_path.exists():
        existing = {asset["name"] for asset in release.get("assets", [])}
        if zip_path.name in existing:
            print(f"asset already attached: {zip_path.name}")
        else:
            upload_url = release["upload_url"].split("{")[0]
            asset = api("POST", f"{upload_url}?name={zip_path.name}",
                        raw=zip_path.read_bytes(), content_type="application/zip")
            print(f"uploaded {asset['browser_download_url']} "
                  f"({asset['size'] / 1024 / 1024:.1f} MB)")

    final = api("GET", f"{API}/releases/tags/{args.tag}")
    sample = final["body"][:40].replace("\n", " ")
    print(f"body check: {sample!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())