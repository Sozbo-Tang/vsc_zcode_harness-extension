"""设置页持久层 ~/.zcode-py/config/settings.json（PLAN §6.5 七组设置）。

设计决策：

- **只存受管键**（:data:`MANAGED_KEYS`）：settings.json 是设置页的真源；
  ``init.settings``（宿主注入的 VS Code 侧配置，如 permissionMode）只做
  运行时覆盖、绝不回写本文件——否则宿主快照会污染用户显式设置。
- **缺省不落盘**：首次加载只写 ``{"version": 1}`` 骨架。消费点各自带
  缺省（``maxTurns or 8``、中继档位回落 describe、阈值 CompactionManager
  构造缺省），文件里出现某键 = 用户显式覆盖。这样 providers.json 里
  provider 条目级的 ``visionRelayPreset`` / ``compactionModel`` 在用户没
  在设置页动过之前仍然生效（settings 覆盖 provider 条目，见
  vision_relay / compaction 消费点）。
- **原子写**：先写同目录临时文件再 :func:`os.replace`，进程被杀不会留
  半截 JSON。
- 损坏文件按空设置降级（读取方广播 config error 的责任在服务层）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .config import home_dir
from .vision_relay import PRESET_IDS

SETTINGS_VERSION = 1

# 默认轮数上限（loop.max_turns 缺省，与 loop.py AgentLoop 构造缺省一致）
DEFAULT_MAX_TURNS = 8
MAX_TURNS_LIMIT = 200

# 隐私护栏文件模式的缺省清单（privacy.PRIVACY_BASENAME_PATTERNS 的拷贝，
# 单独成函数避免 import 环：privacy 不 import 本模块）
def default_privacy_file_patterns() -> list[str]:
    from .privacy import PRIVACY_BASENAME_PATTERNS

    return list(PRIVACY_BASENAME_PATTERNS)


def settings_path() -> Path:
    return home_dir() / "config" / "settings.json"


def load_settings(path: Path | None = None) -> dict:
    """读设置；文件不存在时落一份最小骨架（version only，缺省不落盘）。"""
    target = path or settings_path()
    if not target.exists():
        save_settings({"version": SETTINGS_VERSION}, target)
        return {"version": SETTINGS_VERSION}
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": SETTINGS_VERSION}
    return data if isinstance(data, dict) else {"version": SETTINGS_VERSION}


def save_settings(data: dict, path: Path | None = None) -> Path:
    """原子写：tmp + os.replace（同目录保证 rename 原子性）。"""
    target = path or settings_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(tmp, target)
    return target


def merge_settings(base: dict, overlay: dict) -> dict:
    """浅层合并 + dict 值递归一层（compaction 这类嵌套组不整块覆盖）。"""
    out = dict(base)
    for key, value in overlay.items():
        if (
            key in out
            and isinstance(out[key], dict)
            and isinstance(value, dict)
        ):
            out[key] = {**out[key], **value}
        else:
            out[key] = value
    return out


def normalize_patch(patch: dict) -> tuple[dict, str]:
    """settings_set 载荷规整 → (受管键子集, 错误信息)。

    只收 :data:`MANAGED_KEYS` 认识的键（未知键忽略——前向兼容）；值不合法
    返回错误信息（调用方回 protocol error，不落盘）。单键 ``value`` 通道与
    整体 ``settings`` 通道共用本函数。
    """
    cleaned: dict = {}
    for key, value in patch.items():
        if key == "systemPromptAppend":
            if not isinstance(value, str):
                return {}, "systemPromptAppend must be a string"
            cleaned[key] = value
        elif key == "maxTurns":
            if isinstance(value, bool) or not isinstance(value, int):
                return {}, "maxTurns must be an integer"
            if not 1 <= value <= MAX_TURNS_LIMIT:
                return {}, f"maxTurns must be within 1..{MAX_TURNS_LIMIT}"
            cleaned[key] = value
        elif key == "compactionModel":
            if not isinstance(value, str):
                return {}, "compactionModel must be a string (providerId/modelId or '')"
            cleaned[key] = value.strip()
        elif key == "visionRelayPreset":
            if value not in PRESET_IDS:
                return {}, f"visionRelayPreset must be one of {list(PRESET_IDS)}"
            cleaned[key] = value
        elif key == "visionRelayCustomPrompt":
            if not isinstance(value, str):
                return {}, "visionRelayCustomPrompt must be a string"
            cleaned[key] = value
        elif key == "privacyFilePatterns":
            if not isinstance(value, list) or not all(
                isinstance(p, str) for p in value
            ):
                return {}, "privacyFilePatterns must be an array of strings"
            cleaned[key] = [p for p in value if p.strip()]
        elif key == "telegraphClickSound":
            # R6 收尾：车钟换挡咔哒音效开关（纯 webview 消费，核侧只存取
            # 不接线——bool 之外一律拒收，防脏值落盘）
            if not isinstance(value, bool):
                return {}, "telegraphClickSound must be a boolean"
            cleaned[key] = value
        elif key == "compaction":
            # 嵌套组：只收组内受管键（evictThreshold 留给宿主注入通道）
            if not isinstance(value, dict):
                return {}, "compaction must be an object"
            group: dict = {}
            for sub, sub_value in value.items():
                if sub == "summarizeThreshold":
                    if (
                        isinstance(sub_value, bool)
                        or not isinstance(sub_value, (int, float))
                        or not 0 < float(sub_value) <= 1
                    ):
                        return {}, "compaction.summarizeThreshold must be a number in (0, 1]"
                    group[sub] = float(sub_value)
            if group:
                cleaned["compaction"] = group
        # 未知键忽略（version 除外也忽略——版本号由本模块管理）
    return cleaned, ""


MANAGED_KEYS = (
    "systemPromptAppend",
    "maxTurns",
    "compactionModel",
    "compaction",
    "visionRelayPreset",
    "visionRelayCustomPrompt",
    "privacyFilePatterns",
    "telegraphClickSound",
)


def apply_patch(persisted: dict, cleaned: dict) -> dict:
    """受管补丁并入持久层（嵌套组一层合并；空 compaction 组剔除）。"""
    out = merge_settings(persisted, cleaned)
    if "compaction" in out and not (out.get("compaction") or {}):
        out.pop("compaction")
    return out


__all__ = [
    "DEFAULT_MAX_TURNS",
    "MANAGED_KEYS",
    "MAX_TURNS_LIMIT",
    "SETTINGS_VERSION",
    "apply_patch",
    "default_privacy_file_patterns",
    "load_settings",
    "merge_settings",
    "normalize_patch",
    "save_settings",
    "settings_path",
]
