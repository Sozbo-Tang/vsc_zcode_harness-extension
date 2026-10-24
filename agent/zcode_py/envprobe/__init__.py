"""环境探测包（M5，PLAN §2.5 R7/R21 + §6.4 环境面板）。

``probe(workspace_roots) -> dict`` 与协议 ``env_info`` 帧（protocol.EnvInfo）
字段对齐::

    {
        "hardware": {platform, arch, os, cpuModel, cpuCores,
                     ramTotalBytes, ramTotal, gpuModel, gpuVram},
        "workspaceLangs": [{language, label, markers, markerPaths, missing,
                            version, packageManagers, extras, installHint}],
    }

``environment_facts`` 把探测结果压成 ≤1500 字符的「环境事实」段，供服务层
注入 system prompt（R21：以上事实注入 Agent 上下文）。全部 subprocess 调用
为字面量 argv（见 _exec），离线测试以 monkeypatch subprocess.run 喂假输出。
"""

from __future__ import annotations

from .hardware import probe_hardware
from .langs import probe_workspace_langs

FACTS_LIMIT = 1500


def probe(workspace_roots: list) -> dict:
    """硬件 + 工作区语言探测（对齐 env_info 帧：hardware / workspaceLangs）。"""
    return {
        "hardware": probe_hardware(),
        "workspaceLangs": probe_workspace_langs(workspace_roots),
    }


def _hardware_line(hw: dict) -> str:
    parts: list[str] = []
    cpu_txt = str(hw.get("cpuModel") or "unknown CPU")
    cores = hw.get("cpuCores")
    if cores:
        cpu_txt += f", {cores} cores"
    parts.append("CPU " + cpu_txt)
    ram = hw.get("ramTotal") or _human_bytes(hw.get("ramTotalBytes"))
    if ram:
        parts.append("RAM " + ram)
    os_bits = [str(hw.get("os") or "")]
    arch = hw.get("arch")
    if arch:
        os_bits.append(f"({arch})")
    os_txt = " ".join(b for b in os_bits if b).strip()
    if os_txt:
        parts.append("OS " + os_txt)
    gpu = hw.get("gpuModel")
    if gpu:
        vram = hw.get("gpuVram")
        parts.append("GPU " + (f"{gpu}, {vram}" if vram else gpu))
    return "; ".join(parts)


def _human_bytes(total_bytes) -> str | None:
    if not total_bytes or total_bytes <= 0:
        return None
    gb = total_bytes / 1024**3
    if gb >= 10:
        return f"{gb:.0f} GB"
    if gb >= 1:
        return f"{gb:.1f} GB"
    return f"{total_bytes / 1024**2:.0f} MB"


def _lang_line(entry: dict) -> str:
    label = str(entry.get("label") or entry.get("language") or "?")
    markers = ", ".join(entry.get("markers") or [])
    if entry.get("missing"):
        hint = entry.get("installHint") or "install the toolchain"
        return f"{label}: MISSING toolchain — {hint} (markers: {markers})"
    bits = [str(entry.get("version") or "version unknown")]
    managers = entry.get("packageManagers") or {}
    if managers:
        bits.append("; ".join(f"{k} {v}" for k, v in managers.items()))
    extras = entry.get("extras") or {}
    if extras:
        bits.append("; ".join(f"{k} {v}" for k, v in extras.items()))
    return f"{label}: " + "; ".join(bits) + f" (markers: {markers})"


def environment_facts(env: dict, *, limit: int = FACTS_LIMIT) -> str:
    """探测结果 → ≤limit 字符的「环境事实」段（注入 system prompt 尾部）。"""
    lines = ["Environment facts (auto-detected):"]
    hardware = env.get("hardware") or {}
    hw_line = _hardware_line(hardware)
    if hw_line:
        lines.append("Hardware: " + hw_line)
    for entry in env.get("workspaceLangs") or []:
        lines.append(_lang_line(entry))
    text = "\n".join(lines)
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


__all__ = ["probe", "environment_facts", "probe_hardware", "probe_workspace_langs"]
