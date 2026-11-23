"""Self-update: check GitHub Releases, download the zip, hand over to a tiny updater.

The app ships as a portable folder (``halobar.exe`` + ``_internal``).  Updating it
from inside the running process is impossible on Windows (the exe is locked), so:

1. download ``halobar-vX.Y.Z-win64.zip`` to ``%LOCALAPPDATA%\\halobar\\updates``,
2. verify the SHA256 that GitHub reports for the asset,
3. extract it to a staging folder next to the download,
4. write a short PowerShell script that waits for this process to exit, mirrors the
   staging folder over the install folder, restarts the exe and deletes itself,
5. exit — the script takes over.

A failure at any step leaves the current installation untouched.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

from . import __version__

log = logging.getLogger(__name__)

API_LATEST = "https://api.github.com/repos/zhuiye8/halo-tools/releases/latest"
USER_AGENT = f"halobar-updater/{__version__} (+https://github.com/zhuiye8/halo-tools)"
PROXY_PORTS = (7890, 7897, 7891, 10809, 10808, 20171)


@dataclass
class Release:
    version: str
    tag: str
    name: str
    notes: str
    html_url: str
    asset_url: str = ""
    asset_name: str = ""
    asset_size: int = 0
    digest: str = ""
    published_at: str = ""

    @property
    def available(self) -> bool:
        return bool(self.asset_url)


class UpdateError(RuntimeError):
    pass


# --------------------------------------------------------------------- versions
def version_tuple(value: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in value.lstrip("v").replace("-", ".").split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


def is_newer(latest: str, current: str) -> bool:
    return version_tuple(latest) > version_tuple(current)


# ------------------------------------------------------------------------ proxy
def detect_proxy() -> str | None:
    """Return an explicit proxy URL from the environment, else probe local ports."""
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        value = os.environ.get(name)
        if value:
            return value if "://" in value else f"http://{value}"
    for port in PROXY_PORTS:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.15):
                return f"http://127.0.0.1:{port}"
        except OSError:
            continue
    return None


def _opener(proxy: str | None):
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener(*handlers)


def _get(url: str, proxy: str | None, timeout: float, headers: dict | None = None) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with _opener(proxy).open(request, timeout=timeout) as response:
        return response.read()


def _curl_available() -> bool:
    return shutil.which("curl") is not None


def _curl_get(url: str, proxy: str | None, timeout: float, destination: Path | None = None) -> bytes:
    """Fetch through the system curl (Schannel trust store, retries, proxy support).

    Python's TLS stack rejects some local proxies (certificate chain not in its
    store); Windows' curl uses the OS store, which is what the browser uses too.
    """
    command = [
        "curl", "-sS", "-L", "--fail", "--retry", "3", "--retry-delay", "1",
        "--connect-timeout", str(int(max(3, timeout))),
        "-A", USER_AGENT, "-H", "Accept: application/vnd.github+json",
    ]
    if proxy:
        command += ["-x", proxy]
    if destination is not None:
        command += ["-o", str(destination), url]
    else:
        command += ["-o", "-", url]
    result = subprocess.run(command, capture_output=True, timeout=timeout + 60,
                            creationflags=0x08000000)
    if result.returncode != 0:
        detail = (result.stderr or b"").decode("utf-8", "replace").strip()
        raise UpdateError(f"curl 失败（{result.returncode}）：{detail[:200]}")
    return result.stdout if destination is None else b""


def _get_with_fallback(url: str, proxy: str | None, timeout: float,
                       destination: Path | None = None) -> bytes:
    errors: list[str] = []
    if _curl_available():
        try:
            return _curl_get(url, proxy, timeout, destination)
        except (UpdateError, subprocess.SubprocessError, OSError) as exc:
            errors.append(str(exc))
    if destination is not None:
        # urllib path for the file download
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with _opener(proxy).open(request, timeout=timeout) as response, \
                destination.open("wb") as handle:
            shutil.copyfileobj(response, handle)
        return b""
    try:
        return _get(url, proxy, timeout, {"Accept": "application/vnd.github+json"})
    except Exception as exc:  # noqa: BLE001
        errors.append(str(exc))
        raise UpdateError("；".join(errors)) from exc


# ---------------------------------------------------------------------- releases
def parse_release(payload: dict) -> Release:
    version = str(payload.get("tag_name") or "").lstrip("v")
    release = Release(
        version=version,
        tag=str(payload.get("tag_name") or ""),
        name=str(payload.get("name") or f"halobar v{version}"),
        notes=str(payload.get("body") or ""),
        html_url=str(payload.get("html_url") or ""),
        published_at=str(payload.get("published_at") or ""),
    )
    for asset in payload.get("assets") or []:
        name = str(asset.get("name") or "")
        if name.startswith("halobar-") and name.endswith(".zip"):
            release.asset_url = str(asset.get("browser_download_url") or "")
            release.asset_name = name
            release.asset_size = int(asset.get("size") or 0)
            release.digest = str(asset.get("digest") or "")
            break
    return release


def resolve_proxy(proxy: str | None) -> str | None:
    if proxy in (None, "", "none", "off"):
        return None
    if proxy == "auto":
        return detect_proxy()
    return proxy


def fetch_latest(proxy: str | None = "auto", timeout: float = 8.0,
                 attempts: int = 2) -> Release:
    """Read the latest release, retrying and falling back to a direct connection.

    The local proxy is convenient but flaky (TLS resets are common), so a failed
    proxied request is retried once and then attempted without a proxy.
    """
    effective = resolve_proxy(proxy)
    candidates: list[str | None] = [effective]
    if effective is not None:
        candidates.append(None)

    last_error: Exception | None = None
    for candidate in candidates:
        for attempt in range(attempts):
            try:
                raw = _get_with_fallback(API_LATEST, candidate, timeout)
                payload = json.loads(raw.decode("utf-8"))
                return parse_release(payload)
            except UpdateError as exc:
                last_error = exc
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                last_error = UpdateError(f"网络不可用：{exc}")
            time.sleep(0.4 * (attempt + 1))
    raise last_error or UpdateError("检查更新失败")


def check(current: str = __version__, proxy: str | None = "auto") -> Release | None:
    """Return the latest release when it is newer than ``current``."""
    release = fetch_latest(proxy=proxy)
    return release if is_newer(release.version, current) else None


# ---------------------------------------------------------------------- download
def download(release: Release, destination: Path, proxy: str | None = "auto",
             progress=None, timeout: float = 60.0) -> Path:
    if not release.available:
        raise UpdateError("这个版本没有可下载的 zip")
    destination.parent.mkdir(parents=True, exist_ok=True)
    effective = resolve_proxy(proxy)

    if _curl_available():
        total = release.asset_size or 0
        last_detail = ""
        for candidate in ([effective, None] if effective else [None]):
            for attempt in range(2):
                command = [
                    "curl", "-sS", "-L", "--fail", "--retry", "2", "--retry-delay", "1",
                    "--connect-timeout", "20", "-A", USER_AGENT,
                    "-o", str(destination), release.asset_url,
                ]
                if candidate:
                    command[1:1] = ["-x", candidate]
                process = subprocess.Popen(command, creationflags=0x08000000,
                                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                while process.poll() is None:
                    if progress and total > 0:
                        try:
                            progress(min(0.99, destination.stat().st_size / total))
                        except OSError:
                            pass
                    time.sleep(0.2)
                if process.returncode == 0:
                    last_detail = ""
                    break
                detail = (process.stderr.read() if process.stderr else b"").decode("utf-8", "replace")
                last_detail = detail.strip()[:200]
                log.warning("curl download attempt failed: %s", last_detail)
                time.sleep(0.5)
            if not last_detail:
                break
        if last_detail:
            raise UpdateError(f"下载失败：{last_detail}")
    else:
        request = urllib.request.Request(release.asset_url, headers={"User-Agent": USER_AGENT})
        received = 0
        total = release.asset_size or 0
        with _opener(effective).open(request, timeout=timeout) as response, \
                destination.open("wb") as handle:
            while True:
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
                received += len(chunk)
                if progress and total:
                    progress(min(0.99, received / total))

    digest = hashlib.sha256()
    with destination.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    actual = digest.hexdigest()
    expected = release.digest.replace("sha256:", "").lower() if release.digest else ""
    if expected and actual != expected:
        destination.unlink(missing_ok=True)
        raise UpdateError("下载文件校验失败（SHA256 不一致），已删除")
    if progress:
        progress(1.0)
    log.info("downloaded %s (%d bytes, sha256 %s)", release.asset_name,
             destination.stat().st_size, actual[:12])
    return destination


# ------------------------------------------------------------------------ apply
def install_dir() -> Path:
    return Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else \
        Path(__file__).resolve().parent.parent


def extract(zip_path: Path, staging: Path) -> None:
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    with zipfile.ZipFile(zip_path) as archive:
        archive.extractall(staging)
    if not (staging / "halobar.exe").exists():
        raise UpdateError("更新包里找不到 halobar.exe")


UPDATER_SCRIPT = r"""param([int]$ParentPid, [string]$Source, [string]$Target, [string]$Exe)
$ErrorActionPreference = 'Stop'
try {
    Wait-Process -Id $ParentPid -Timeout 60 -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 600
    robocopy $Source $Target /E /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "robocopy failed ($LASTEXITCODE)" }
    Start-Process -FilePath $Exe -WorkingDirectory $Target
    Remove-Item -LiteralPath $Source -Recurse -Force -ErrorAction SilentlyContinue
} catch {
    $_ | Out-File -FilePath (Join-Path $env:TEMP 'halobar-update-error.log') -Append
} finally {
    Remove-Item -LiteralPath $MyInvocation.MyCommand.Path -Force -ErrorAction SilentlyContinue
}
"""


def launch_updater(staging: Path, target: Path | None = None) -> Path:
    """Start the hand-off script; the caller should exit right after."""
    target = target or install_dir()
    exe = target / "halobar.exe"
    if not exe.exists():
        raise UpdateError("当前不是打包运行，无法自动更新")
    probe = target / ".halobar-write-test"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise UpdateError(f"安装目录不可写，请手动更新：{exc}") from exc

    script = Path(tempfile.gettempdir()) / f"halobar-update-{os.getpid()}.ps1"
    script.write_text(UPDATER_SCRIPT, encoding="utf-8-sig")
    subprocess.Popen(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
         "-ParentPid", str(os.getpid()), "-Source", str(staging),
         "-Target", str(target), "-Exe", str(exe)],
        creationflags=0x08000000, close_fds=True,
    )
    log.info("updater started: %s -> %s", staging, target)
    return script