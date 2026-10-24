"""硬件探测（R7，PLAN §2.5 / §6.4 硬件卡）：CPU 型号/核数、RAM 总量、
OS 版本/架构、GPU 型号/显存尽力。

三平台策略：

- **macOS**：sysctl 字面量 argv 查 ``machdep.cpu.brand_string`` / ``hw.ncpu``
  / ``hw.memsize``；OS 版本走 ``platform.mac_ver()``（纯标准库，无子进程）。
  GPU：Apple Silicon 为 SoC 集成 GPU、无独立显存概念——如实标注
  ``integrated (Apple Silicon)`` / 共享显存，不做慢速 system_profiler 扫描。
- **Linux**：读 /proc（cpuinfo / meminfo）与 /etc/os-release；文件不存在或
  字段缺失一律置 None。GPU 尽力 ``nvidia-smi``（字面量 argv；未装则 None）。
- **Windows**：platform 模块尽力 + ``ctypes GlobalMemoryStatusEx`` 读 RAM
  （纯标准库调用）；GPU 置 None（不引入 wmic/powershell 之类 shell 形态）。

全部 subprocess 调用一律字面量 argv（_exec.run_argv），测试以 monkeypatch
subprocess.run 喂假输出。
"""

from __future__ import annotations

import os
import platform
import sys

from ._exec import completed_output, run_argv

# 模块级常量而非内联比较：测试可 monkeypatch 切换平台分支
_PLATFORM = sys.platform


def _read_text(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def _base_info() -> dict:
    return {
        "platform": _PLATFORM,
        "arch": platform.machine() or None,
        "os": None,
        "cpuModel": None,
        "cpuCores": os.cpu_count(),
        "ramTotalBytes": None,
        "ramTotal": None,
        "gpuModel": None,
        "gpuVram": None,
    }


def _human_ram(total_bytes: int | None) -> str | None:
    if not total_bytes or total_bytes <= 0:
        return None
    gb = total_bytes / 1024**3
    if gb >= 10:
        return f"{gb:.0f} GB"
    if gb >= 1:
        return f"{gb:.1f} GB"
    return f"{total_bytes / 1024**2:.0f} MB"


def _probe_darwin() -> dict:
    info = _base_info()
    brand = completed_output(run_argv(["sysctl", "-n", "machdep.cpu.brand_string"]))
    info["cpuModel"] = brand
    ncpu = completed_output(run_argv(["sysctl", "-n", "hw.ncpu"]))
    try:
        info["cpuCores"] = int(ncpu) if ncpu else os.cpu_count()
    except ValueError:
        info["cpuCores"] = os.cpu_count()
    memsize = completed_output(run_argv(["sysctl", "-n", "hw.memsize"]))
    try:
        info["ramTotalBytes"] = int(memsize) if memsize else None
    except ValueError:
        info["ramTotalBytes"] = None
    info["ramTotal"] = _human_ram(info["ramTotalBytes"])
    version = platform.mac_ver()[0]
    info["os"] = f"macOS {version}" if version else "macOS"
    # Apple Silicon：GPU 集成在 SoC 上，无独立显存概念——如实描述而非瞎报
    if brand and brand.startswith("Apple"):
        info["gpuModel"] = f"{brand} GPU (integrated, Apple Silicon)"
        info["gpuVram"] = "shared system memory"
    return info


def _parse_proc_cpuinfo(content: str | None) -> str | None:
    if not content:
        return None
    for line in content.splitlines():
        if line.startswith("model name") and ":" in line:
            return line.split(":", 1)[1].strip() or None
    return None


def _parse_proc_meminfo(content: str | None) -> int | None:
    if not content:
        return None
    for line in content.splitlines():
        if line.startswith("MemTotal:"):
            parts = line.split()
            if len(parts) >= 2:
                try:
                    return int(parts[1]) * 1024  # /proc/meminfo 单位 kB
                except ValueError:
                    return None
    return None


def _parse_os_release(content: str | None) -> str | None:
    if not content:
        return None
    for line in content.splitlines():
        if line.startswith("PRETTY_NAME="):
            return line.split("=", 1)[1].strip().strip('"') or None
    return None


def _parse_nvidia_smi(output: str | None) -> tuple[str | None, str | None]:
    """``nvidia-smi --query-gpu=name,memory.total --format=csv,noheader`` 输出形如
    ``NVIDIA GeForce RTX 4090, 24576 MiB``。"""
    if not output:
        return None, None
    first = output.splitlines()[0]
    name, _, vram = first.partition(",")
    name = name.strip() or None
    vram = vram.replace("MiB", "").strip()
    try:
        mib = int(vram)
        vram_txt = f"{mib / 1024:.0f} GB" if mib >= 1024 else f"{mib} MiB"
    except ValueError:
        vram_txt = vram or None
    return name, vram_txt


def _probe_linux() -> dict:
    info = _base_info()
    info["cpuModel"] = _parse_proc_cpuinfo(_read_text("/proc/cpuinfo"))
    info["cpuCores"] = os.cpu_count()
    info["ramTotalBytes"] = _parse_proc_meminfo(_read_text("/proc/meminfo"))
    info["ramTotal"] = _human_ram(info["ramTotalBytes"])
    os_name = _parse_os_release(_read_text("/etc/os-release"))
    if os_name is None:
        os_name = _parse_os_release(_read_text("/usr/lib/os-release"))
    if os_name is None and platform.release():
        os_name = f"Linux {platform.release()}"
    info["os"] = os_name
    gpu, vram = _parse_nvidia_smi(
        completed_output(
            run_argv(
                ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"]
            )
        )
    )
    info["gpuModel"] = gpu
    info["gpuVram"] = vram
    return info


def _windows_ram_bytes() -> int | None:
    import ctypes

    windll = getattr(ctypes, "windll", None)
    if windll is None:
        return None

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    try:
        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            return int(stat.ullTotalPhys)
    except Exception:  # noqa: BLE001 —— 尽力探测，失败置 None
        return None
    return None


def _probe_windows() -> dict:
    info = _base_info()
    info["cpuModel"] = platform.processor() or None
    info["cpuCores"] = os.cpu_count()
    info["ramTotalBytes"] = _windows_ram_bytes()
    info["ramTotal"] = _human_ram(info["ramTotalBytes"])
    release = platform.release()
    info["os"] = f"Windows {release}" if release else "Windows"
    # GPU：Windows 尽力语义下不引入 wmic/powershell（shell 形态调用被禁），置 None
    return info


def probe_hardware() -> dict:
    """探测硬件事实；不可得字段一律 None（UI/注入层各自降级）。"""
    if _PLATFORM == "darwin":
        return _probe_darwin()
    if _PLATFORM == "linux":
        return _probe_linux()
    if _PLATFORM in ("win32", "cygwin"):
        return _probe_windows()
    info = _base_info()
    info["os"] = platform.system() or None
    return info
