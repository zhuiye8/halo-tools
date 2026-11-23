"""Press the SMTC reader in a tight loop and monitor BOTH local and system-wide impact.

Tracks:
* Current process: RSS, Private Bytes, Paged/NonPaged quota, Python threads,
  native OS threads (catches WinRT/RPC thread leaks), handle count, and
  ``halobar.media`` internal SMTC call/event/timeout counters.
* Windows kernel pools: system ``KernelPaged`` (PoolPagedBytes),
  ``KernelNonpaged`` (PoolNonpagedBytes), total handles & threads, plus
  optional PoolTag breakdown (``AlMs``, ``Icp ``, ``Obtb``) via
  ``NtQuerySystemInformation(SystemPoolTagInformation)``.
* External processes implicated in SMTC/ALPC backpressure:
  - ``NPSMSvc`` (Per-User SMTC service ``NPSMSvc_<LUID>``): memory, CPU seconds,
    OS threads, handles;
  - ``cloudmusic.exe`` (NetEase Cloud Music): process count, total OS threads,
    working set, handles.

Usage::

    python probe/smtc_leak_probe.py --iterations 600 --interval 0.1
    python probe/smtc_leak_probe.py --duration 120 --interval 0.1 --pooltags --csv probe.csv
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import ctypes
import ctypes.wintypes as wt
import os
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from halobar import media  # noqa: E402

# ---------------------------------------------------------------- Win32 ctypes
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_psapi = ctypes.WinDLL("psapi", use_last_error=True)
_advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
_ntdll = ctypes.WinDLL("ntdll", use_last_error=True)

_TH32CS_SNAPPROCESS = 0x00000002
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SC_MANAGER_ENUMERATE_SERVICE = 0x0004
_SC_ENUM_PROCESS_INFO = 0
_SERVICE_WIN32 = 0x00000030
_SERVICE_ACTIVE = 0x00000001
_ERROR_MORE_DATA = 234
_SYSTEM_POOLTAG_INFORMATION = 22
_STATUS_INFO_LENGTH_MISMATCH = 0xC0000004


class MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
    ]


class PERFORMANCE_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("cb", wt.DWORD),
        ("CommitTotal", ctypes.c_size_t),
        ("CommitLimit", ctypes.c_size_t),
        ("CommitPeak", ctypes.c_size_t),
        ("PhysicalTotal", ctypes.c_size_t),
        ("PhysicalAvailable", ctypes.c_size_t),
        ("SystemCache", ctypes.c_size_t),
        ("KernelTotal", ctypes.c_size_t),
        ("KernelPaged", ctypes.c_size_t),
        ("KernelNonpaged", ctypes.c_size_t),
        ("PageSize", ctypes.c_size_t),
        ("HandleCount", wt.DWORD),
        ("ProcessCount", wt.DWORD),
        ("ThreadCount", wt.DWORD),
    ]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("cntUsage", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD),
        ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wt.DWORD),
        ("szExeFile", wt.WCHAR * 260),
    ]


class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wt.DWORD), ("dwHighDateTime", wt.DWORD)]


class SERVICE_STATUS_PROCESS(ctypes.Structure):
    _fields_ = [
        ("dwServiceType", wt.DWORD),
        ("dwCurrentState", wt.DWORD),
        ("dwControlsAccepted", wt.DWORD),
        ("dwWin32ExitCode", wt.DWORD),
        ("dwServiceSpecificExitCode", wt.DWORD),
        ("dwCheckPoint", wt.DWORD),
        ("dwWaitHint", wt.DWORD),
        ("dwProcessId", wt.DWORD),
        ("dwServiceFlags", wt.DWORD),
    ]


class ENUM_SERVICE_STATUS_PROCESSW(ctypes.Structure):
    _fields_ = [
        ("lpServiceName", wt.LPWSTR),
        ("lpDisplayName", wt.LPWSTR),
        ("ServiceStatusProcess", SERVICE_STATUS_PROCESS),
    ]


class SYSTEM_POOLTAG(ctypes.Structure):
    _fields_ = [
        ("Tag", ctypes.c_char * 4),
        ("PagedAllocs", wt.ULONG),
        ("PagedFrees", wt.ULONG),
        ("PagedUsed", ctypes.c_size_t),
        ("NonPagedAllocs", wt.ULONG),
        ("NonPagedFrees", wt.ULONG),
        ("NonPagedUsed", ctypes.c_size_t),
    ]


_kernel32.GetCurrentProcess.restype = wt.HANDLE
_kernel32.OpenProcess.restype = wt.HANDLE
_kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
_kernel32.CloseHandle.restype = wt.BOOL
_kernel32.CloseHandle.argtypes = [wt.HANDLE]
_kernel32.GetProcessHandleCount.restype = wt.BOOL
_kernel32.GetProcessHandleCount.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
_kernel32.GetProcessTimes.restype = wt.BOOL
_kernel32.GetProcessTimes.argtypes = [
    wt.HANDLE, ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME),
    ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME),
]
_kernel32.CreateToolhelp32Snapshot.restype = wt.HANDLE
_kernel32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
_kernel32.Process32FirstW.restype = wt.BOOL
_kernel32.Process32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
_kernel32.Process32NextW.restype = wt.BOOL
_kernel32.Process32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]

_psapi.GetProcessMemoryInfo.restype = wt.BOOL
_psapi.GetProcessMemoryInfo.argtypes = [wt.HANDLE, ctypes.POINTER(MEMORY_COUNTERS), wt.DWORD]
_psapi.GetPerformanceInfo.restype = wt.BOOL
_psapi.GetPerformanceInfo.argtypes = [ctypes.POINTER(PERFORMANCE_INFORMATION), wt.DWORD]

_advapi32.OpenSCManagerW.restype = wt.HANDLE
_advapi32.OpenSCManagerW.argtypes = [wt.LPCWSTR, wt.LPCWSTR, wt.DWORD]
_advapi32.CloseServiceHandle.restype = wt.BOOL
_advapi32.CloseServiceHandle.argtypes = [wt.HANDLE]
_advapi32.EnumServicesStatusExW.restype = wt.BOOL
_advapi32.EnumServicesStatusExW.argtypes = [
    wt.HANDLE, wt.DWORD, wt.DWORD, wt.DWORD, ctypes.c_void_p, wt.DWORD,
    ctypes.POINTER(wt.DWORD), ctypes.POINTER(wt.DWORD), ctypes.POINTER(wt.DWORD),
    wt.LPCWSTR,
]

_ntdll.NtQuerySystemInformation.restype = wt.ULONG
_ntdll.NtQuerySystemInformation.argtypes = [
    wt.ULONG, ctypes.c_void_p, wt.ULONG, ctypes.POINTER(wt.ULONG),
]


def _ft_to_seconds(ft: FILETIME) -> float:
    raw = (ft.dwHighDateTime << 32) | ft.dwLowDateTime
    return raw / 10_000_000.0


def _snapshot_processes() -> dict[int, tuple[str, int]]:
    """Return ``{pid: (exe_name_lower, os_thread_count)}``."""
    out: dict[int, tuple[str, int]] = {}
    snap = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if snap == wt.HANDLE(-1).value or not snap:
        return out
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = _kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            out[int(entry.th32ProcessID)] = (str(entry.szExeFile).lower(), int(entry.cntThreads))
            ok = _kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        _kernel32.CloseHandle(snap)
    return out


def _inspect_pid(pid: int) -> tuple[float, float, float, int]:
    """Return ``(rss_mb, private_mb, cpu_seconds, handles)`` for ``pid``."""
    if pid == os.getpid():
        handle = _kernel32.GetCurrentProcess()
        close_after = False
    else:
        handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return 0.0, 0.0, 0.0, 0
        close_after = True
    try:
        mem = MEMORY_COUNTERS()
        mem.cb = ctypes.sizeof(mem)
        rss = private = 0.0
        if _psapi.GetProcessMemoryInfo(handle, ctypes.byref(mem), mem.cb):
            rss = mem.WorkingSetSize / (1024 * 1024)
            private = mem.PagefileUsage / (1024 * 1024)

        creation, exit_t, kernel_t, user_t = FILETIME(), FILETIME(), FILETIME(), FILETIME()
        cpu_s = 0.0
        if _kernel32.GetProcessTimes(
            handle, ctypes.byref(creation), ctypes.byref(exit_t),
            ctypes.byref(kernel_t), ctypes.byref(user_t),
        ):
            cpu_s = _ft_to_seconds(kernel_t) + _ft_to_seconds(user_t)

        handles = wt.DWORD(0)
        hcount = int(handles.value) if _kernel32.GetProcessHandleCount(handle, ctypes.byref(handles)) else 0
        return rss, private, cpu_s, hcount
    finally:
        if close_after:
            _kernel32.CloseHandle(handle)


_SCM_BUF: ctypes.Array | None = None
_POOLTAG_BUF: ctypes.Array | None = None


def _find_service_pids(prefix: str = "npsmsvc") -> list[int]:
    """Locate active Windows service PIDs matching ``prefix`` (handles per-user LUID suffixes)."""
    global _SCM_BUF
    scm = _advapi32.OpenSCManagerW(None, None, _SC_MANAGER_ENUMERATE_SERVICE)
    if not scm:
        return []
    try:
        needed = wt.DWORD(0)
        count = wt.DWORD(0)
        resume = wt.DWORD(0)
        _advapi32.EnumServicesStatusExW(
            scm, _SC_ENUM_PROCESS_INFO, _SERVICE_WIN32, _SERVICE_ACTIVE,
            None, 0, ctypes.byref(needed), ctypes.byref(count), ctypes.byref(resume), None,
        )
        if needed.value == 0:
            return []
        if _SCM_BUF is None or len(_SCM_BUF) < needed.value:
            _SCM_BUF = ctypes.create_string_buffer(max(needed.value + 4096, 65536))
        resume = wt.DWORD(0)
        if not _advapi32.EnumServicesStatusExW(
            scm, _SC_ENUM_PROCESS_INFO, _SERVICE_WIN32, _SERVICE_ACTIVE,
            _SCM_BUF, len(_SCM_BUF), ctypes.byref(needed), ctypes.byref(count), ctypes.byref(resume), None,
        ):
            return []
        base_addr = ctypes.addressof(_SCM_BUF)
        stride = ctypes.sizeof(ENUM_SERVICE_STATUS_PROCESSW)
        pids: set[int] = set()
        target = prefix.lower()
        for idx in range(count.value):
            entry = ENUM_SERVICE_STATUS_PROCESSW.from_address(base_addr + idx * stride)
            name = (entry.lpServiceName or "").lower()
            pid = int(entry.ServiceStatusProcess.dwProcessId)
            if name.startswith(target) and pid > 0:
                pids.add(pid)
        return sorted(pids)
    finally:
        _advapi32.CloseServiceHandle(scm)


def _kernel_perf() -> tuple[float, float, int, int]:
    """Return ``(pool_paged_mb, pool_nonpaged_mb, sys_handles, sys_threads)``."""
    info = PERFORMANCE_INFORMATION()
    info.cb = ctypes.sizeof(info)
    if not _psapi.GetPerformanceInfo(ctypes.byref(info), info.cb):
        return 0.0, 0.0, 0, 0
    page = info.PageSize
    paged_mb = info.KernelPaged * page / (1024 * 1024)
    nonpaged_mb = info.KernelNonpaged * page / (1024 * 1024)
    return paged_mb, nonpaged_mb, int(info.HandleCount), int(info.ThreadCount)


def _query_pool_tags(tags: tuple[bytes, ...] = (b"AlMs", b"Icp ", b"Obtb")) -> dict[str, tuple[int, float]] | None:
    """Best-effort query of kernel pool tags via NtQuerySystemInformation(22).

    Reuses a single buffer and avoids ``ctypes.cast`` to prevent reference cycles
    from inflating the probe's own working set over long runs.
    """
    global _POOLTAG_BUF
    if _POOLTAG_BUF is None:
        _POOLTAG_BUF = ctypes.create_string_buffer(256 * 1024)
    for _ in range(6):
        ret = wt.ULONG(0)
        status = _ntdll.NtQuerySystemInformation(
            _SYSTEM_POOLTAG_INFORMATION, _POOLTAG_BUF, len(_POOLTAG_BUF), ctypes.byref(ret)
        )
        if status == _STATUS_INFO_LENGTH_MISMATCH:
            new_size = max(len(_POOLTAG_BUF) * 2, int(ret.value) + 16384)
            _POOLTAG_BUF = ctypes.create_string_buffer(new_size)
            continue
        if status != 0:
            return None
        count = wt.ULONG.from_buffer(_POOLTAG_BUF, 0).value
        offset = ctypes.alignment(SYSTEM_POOLTAG)
        stride = ctypes.sizeof(SYSTEM_POOLTAG)
        base_addr = ctypes.addressof(_POOLTAG_BUF) + offset
        wanted = set(tags)
        found: dict[str, tuple[int, float]] = {}
        for idx in range(count):
            item = SYSTEM_POOLTAG.from_address(base_addr + idx * stride)
            raw_tag = bytes(item.Tag)
            if raw_tag in wanted:
                live = (item.PagedAllocs - item.PagedFrees) + (item.NonPagedAllocs - item.NonPagedFrees)
                used_mb = (item.PagedUsed + item.NonPagedUsed) / (1024 * 1024)
                found[raw_tag.decode("ascii", "replace")] = (int(live), used_mb)
        return found
    return None


@dataclass
class Sample:
    elapsed_s: float
    iteration: int
    self_rss_mb: float
    self_private_mb: float
    self_py_threads: int
    self_os_threads: int
    self_handles: int
    pool_paged_mb: float
    pool_nonpaged_mb: float
    sys_handles: int
    sys_threads: int
    npsm_pids: int
    npsm_rss_mb: float
    npsm_cpu_s: float
    npsm_threads: int
    npsm_handles: int
    cm_procs: int
    cm_threads: int
    cm_rss_mb: float
    cm_handles: int
    smtc_mode: str
    smtc_scans: int
    smtc_meta: int
    smtc_state: int
    smtc_events: int
    smtc_timeouts: int
    smtc_skips: int
    alms_mb: float | None = None
    icp_mb: float | None = None
    obtb_mb: float | None = None


def take_sample(
    iteration: int,
    started_at: float,
    reader: media.PlaybackReader,
    *,
    include_pooltags: bool = False,
) -> Sample:
    procs = _snapshot_processes()
    my_pid = os.getpid()
    self_rss, self_priv, _, self_handles = _inspect_pid(my_pid)
    self_os_threads = procs.get(my_pid, ("", 0))[1]

    pool_paged, pool_nonpaged, sys_handles, sys_threads = _kernel_perf()

    npsm_pids = _find_service_pids("npsmsvc")
    npsm_rss = npsm_cpu = 0.0
    npsm_threads = npsm_handles = 0
    for pid in npsm_pids:
        rss, _, cpu, handles = _inspect_pid(pid)
        npsm_rss += rss
        npsm_cpu += cpu
        npsm_handles += handles
        npsm_threads += procs.get(pid, ("", 0))[1]

    cm_pids = [pid for pid, (name, _) in procs.items() if name == "cloudmusic.exe"]
    cm_rss = 0.0
    cm_threads = cm_handles = 0
    for pid in cm_pids:
        rss, _, _, handles = _inspect_pid(pid)
        cm_rss += rss
        cm_handles += handles
        cm_threads += procs[pid][1]

    diag = reader.diagnostics()["smtc"]
    alms_mb = icp_mb = obtb_mb = None
    if include_pooltags:
        tags = _query_pool_tags()
        if tags is not None:
            alms_mb = tags.get("AlMs", (0, 0.0))[1]
            icp_mb = tags.get("Icp ", (0, 0.0))[1]
            obtb_mb = tags.get("Obtb", (0, 0.0))[1]

    return Sample(
        elapsed_s=time.monotonic() - started_at,
        iteration=iteration,
        self_rss_mb=self_rss,
        self_private_mb=self_priv,
        self_py_threads=threading.active_count(),
        self_os_threads=self_os_threads,
        self_handles=self_handles,
        pool_paged_mb=pool_paged,
        pool_nonpaged_mb=pool_nonpaged,
        sys_handles=sys_handles,
        sys_threads=sys_threads,
        npsm_pids=len(npsm_pids),
        npsm_rss_mb=npsm_rss,
        npsm_cpu_s=npsm_cpu,
        npsm_threads=npsm_threads,
        npsm_handles=npsm_handles,
        cm_procs=len(cm_pids),
        cm_threads=cm_threads,
        cm_rss_mb=cm_rss,
        cm_handles=cm_handles,
        smtc_mode=str(diag["mode"]),
        smtc_scans=int(diag["session_scans"]),
        smtc_meta=int(diag["metadata_reads"]),
        smtc_state=int(diag["state_reads"]),
        smtc_events=int(diag["events_received"]),
        smtc_timeouts=int(diag["timeouts"]),
        smtc_skips=int(diag["inflight_skips"]) + int(diag["backoff_skips"]),
        alms_mb=alms_mb,
        icp_mb=icp_mb,
        obtb_mb=obtb_mb,
    )


def _format_sample(label: str, s: Sample) -> str:
    extra = ""
    if s.alms_mb is not None:
        extra = f" | tags AlMs={s.alms_mb:.1f}M Icp={s.icp_mb:.1f}M Obtb={s.obtb_mb:.1f}M"
    return (
        f"{label:8s} t={s.elapsed_s:5.1f}s "
        f"| self rss={s.self_rss_mb:5.1f}M thr={s.self_os_threads:2d}(py={s.self_py_threads}) hnd={s.self_handles:4d} "
        f"| pool P={s.pool_paged_mb:6.1f}M NP={s.pool_nonpaged_mb:5.1f}M "
        f"| NPSM rss={s.npsm_rss_mb:5.1f}M cpu={s.npsm_cpu_s:6.2f}s thr={s.npsm_threads:2d} "
        f"| cloudmusic thr={s.cm_threads:3d}({s.cm_procs}) "
        f"| smtc[{s.smtc_mode}] scan={s.smtc_scans} meta={s.smtc_meta} state={s.smtc_state} "
        f"evt={s.smtc_events} to={s.smtc_timeouts}"
        f"{extra}"
    )


async def run_probe(args: argparse.Namespace) -> int:
    reader = media.PlaybackReader(smtc_interval=args.smtc_interval)
    samples: list[Sample] = []
    started = time.monotonic()
    deadline = (started + args.duration) if args.duration > 0 else None

    print(
        f"SMTC leak probe pid={os.getpid()} interval={args.interval}s "
        f"smtc_interval={args.smtc_interval}s use_sixk={args.use_sixk}"
    )
    if args.pooltags and _query_pool_tags() is None:
        print("  note: NtQuerySystemInformation(PoolTag) unavailable (need elevated/policy); using GetPerformanceInfo")

    start_sample = take_sample(0, started, reader, include_pooltags=args.pooltags)
    samples.append(start_sample)
    print(_format_sample("START", start_sample))

    iteration = 0
    try:
        while True:
            if deadline is not None:
                if time.monotonic() >= deadline:
                    break
            elif iteration >= args.iterations:
                break

            iteration += 1
            try:
                await reader.read(use_sixk=args.use_sixk)
            except Exception as exc:  # noqa: BLE001
                if iteration % 50 == 1:
                    print(f"  iter {iteration} read failed: {type(exc).__name__}: {exc}")

            if iteration % args.report_every == 0:
                sample = take_sample(iteration, started, reader, include_pooltags=args.pooltags)
                samples.append(sample)
                print(_format_sample(f"#{iteration}", sample))

            await asyncio.sleep(args.interval)
    finally:
        end_sample = take_sample(iteration, started, reader, include_pooltags=args.pooltags)
        samples.append(end_sample)
        print(_format_sample("END", end_sample))
        reader.close()

    # Print delta summary
    peak_paged = max(s.pool_paged_mb for s in samples)
    peak_npsm_rss = max(s.npsm_rss_mb for s in samples)
    peak_cm_thr = max(s.cm_threads for s in samples)
    peak_self_thr = max(s.self_os_threads for s in samples)
    peak_self_hnd = max(s.self_handles for s in samples)
    total_winrt = end_sample.smtc_scans + end_sample.smtc_meta + end_sample.smtc_state
    ratio = (1.0 - total_winrt / max(1, iteration * 4)) * 100.0

    print("\n=== Summary (Start -> Peak -> End | Delta) ===")
    print(f"  Iterations           : {iteration} ticks ({end_sample.elapsed_s:.1f}s)")
    print(f"  WinRT calls          : scans={end_sample.smtc_scans}, meta={end_sample.smtc_meta}, "
          f"state={end_sample.smtc_state}, events={end_sample.smtc_events}, timeouts={end_sample.smtc_timeouts} "
          f"(~{ratio:.1f}% reduction vs uncached 4-call/tick)")
    print(f"  Self RSS (MB)        : {start_sample.self_rss_mb:.1f} -> {max(s.self_rss_mb for s in samples):.1f} "
          f"-> {end_sample.self_rss_mb:.1f} (delta {end_sample.self_rss_mb - start_sample.self_rss_mb:+.1f})")
    print(f"  Self OS threads      : {start_sample.self_os_threads} -> {peak_self_thr} "
          f"-> {end_sample.self_os_threads} (delta {end_sample.self_os_threads - start_sample.self_os_threads:+d})")
    print(f"  Self handles         : {start_sample.self_handles} -> {peak_self_hnd} "
          f"-> {end_sample.self_handles} (delta {end_sample.self_handles - start_sample.self_handles:+d})")
    print(f"  Kernel Paged (MB)    : {start_sample.pool_paged_mb:.1f} -> {peak_paged:.1f} "
          f"-> {end_sample.pool_paged_mb:.1f} (delta {end_sample.pool_paged_mb - start_sample.pool_paged_mb:+.1f})")
    print(f"  Kernel NonPaged (MB) : {start_sample.pool_nonpaged_mb:.1f} -> {max(s.pool_nonpaged_mb for s in samples):.1f} "
          f"-> {end_sample.pool_nonpaged_mb:.1f} (delta {end_sample.pool_nonpaged_mb - start_sample.pool_nonpaged_mb:+.1f})")
    print(f"  NPSMSvc RSS / CPU    : {start_sample.npsm_rss_mb:.1f}M -> {peak_npsm_rss:.1f}M -> {end_sample.npsm_rss_mb:.1f}M "
          f"| CPU delta {end_sample.npsm_cpu_s - start_sample.npsm_cpu_s:+.3f}s")
    print(f"  cloudmusic.exe thr   : {start_sample.cm_threads} -> {peak_cm_thr} "
          f"-> {end_sample.cm_threads} (delta {end_sample.cm_threads - start_sample.cm_threads:+d})")

    if args.csv:
        out_path = Path(args.csv)
        fieldnames = list(Sample.__dataclass_fields__.keys())
        with out_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for item in samples:
                writer.writerow(item.__dict__)
        print(f"  CSV written          : {out_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("iterations_pos", nargs="?", type=int, default=None,
                        help="optional positional iterations (legacy compatibility)")
    parser.add_argument("interval_pos", nargs="?", type=float, default=None,
                        help="optional positional interval in seconds (legacy compatibility)")
    parser.add_argument("--iterations", type=int, default=600)
    parser.add_argument("--duration", type=float, default=0.0,
                        help="run for N seconds instead of fixed iterations")
    parser.add_argument("--interval", type=float, default=0.1,
                        help="loop tick interval in seconds (default 0.1s)")
    parser.add_argument("--smtc-interval", type=float, default=0.5,
                        help="PlaybackReader SMTC poll interval in seconds (default 0.5s)")
    parser.add_argument("--report-every", type=int, default=50)
    parser.add_argument("--use-sixk", action="store_true",
                        help="include 6K Labs source (disabled by default to stress SMTC)")
    parser.add_argument("--pooltags", action="store_true",
                        help="attempt to read AlMs/Icp /Obtb kernel pool tags")
    parser.add_argument("--csv", default="", help="write time-series CSV to path")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.iterations_pos is not None:
        args.iterations = args.iterations_pos
    if args.interval_pos is not None:
        args.interval = args.interval_pos
    return asyncio.run(run_probe(args))


if __name__ == "__main__":
    raise SystemExit(main())
